"""M4 shared transcripts: chunked, resumable, deterministic, immutable, shared by consumers.

Real FFmpeg audio extraction, real child processes and real chunk offsets; the engine is
``tests/m4_stub_whisper.py``, which decodes tone-coded words from the audio itself (no MLX,
no model download). Every assertion compares against the generated ground truth."""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import (
    Job,
    JobType,
    Transcript,
    TranscriptChunk,
    TranscriptSegment,
    TranscriptWord,
)
from app.services import acquisition, clips, highlight_scan, queue, transcripts
from app.workers.worker import WorkerProcess
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.m4_fixtures import plan_words, tone_word_audio
from tests.test_ingest import drain
from tests.test_rights import World

STUB = Path(__file__).with_name("m4_stub_whisper.py")
TOLERANCE_MS = 40
DURATION_S = 24.0


@pytest.fixture
def engine(settings, tmp_path, monkeypatch):
    """Local "Whisper": the stub child, a model folder with the two checksummed files,
    10 s chunks with 2 s overlap (three chunks for the 24 s recording)."""
    model = tmp_path / "whisper-base-mlx"
    model.mkdir()
    (model / "config.json").write_text('{"n_mels": 80}')
    (model / "weights.npz").write_bytes(b"stub weights v1")
    log = tmp_path / "whisper-calls.jsonl"
    monkeypatch.setattr(settings, "clip_whisper_python", Path(sys.executable))
    monkeypatch.setattr(settings, "clip_whisper_model", model)
    monkeypatch.setattr(settings, "transcribe_chunk_seconds", 10)
    monkeypatch.setattr(settings, "transcribe_overlap_seconds", 2)
    monkeypatch.setattr(transcripts, "CHILD", STUB)
    monkeypatch.setenv("RIFTWIRE_STUB_WHISPER_LOG", str(log))
    monkeypatch.delenv("RIFTWIRE_STUB_WHISPER_FAIL_AFTER", raising=False)
    transcripts._checksums.clear()

    class Engine:
        model_dir = model
        log_path = log

        @staticmethod
        def calls() -> int:
            return sum(1 for _ in log.open()) if log.exists() else 0

    return Engine


@pytest.fixture
def spoken(db, settings, tmp_path):
    """A READY, rights-cleared recording whose audio carries 50 tone-coded words, some
    straddling the 10 s and 20 s chunk boundaries."""
    words = plan_words(50, start_s=0.5)
    wav = tone_word_audio(tmp_path / "speech.wav", words, DURATION_S)
    mp4 = tmp_path / "speech.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=320x240:rate=30:duration={DURATION_S}", "-i", str(wav),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
                    "-c:a", "aac", "-b:a", "128k", "-shortest", str(mp4)],
                   check=True, timeout=120)
    rec = import_file(db, settings, mp4).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)
    return rec, words


def _transcribe(db, settings) -> None:
    drain(db, settings)


def _stored_words(db, settings, rec_id):
    with db.session() as s:
        tr = transcripts.current(s, settings, rec_id)
        assert tr is not None
        return tr.id, transcripts.words(s, tr.id)


# ------------------------------------------------------------------------- pure merge
def _plan_raw():
    plans = transcripts.plan_chunks(30_000, 10_000, 2_000)
    raw = {0: [("a", 9_500, 9_900, 0.9), ("b", 11_000, 11_300, 0.6)],     # b: chunk 1's
           1: [("a", 1_500, 1_900, 0.8), ("b", 3_000, 3_300, 0.7),        # (offset 8 s)
               ("c", 11_900, 12_200, 0.9)],
           2: [("c", 1_900, 2_200, 0.4), ("d", 5_000, 5_400, 0.9)]}       # (offset 18 s)
    return [(p, {"words": [{"w": t, "start_ms": a, "end_ms": b, "prob": pr, "seg": 0}
                           for t, a, b, pr in raw[p.idx]]}) for p in plans]


