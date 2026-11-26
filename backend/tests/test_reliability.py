"""M4 evidence reliability: transcript confidence combined with measured audio.

The audio is generated and measured for real (FFmpeg ``astats`` via ``signals``):
* 0-8 s: room noise at about -77 dBFS (measured silence) with quiet commentary (two
  -55 dBFS bursts at 5 s and 6 s, still inside measured silence);
* 8-14 s: loud background music (a -20 dBFS chord);
* 16 s and 17 s: normal speech at about -13 dBFS.
Words are placed on it with chosen engine probabilities. No universal threshold is
validated here; the tests pin what each rule does and does not do."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Job, JobType, SignalKind, SystemEvent
from app.services import (
    acquisition,
    analysis_evidence,
    clips,
    evidence_reliability,
    highlight_scan,
    queue,
    signals,
    transcripts,
)
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.test_ingest import drain
from tests.test_rights import World
from tests.test_transcripts import engine  # noqa: F401  (fixture)

AUDIO = ("anoisesrc=d=20:c=white:a=0.0006:r=48000[n];"
         "aevalsrc='if(between(t,5,5.4)+between(t,6,6.4),0.0024*sin(2*PI*700*t),0)"
         "+if(between(t,8,14),0.08*(sin(2*PI*220*t)+sin(2*PI*277*t)+sin(2*PI*330*t)),0)"
         "+if(between(t,16,16.4)+between(t,17,17.4),0.3*sin(2*PI*800*t),0)'"
         ":s=48000:d=20[s];[n][s]amix=inputs=2:normalize=0[a]")
WORDS = [  # text, start, end, prob, expected class, scenario
    ("you", 2000, 2300, 0.34, "suspected_hallucination", "silence: low-probability word"),
    ("thanks", 3000, 3300, 0.92, "audio_contradicted", "silence: confident word"),
    ("quietly", 5000, 5400, 0.88, "reliable", "genuine low-volume commentary"),
    ("whisper", 6000, 6400, 0.31, "low_confidence", "quiet speech, unsure engine"),
    ("la", 10000, 10300, 0.22, "low_confidence", "background music, unsure engine"),
    ("drive", 11000, 11400, 0.91, "reliable", "speech over background music"),
    ("go", 16000, 16400, 0.95, "reliable", "normal speech"),
    ("now", 17000, 17000, 0.9, "degenerate_timing", "zero-length word"),
]


def _audio_series(tmp_path: Path) -> dict:
    wav = tmp_path / "rel.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_complex", AUDIO,
                    "-map", "[a]", "-ac", "1", str(wav)], check=True, timeout=120)
    r = subprocess.run(signals.audio_command("ffmpeg", wav), capture_output=True, timeout=120,
                       check=True)
    series, _ = signals.analyse_audio(signals.parse_metadata(r.stdout.decode()), 20_000)
    return series


def _words():
    return [{"idx": i, "w": t, "start_ms": a, "end_ms": b, "prob": p}
            for i, (t, a, b, p, _, _) in enumerate(WORDS)]


@requires_ffmpeg
def test_each_scenario_gets_the_expected_class_with_reasons(tmp_path):
    series = _audio_series(tmp_path)
    assert series["silences_ms"][0] == [0, 8000]          # quiet speech is inside "silence"
    got = evidence_reliability.assess_words(_words(), series)
    for (text, *_rest, expected, scenario), a in zip(WORDS, got, strict=True):
        assert a["class"] == expected, (scenario, text, a)
        assert a["reasons"] or expected == "reliable", scenario
    by = {w[0]: a for w, a in zip(WORDS, got, strict=True)}
    # The audio contradicts the word and the engine is unsure: suppressed, never evidence.
    assert by["you"]["caption"] == "suppress" and not by["you"]["can_support"]
    # A confident engine keeps the word visible; it still cannot trigger on its own.
    assert by["thanks"]["caption"] == "show" and not by["thanks"]["can_trigger"]
    assert by["thanks"]["can_support"] and by["thanks"]["needs_corroboration"]
    # Genuine quiet commentary: below the silence level but well above the room floor.
    assert by["quietly"]["measured"]["snr_db"] > 15 and by["quietly"]["can_trigger"]
    # Low engine confidence alone (quiet speech, music) never suppresses anything...
    for text in ("whisper", "la"):
        assert by[text]["caption"] == "show" and by[text]["can_support"]
        assert not by[text]["can_trigger"] and by[text]["needs_corroboration"]
        assert "not validated" in by[text]["reasons"][-1]
    # ...and music is not mistaken for silence.
    assert by["la"]["measured"]["silence_fraction"] == 0
    assert by["drive"]["can_trigger"]
    assert not by["now"]["can_trigger"] and not by["now"]["can_support"]


def test_without_audio_measurement_nothing_is_suppressed_or_trusted_alone():
    got = evidence_reliability.assess_words(_words(), None)
    assert {a["class"] for a in got} == {"unassessed_audio", "degenerate_timing"}
    assert all(a["caption"] == "show" for a in got)
    assert not any(a["can_trigger"] for a in got)
    silent = evidence_reliability.assess_words(_words()[:1], None, audio_state="no_audio")
    assert "no audio track" in silent[0]["reasons"][0]


def test_profile_is_versioned_and_marks_the_probability_threshold_unvalidated():
    p = evidence_reliability.PROFILE
    assert p["version"] == 1 and p["low_prob_validated"] is False
    assert p["audio_thresholds_validated"] is False
    assert len(evidence_reliability.profile_fingerprint()) == 64


# --------------------------------------------------- through the real pipeline
@pytest.fixture
def recording(db, settings, tmp_path, engine, monkeypatch):  # noqa: F811
    mp4 = tmp_path / "rel.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=30:duration=20", "-filter_complex", AUDIO,
                    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", "20",
                    str(mp4)], check=True, timeout=120)
    rec = import_file(db, settings, mp4).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)

    def fake_engine(settings_, source, work, chunk, run):       # engine output stand-in
        return ({"words": [{"w": t, "start_ms": a - chunk.start_ms, "end_ms": b - chunk.start_ms,
                            "prob": p, "seg": 0} for t, a, b, p, *_ in WORDS],
                 "engine_version": "test-engine 1"}, "0" * 64, 1)

    monkeypatch.setattr(transcripts, "transcribe_chunk", fake_engine)
    with db.session() as s:
        transcripts.request(s, settings, rec.id)
        signals.request(s, settings, rec.id, SignalKind.AUDIO)
    drain(db, settings)
    return rec


@requires_ffmpeg
def test_captions_suppress_with_reasons_and_the_raw_transcript_is_unchanged(db, settings,
                                                                           recording):
    words, tr_id = clips._words(db, settings, recording, 0, 20_000)
    assert [w["w"] for w in words] == ["thanks", "quietly", "whisper", "la", "drive", "go",
                                       "now"]
    with db.session() as s:
        raw = transcripts.words(s, tr_id)
        assert [w["w"] for w in raw] == [w[0] for w in WORDS]   # nothing removed from raw
        events = s.scalars(select(SystemEvent).where(
            SystemEvent.event_type == "caption_words_suppressed")).all()
        assert len(events) == 1
        assert '"you"' in events[0].message and "suspected_hallucination" in events[0].message
    trig, _ = clips._words(db, settings, recording, 0, 20_000, "trigger")
    assert [w["w"] for w in trig] == ["quietly", "drive", "go"]


@requires_ffmpeg
def test_evidence_carries_reliability_and_scan_ignores_unreliable_words(db, settings,
                                                                         recording,
                                                                         monkeypatch):
    with db.session() as s:
        bundle = analysis_evidence.window(s, settings, recording.id, 0, 20_000)
    words = {w["text"]: w["reliability"] for w in bundle["transcript"]["words"]}
    assert words["you"]["class"] == "suspected_hallucination"
    assert words["la"]["class"] == "low_confidence" and words["la"]["can_support"]
    counts = bundle["transcript"]["reliability"]["counts"]
    assert counts["suspected_hallucination"] == 1 and counts["audio_contradicted"] == 1
    assert counts["reliable"] == 3
    assert bundle["transcript"]["reliability"]["time_basis"] == "raw word times"
    assert words["thanks"]["needs_corroboration"] and "snr_db" in words["thanks"]["measured"]
    speech = bundle["transcript"]["speech"]
    assert speech["word_count"] == len(WORDS) - 1
    assert speech["excluded_suspected_hallucinations"] == 1
    seen = {}
    real = highlight_scan.scan_signals

    def scan(series, ws, duration_ms):
        seen["words"] = [w["w"] for w in ws]
        return real(series, ws, duration_ms)

    monkeypatch.setattr(highlight_scan, "scan_signals", scan)
    with db.session() as s:
        queue.enqueue(s, recording.id, JobType.HIGHLIGHT_SCAN, 3)
    drain(db, settings)
    assert seen["words"] == ["quietly", "drive", "go"]
    with db.session() as s:
        job = s.scalar(select(Job).where(Job.job_type == "HIGHLIGHT_SCAN"))
        result = json.loads(job.result_json)
        assert result["word_reliability"]["counts"]["suspected_hallucination"] == 1
        assert result["word_reliability"]["fallback"] is None
        assert set(result["signal_run_ids"]) == {"audio", "visual"}


# ------------------------------------------- independent review of PR #20 (2026-11-25)
def _flat_series(bed_db: float, word: tuple[int, int, float]) -> dict:
    """A measured series: a quiet bed everywhere (all of it under the -50 dBFS silence
    level), with one louder stretch for the word."""
    t = list(range(0, 20_000, 100))
    rms = [word[2] if word[0] <= x < word[1] else bed_db for x in t]
    return {"t_ms": t, "step_ms": 100, "rms_db": rms, "silences_ms": [[0, 20_000]]}


def test_review_quiet_commentary_over_a_quiet_bed_is_shown_when_the_engine_is_sure():
    series = _flat_series(-58.0, (5000, 5400, -55.0))       # 3 dB above the bed
    word = [{"idx": 0, "w": "nice", "start_ms": 5000, "end_ms": 5400, "prob": 0.9}]
    a = evidence_reliability.assess_words(word, series)[0]
    assert a["class"] == "audio_contradicted" and a["caption"] == "show"
    assert a["measured"]["snr_db"] == 3.0 and "not validated" in a["reasons"][0]
    unsure = evidence_reliability.assess_words([{**word[0], "prob": 0.3}], series)[0]
    assert unsure["class"] == "suspected_hallucination" and unsure["caption"] == "suppress"


def test_review_triggering_fallback_only_when_audio_failed():
    ws = _words()
    pending = evidence_reliability.assess_words(ws, None, audio_state="pending")
    assert evidence_reliability.triggering(ws, pending, "pending") == []
    failed = evidence_reliability.assess_words(ws, None, audio_state="failed")
    got = [w["w"] for w in evidence_reliability.triggering(ws, failed, "failed")]
    assert got == ["thanks", "quietly", "drive", "go"]        # prob >= 0.5, not degenerate
    assert "measurement failed" in failed[0]["reasons"][0]


@pytest.fixture
def transcribed(db, settings, tmp_path, engine, monkeypatch):  # noqa: F811
    """The same recording, transcribed but with no audio measurement requested."""
    mp4 = tmp_path / "rel.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=30:duration=20", "-filter_complex", AUDIO,
                    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "20", str(mp4)],
                   check=True, timeout=120)
    rec = import_file(db, settings, mp4).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)

    def fake_engine(settings_, source, work, chunk, run):
        return ({"words": [{"w": t, "start_ms": a - chunk.start_ms, "end_ms": b - chunk.start_ms,
                            "prob": p, "seg": 0} for t, a, b, p, *_ in WORDS],
                 "engine_version": "test-engine 1"}, "0" * 64, 1)

    monkeypatch.setattr(transcripts, "transcribe_chunk", fake_engine)
    with db.session() as s:
        transcripts.request(s, settings, rec.id)
    drain(db, settings)
    return rec


@requires_ffmpeg
def test_review_suggestions_wait_for_the_audio_measurement(db, settings, transcribed):
    from app.core.errors import ConflictError

    with pytest.raises(ConflictError) as e:
        clips._words(db, settings, transcribed, 0, 20_000, "trigger")
    assert e.value.code == "audio_signals_pending"
    with db.session() as s:
        assert s.scalar(select(Job).where(Job.job_type == "AUDIO_SIGNALS")) is not None
    drain(db, settings)
    trig, _ = clips._words(db, settings, transcribed, 0, 20_000, "trigger")
    assert [w["w"] for w in trig] == ["quietly", "drive", "go"]


@requires_ffmpeg
def test_review_failed_audio_measurement_falls_back_and_says_so(db, settings, transcribed,
                                                               monkeypatch):
    from app.core.errors import PermanentJobError

    real = signals.measure

    def broken(kind, *a, **k):
        if kind == SignalKind.AUDIO:
            raise PermanentJobError("audio decoder broke")
        return real(kind, *a, **k)

    monkeypatch.setattr(signals, "measure", broken)
    with db.session() as s:
        signals.request(s, settings, transcribed.id, SignalKind.AUDIO)
        queue.enqueue(s, transcribed.id, JobType.HIGHLIGHT_SCAN, 3)
    drain(db, settings)
    with db.session() as s:
        assert s.scalar(select(Job).where(Job.job_type == "AUDIO_SIGNALS")).status == "FAILED"
        scan = s.scalar(select(Job).where(Job.job_type == "HIGHLIGHT_SCAN"))
        assert scan.status == "COMPLETED", scan.last_error
        rel = json.loads(scan.result_json)["word_reliability"]
        assert rel["audio"] == "failed" and "pre-M4" in rel["fallback"]
    trig, _ = clips._words(db, settings, transcribed, 0, 20_000, "trigger")
    assert [w["w"] for w in trig] == ["thanks", "quietly", "drive", "go"]


@requires_ffmpeg
def test_review_suppression_events_are_complete_and_not_repeated(db, settings, recording,
                                                                monkeypatch):
    monkeypatch.setattr(clips, "SUPPRESSED_PER_EVENT", 1)
    many = [{"idx": i, "w": "x" * 300, "start_ms": i, "reasons": ["r" * 200]}
            for i in range(12)]
    with db.session() as s:
        clips._record_suppressed(s, "t-1", recording.id, many)
        clips._record_suppressed(s, "t-1", recording.id, many)      # rendered again
        clips._record_suppressed(s, "t-2", recording.id, many[:1])  # another transcript
    with db.session() as s:
        msgs = s.scalars(select(SystemEvent.message).where(
            SystemEvent.event_type == "caption_words_suppressed",
            SystemEvent.related_recording_id == recording.id)).all()
    ours = [m for m in msgs if "transcript t-1 " in m]
    assert len(ours) == 12 and len([m for m in msgs if "transcript t-2 " in m]) == 1
    assert sorted(json.loads(m.split(" :: ", 1)[1])[0]["idx"] for m in ours) == list(range(12))
    clips._words(db, settings, recording, 0, 20_000)                  # real render, twice
    clips._words(db, settings, recording, 0, 20_000)
    with db.session() as s:
        real = [m for m in s.scalars(select(SystemEvent.message).where(
            SystemEvent.event_type == "caption_words_suppressed")) if "t-" not in m]
    assert len(real) == 1


@requires_ffmpeg
def test_review_reliability_uses_raw_times_under_a_correction(db, settings, recording):
    with db.session() as s:
        tr = transcripts.current(s, settings, recording.id)
        transcripts.add_correction(s, tr.id, 1500, validated=True, validation_ref="test")
    with db.session() as s:
        raw = analysis_evidence.window(s, settings, recording.id, 0, 20_000)
        cor = analysis_evidence.window(s, settings, recording.id, 1000, 20_000, corrected=True)
    by_raw = {w["text"]: w["reliability"]["class"] for w in raw["transcript"]["words"]}
    by_cor = {w["text"]: w["reliability"]["class"] for w in cor["transcript"]["words"]}
    assert by_cor and all(by_cor[t] == by_raw[t] for t in by_cor)
    assert cor["transcript"]["reliability"]["time_basis"] == "raw word times"
