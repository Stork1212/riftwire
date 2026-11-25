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
    ("thanks", 3000, 3300, 0.92, "suspected_hallucination", "silence: confident word"),
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
    # Two independent measurements contradict the word: suppressed, never evidence.
    assert by["you"]["caption"] == "suppress" and not by["you"]["can_support"]
    assert by["thanks"]["caption"] == "suppress"          # confidence alone cannot rescue it
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
    silent = evidence_reliability.assess_words(_words()[:1], None, has_audio=False)
    assert "no audio track" in silent[0]["reasons"][0]


def test_profile_is_versioned_and_marks_the_probability_threshold_unvalidated():
    p = evidence_reliability.PROFILE
    assert p["version"] == 1 and p["low_prob_validated"] is False
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
    assert [w["w"] for w in words] == ["quietly", "whisper", "la", "drive", "go", "now"]
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
    assert counts["suspected_hallucination"] == 2 and counts["reliable"] == 3
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
        assert result["word_reliability"]["counts"]["suspected_hallucination"] == 2
        assert set(result["signal_run_ids"]) == {"audio", "visual"}