def test_chunk_plan_covers_the_recording_with_overlap():
    plans = transcripts.plan_chunks(25_000, 10_000, 2_000)
    assert [(p.start_ms, p.end_ms, p.core_start_ms, p.core_end_ms) for p in plans] == [
        (0, 12_000, 0, 10_000), (8_000, 22_000, 10_000, 20_000),
        (18_000, 25_000, 20_000, 25_000)]
    assert transcripts.plan_chunks(0, 10_000, 2_000) == []


def test_merge_keeps_each_word_once_and_is_order_independent():
    chunks = _plan_raw()
    words, segments = transcripts.merge(chunks, 30_000)
    assert [(w["text"], w["start_ms"], w["chunk_idx"]) for w in words] == [
        ("a", 9_500, 0), ("b", 11_000, 1), ("c", 19_900, 1), ("d", 23_000, 2)]
    # 10 s: comparable quality, the core edge stands. 20 s: chunk 1's "c" (0.9) is clearly
    # better than chunk 2's (0.4), so chunk 1 owns that overlap zone (merge v2).
    for _ in range(5):
        shuffled = chunks[:]
        random.shuffle(shuffled)
        assert transcripts.merge(shuffled, 30_000) == (words, segments)
    assert [(s["first_word_idx"], s["last_word_idx"]) for s in segments] == [
        (0, 0), (1, 2), (3, 3)]


def test_merge_deduplicates_the_same_word_seen_by_two_chunks():
    plans = transcripts.plan_chunks(20_000, 10_000, 2_000)
    # The same word, reported by both chunks with midpoints just either side of 10 s.
    a = {"words": [{"w": "Wow!", "start_ms": 9_800, "end_ms": 10_190, "prob": 0.7}]}
    b = {"words": [{"w": "wow", "start_ms": 1_820, "end_ms": 2_200, "prob": 0.9}]}
    words, _ = transcripts.merge([(plans[0], a), (plans[1], b)], 20_000)
    assert [(w["text"], w["prob"], w["chunk_idx"]) for w in words] == [("wow", 0.9, 1)]


def _native_failure():
    """The M4 native run on 2026-11-25 (macOS speech, real mlx-whisper base, 150 s, 120 s
    chunks, 4 s overlap). Chunk 0 (0-124 s) repeated the FIRST phrase over the boundary
    phrase with near-zero probabilities and collapsed timestamps; chunk 1 (from 116 s)
    heard the boundary phrase correctly. Chunk-relative times; chunk 0's values are the
    stored ones from that run, chunk 1's reconstruct its correct words."""
    first = [("The", 4600, 5140, .89), ("police", 5140, 5420, .93),
             ("helicopter", 5420, 6000, 1.0), ("is", 6000, 6280, 1.0),
             ("right", 6280, 6420, 1.0), ("above", 6420, 6680, 1.0), ("us.", 6680, 7000, 1.0),
             ("Take", 7200, 7320, .99), ("the", 7320, 7520, 1.0), ("next", 7520, 7740, 1.0),
             ("exit", 7740, 7960, 1.0), ("now.", 7960, 8300, .92)]
    repeat = [("The", 118440, 119840, .0118), ("police", 119840, 119980, 0.0)] + [
        (t, 119980, 119980, p) for t, p in (("helicopter", 0.0), ("is", .1265),
                                            ("right", .0035), ("above", .0087), ("us.", .1432),
                                            ("Take", .0018), ("the", .1661), ("next", .0019),
                                            ("exit", .0023), ("now.", .0016))]
    boundary = [("Seven", 1500, 1900, .97), ("orange", 1900, 2300, .95),
                ("taxis", 2300, 2800, .93), ("crashed", 2800, 3300, .96),
                ("into", 3300, 3500, .99), ("the", 3500, 3640, .99),
                ("fountain", 3940, 4260, .998), ("at", 4260, 4560, .997),
                ("the", 4560, 4720, .994), ("harbor.", 4720, 4980, .889)]
    mission = [("Mission", 21080, 22400, .74), ("complete", 22400, 22860, .91)]

    def raw(ws):
        return {"words": [{"w": t, "start_ms": a, "end_ms": b, "prob": p, "seg": 0}
                          for t, a, b, p in ws]}
    plans = transcripts.plan_chunks(150_000, 120_000, 4_000)
    return [(plans[0], raw(first + repeat)), (plans[1], raw(boundary + mission))]


def test_merge_v2_gives_the_overlap_to_the_chunk_with_real_words():
    chunks = _native_failure()
    [decision] = transcripts.boundaries(chunks, 150_000)
    assert decision["owner"] == "later" and decision["cut_ms"] == 116_000
    assert decision["quality"][0] < 0.1 < 0.9 < decision["quality"][1]
    words, _ = transcripts.merge(chunks, 150_000)
    text = " ".join(w["text"] for w in words)
    assert text == ("The police helicopter is right above us. Take the next exit now. "
                    "Seven orange taxis crashed into the fountain at the harbor. "
                    "Mission complete")
    assert words[12]["start_ms"] == 117_500 and words[12]["chunk_idx"] == 1
    for _ in range(3):                                   # still order-independent
        assert transcripts.merge(list(reversed(chunks)), 150_000)[0] == words


def test_merge_v2_keeps_the_nominal_boundary_when_quality_is_comparable_or_one_side_silent():
    plans = transcripts.plan_chunks(20_000, 10_000, 2_000)
    good = {"words": [{"w": "go", "start_ms": 1_200, "end_ms": 1_500, "prob": 0.9}]}
    also = {"words": [{"w": "go", "start_ms": 9_200, "end_ms": 9_500, "prob": 0.8}]}
    [d] = transcripts.boundaries([(plans[0], also), (plans[1], good)], 20_000)
    assert d["owner"] == "nominal" and d["cut_ms"] == 10_000
    [d] = transcripts.boundaries([(plans[0], {"words": []}), (plans[1], good)], 20_000)
    assert d["owner"] == "nominal" and d["quality"] == [None, 0.9]


def test_fingerprint_changes_with_the_model_and_the_chunking(settings, engine):
    base = transcripts.current_config(settings)
    assert len(base["model_sha256"]) == 64
    same = transcripts.current_config(settings)
    assert transcripts.fingerprint(base) == transcripts.fingerprint(same)
    (engine.model_dir / "weights.npz").write_bytes(b"stub weights v2 (different size)")
    assert transcripts.current_config(settings)["model_sha256"] != base["model_sha256"]
    assert base["decode"] == {"condition_on_previous_text": False}
    assert base["merge_version"] == 2
    settings.transcribe_chunk_seconds = 20
    assert transcripts.fingerprint(transcripts.current_config(settings)) != \
        transcripts.fingerprint(base)


# ---------------------------------------------------------------------- the TRANSCRIBE job
@requires_ffmpeg
def test_transcript_matches_the_audio_across_chunk_boundaries(db, settings, engine, spoken):
    rec, truth = spoken
    with db.session() as s:
        assert transcripts.request(s, settings, rec.id) is not None
    _transcribe(db, settings)
    tr_id, got = _stored_words(db, settings, rec.id)
    assert [w["w"] for w in got] == [t.text for t in truth]          # none lost or doubled
    for w, t in zip(got, truth, strict=True):
        assert abs(w["start_ms"] - t.start_ms) <= TOLERANCE_MS, (w, t)
        assert abs(w["end_ms"] - t.end_ms) <= TOLERANCE_MS, (w, t)
    with db.session() as s:
        tr = s.get(Transcript, tr_id)
        assert (tr.status, tr.word_count, tr.coverage_ms) == ("COMPLETE", 50, tr.duration_ms)
        assert tr.engine_version == "stub-tone-decoder 1"
        assert tr.recording_sha256 == rec.sha256
        chunks = s.scalars(select(TranscriptChunk).where(
            TranscriptChunk.transcript_id == tr_id).order_by(TranscriptChunk.idx)).all()
        assert len(chunks) == 3 and all(c.status == "DONE" and c.audio_sha256 for c in chunks)
        raw = json.loads(gzip.decompress(chunks[1].raw_json_gz))
        assert raw["engine_version"] == "stub-tone-decoder 1"           # raw output kept
        assert s.scalar(select(func.count()).select_from(TranscriptSegment).where(
            TranscriptSegment.transcript_id == tr_id)) >= 1
    assert engine.calls() == 3
    calls = [json.loads(line) for line in engine.log_path.read_text().splitlines()]
    assert all(c["options"] == {"condition_on_previous_text": False} for c in calls)


@requires_ffmpeg
def test_completed_transcript_is_never_produced_twice(db, settings, engine, spoken):
    rec, _ = spoken
    with db.session() as s:
        transcripts.request(s, settings, rec.id)
    _transcribe(db, settings)
    with db.session() as s:
        assert transcripts.request(s, settings, rec.id) is None
        job = s.scalar(select(Job).where(Job.job_type == JobType.TRANSCRIBE.value))
        assert job.status == "COMPLETED"
        queue.enqueue(s, rec.id, JobType.TRANSCRIBE, 3)                 # idempotent enqueue
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.job_type == JobType.TRANSCRIBE.value)) == 1
    _transcribe(db, settings)
    assert engine.calls() == 3
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Transcript)) == 1


@requires_ffmpeg
def test_a_crash_resumes_at_the_first_unfinished_chunk(db, settings, engine, spoken,
                                                       monkeypatch):
    rec, truth = spoken
    with db.session() as s:
        transcripts.request(s, settings, rec.id)
    monkeypatch.setenv("RIFTWIRE_STUB_WHISPER_FAIL_AFTER", "1")      # dies on chunk 1
    worker = WorkerProcess(settings, db)
    claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
    worker._execute(claimed)
    with db.session() as s:
        job = s.get(Job, claimed.id)
        assert job.status == "RETRY_WAIT" and "chunk 1" in job.last_error
        assert [c.status for c in s.scalars(select(TranscriptChunk).order_by(
            TranscriptChunk.idx))] == ["DONE", "PENDING", "PENDING"]
    assert engine.calls() == 2
    monkeypatch.delenv("RIFTWIRE_STUB_WHISPER_FAIL_AFTER")
    _transcribe(db, settings)
    assert engine.calls() == 4                     # chunks 1 and 2 only; chunk 0 kept
    _, got = _stored_words(db, settings, rec.id)
    assert [w["w"] for w in got] == [t.text for t in truth]


@requires_ffmpeg
def test_raw_words_are_immutable_and_corrections_are_a_separate_layer(db, settings, engine,
                                                                       spoken):
    rec, _ = spoken
    with db.session() as s:
        transcripts.request(s, settings, rec.id)
    _transcribe(db, settings)
    tr_id, raw = _stored_words(db, settings, rec.id)
    conn = sqlite3.connect(settings.db_path)
    for sql in ("UPDATE transcript_words SET start_ms = start_ms + 1",
                "DELETE FROM transcript_words",
                "UPDATE transcript_segments SET text = 'x'",
                "UPDATE transcript_chunks SET raw_json_gz = x'00'",
                "DELETE FROM transcript_chunks",
                "UPDATE transcripts SET word_count = 0"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()
    with db.session() as s:
        with pytest.raises(ConflictError):
            transcripts.words(s, tr_id, corrected=True)          # nothing validated yet
        transcripts.add_correction(s, tr_id, -120)                # unvalidated: not applied
    with db.session() as s:
        with pytest.raises(ConflictError):
            transcripts.words(s, tr_id, corrected=True)
        transcripts.add_correction(s, tr_id, -80, validated=True, validation_ref="M4-test")
    with db.session() as s:
        shifted = transcripts.words(s, tr_id, corrected=True)
        assert [w["start_ms"] for w in shifted] == [w["start_ms"] - 80 for w in raw]
        assert transcripts.words(s, tr_id) == raw                 # raw unchanged
        conn = sqlite3.connect(settings.db_path)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE transcript_corrections SET params_json = '{}'")
        conn.close()


@requires_ffmpeg
def test_a_new_configuration_writes_a_new_transcript_beside_the_old(db, settings, engine,
                                                                    spoken):
    rec, _ = spoken
    with db.session() as s:
        transcripts.request(s, settings, rec.id)
    _transcribe(db, settings)
    old_id, old = _stored_words(db, settings, rec.id)
    settings.transcribe_chunk_seconds = 15
    with db.session() as s:
        assert transcripts.current(s, settings, rec.id) is None
        job = transcripts.request(s, settings, rec.id)
        assert job.status == "PENDING"                            # explicit regeneration
    _transcribe(db, settings)
    new_id, new = _stored_words(db, settings, rec.id)
    assert new_id != old_id and [w["w"] for w in new] == [w["w"] for w in old]
    with db.session() as s:
        assert transcripts.words(s, old_id) == old                # old one kept, unchanged
        assert s.scalar(select(func.count()).select_from(Transcript)) == 2


@requires_ffmpeg
def test_silent_recording_gets_an_empty_complete_transcript(db, settings, engine, media_dir):
    rec = import_file(db, settings, media_dir / "noaudio.mp4").recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)
        transcripts.request(s, settings, rec.id)
    _transcribe(db, settings)
    with db.session() as s:
        tr = transcripts.current(s, settings, rec.id)
        assert (tr.word_count, tr.engine_version) == (0, "no-audio")
    assert engine.calls() == 0


# ------------------------------------------------------------------------- consumers
@requires_ffmpeg
def test_scan_and_clip_captions_share_one_transcript(db, settings, engine, spoken,
                                                      monkeypatch):
    rec, truth = spoken
    seen: dict = {}
    real = highlight_scan.scan_signals

    def scan(series, words, duration_ms):
        seen["words"] = words
        return real(series, words, duration_ms)

    monkeypatch.setattr(highlight_scan, "scan_signals", scan)
    with db.session() as s:
        queue.enqueue(s, rec.id, JobType.HIGHLIGHT_SCAN, 3)
    worker = WorkerProcess(settings, db)
    claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
    worker._execute(claimed)                                      # waits for the transcript
    with db.session() as s:
        job = s.get(Job, claimed.id)
        assert job.status == "RETRY_WAIT" and job.attempt_count == 0
        assert "dependency" in job.last_error
    with pytest.raises(ConflictError) as pending:                 # captions wait too
        clips._words(db, settings, rec, 0, 5_000)
    assert pending.value.code == "transcript_pending"
    drain(db, settings)
    with db.session() as s:
        scan_job = s.get(Job, claimed.id)
        assert scan_job.status == "COMPLETED", scan_job.last_error
        tr = transcripts.current(s, settings, rec.id)
        result = json.loads(scan_job.result_json)
        assert result["transcript_id"] == tr.id
        assert result["motion_source"] == "visual_signals"
        assert set(result["signal_run_ids"]) == {"audio", "visual"}
    assert [w["w"] for w in seen["words"]] == [t.text for t in truth]
    caption_words, tr_id = clips._words(db, settings, rec, 10_000, 4_000)
    assert tr_id == tr.id
    inside = [t for t in truth if 10_000 <= (t.start_ms + t.end_ms) / 2 < 14_000]
    assert [w["w"] for w in caption_words] == [t.text for t in inside]
    for w, t in zip(caption_words, inside, strict=True):          # re-based to clip time
        assert abs(w["start_ms"] - (t.start_ms - 10_000)) <= TOLERANCE_MS
    data = clips.cut(db, settings, rec.id, 10_000, 14_000, subtitles=True)
    assert data[4:8] == b"ftyp" and b"tx3g" in data
    assert inside[0].text.encode() in data                       # caption text = stored words
    with db.session() as s:
        from app.models import SystemEvent

        exported = s.scalars(select(SystemEvent).where(
            SystemEvent.event_type == "clip_exported")).all()
        assert len(exported) == 1 and tr.id in exported[0].message
    assert engine.calls() == 3                                    # one transcription, total


# ------------------------------------------------------------------------- migration
def test_downgrade_refuses_while_transcripts_exist(db, settings, engine, tmp_path):
    with db.session() as s:
        cfg = transcripts.ensure_config(s, transcripts.current_config(settings))
        cfg_id = cfg.id
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO transcripts (id, recording_id, config_id, recording_sha256, "
                 "status, duration_ms, word_count, coverage_ms, created_at) VALUES "
                 "('t1', 'r1', ?, 'x', 'PENDING', 1000, 0, 0, '2026-11-25 00:00:00')",
                 (cfg_id,))
    conn.commit()
    conn.close()
    db.dispose()
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    with pytest.raises(Exception, match="transcript"):
        command.downgrade(alembic_cfg, "0009")
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0012",)]
    assert conn.execute("SELECT count(*) FROM transcripts").fetchone() == (1,)
    conn.close()


def test_empty_database_migrates_down_and_up_cleanly(db, settings):
    db.dispose()
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.downgrade(alembic_cfg, "0009")
    conn = sqlite3.connect(settings.db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert not {n for n in names if n.startswith("transcript")}
    command.upgrade(alembic_cfg, "head")
    conn = sqlite3.connect(settings.db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"transcripts", "transcript_words", "transcript_chunks", "transcript_segments",
            "transcript_corrections", "transcription_configs"} <= names
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_transcript_word_model_is_exported():
    assert TranscriptWord.__tablename__ == "transcript_words"


# ----------------------------------------------------------------------------- API
@requires_ffmpeg
def test_transcript_api_requests_reports_and_serves_raw_words(db, settings, engine, spoken):
    from fastapi.testclient import TestClient

    from app.main import create_app

    rec, truth = spoken
    url = f"/api/v1/recordings/{rec.id}/transcript"
    with TestClient(create_app(settings)) as client:
        before = client.get(url).json()
        assert before["transcript"] is None and before["job"] is None
        assert before["engine"]["available"] and before["engine"]["model_id"]
        queued = client.post(url)
        assert queued.status_code == 200, queued.text
        assert queued.json()["queued"] is True
        assert client.post(url).json()["job"]["id"] == queued.json()["job"]["id"]
        drain(db, settings)
        done = client.get(url, params={"words": "true", "start_ms": 10_000,
                                       "end_ms": 14_000}).json()
        assert done["transcript"]["status"] == "COMPLETE"
        assert done["transcript"]["timestamps"] == "raw"
        inside = [t.text for t in truth if 10_000 <= (t.start_ms + t.end_ms) / 2 < 14_000]
        assert [w["w"] for w in done["words"]] == inside
        assert client.post(url).json() == {"queued": False, "job": None}
        assert client.get(f"/api/v1/recordings/{'0' * 36}/transcript").status_code == 404


@requires_ffmpeg
def test_transcription_needs_analyze_rights(db, settings, engine, media_dir):
    from fastapi.testclient import TestClient

    from app.main import create_app

    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    drain(db, settings)
    url = f"/api/v1/recordings/{rec.id}/transcript"
    with TestClient(create_app(settings)) as client:
        assert client.get(url).status_code == 403          # no rights record attached yet
        assert client.post(url).status_code == 403
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.job_type == JobType.TRANSCRIBE.value)) == 0
    assert engine.calls() == 0


def test_only_the_transcribe_job_launches_whisper():
    """Plan §8: no application module may run Whisper except TRANSCRIBE."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(app_dir).as_posix()
        if rel in ("services/transcripts.py", "workers/whisper_child.py"):
            continue
        for needle in ("transcribe_child.py", "whisper_child", "mlx_whisper",
                       "transcripts.CHILD"):
            if needle in text:
                offenders.append(f"{rel}: {needle}")
    assert offenders == []
