"""M4 audio and visual signals and the analysis-evidence layer.

Generated footage only: ``marker_clip`` puts a one-frame white flash and a 100 ms 1 kHz beep
at known times, optionally with variable frame rate (two of every three frames dropped
through the middle of the clip), so frame index x fps does not give the time."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.errors import NotFoundError
from app.main import create_app
from app.models import Job, JobType, Keyframe, SignalKind, SignalRun
from app.services import acquisition, analysis_evidence, signals
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.m4_fixtures import marker_clip
from tests.test_ingest import drain
from tests.test_rights import World

MARKERS = [1.0, 4.5, 8.0]          # 4.5 s lies inside the VFR stretch (3 s to 6 s)
FRAME_MS = 34                      # one frame at 30 fps, rounded up
AUDIO_TOL_MS = 50


def _cleared(db, settings, path: Path):
    rec = import_file(db, settings, path).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)
    return rec


@pytest.fixture(params=[False, True], ids=["cfr", "vfr"])
def markers(request, db, settings, tmp_path):
    clip = marker_clip(tmp_path / f"markers-{request.param}.mp4", MARKERS, 10.0,
                       vfr=request.param)
    return _cleared(db, settings, clip), request.param


def _measure(db, settings, rec_id):
    with db.session() as s:
        for kind in SignalKind:
            signals.request(s, settings, rec_id, kind)
    drain(db, settings)
    with db.session() as s:
        audio = signals.current(s, rec_id, SignalKind.AUDIO)
        visual = signals.current(s, rec_id, SignalKind.VISUAL)
        assert audio is not None and visual is not None
        s.expunge_all()
    return audio, visual


# ------------------------------------------------------------------------ pure parts
def test_metadata_parser_reads_presentation_times_and_values():
    text = ("frame:0    pts:0       pts_time:0\nlavfi.scd.mafd=0.000\nlavfi.scd.score=0.000\n"
            "frame:7    pts:76800   pts_time:5\nlavfi.scd.mafd=99.6\nlavfi.scd.time=5\n")
    assert signals.parse_metadata(text) == [
        (0.0, {"scd.mafd": "0.000", "scd.score": "0.000"}),
        (5.0, {"scd.mafd": "99.6", "scd.time": "5"})]


def test_audio_analysis_separates_measured_and_inferred():
    frames = [(i / 10, {"astats.Overall.RMS_level": "-inf" if i < 20 or i > 24 else "-6",
                        "astats.Overall.Peak_level": "-3"}) for i in range(40)]
    series, events = signals.analyse_audio(frames, 4000)
    assert series["onsets_ms"] == [2000]
    assert series["silences_ms"] == [[0, 2000], [2500, 4000]]
    assert {(e["type"], e["kind"]) for e in events} == {("onset", "measured"),
                                                         ("loud_burst", "inferred")}
    burst = next(e for e in events if e["type"] == "loud_burst")
    assert "not a claim" in burst["note"] and burst["method"] and burst["thresholds"]


def test_keyframe_plan_is_capped_and_prefers_long_scenes():
    scenes = [{"idx": i, "start_ms": i * 1000, "end_ms": i * 1000 + (900 if i % 2 else 400),
               "duration_ms": 900 if i % 2 else 400} for i in range(10)]
    plan = signals.keyframe_plan(scenes, 10_000)
    assert len(plan) == signals.VISUAL_CONFIG["keyframes_min"]
    assert all(p["scene_idx"] % 2 for p in plan)
    assert [p["pts_ms"] for p in plan] == sorted(p["pts_ms"] for p in plan)


# ------------------------------------------------------------- against generated footage
@requires_ffmpeg
def test_signal_times_match_the_footage(db, settings, markers):
    rec, vfr = markers
    audio, visual = _measure(db, settings, rec.id)
    a, v = signals.series(audio), signals.series(visual)
    assert v["vfr_detected"] is vfr
    for m in MARKERS:
        t = int(m * 1000)
        assert any(abs(c - t) <= FRAME_MS for c in v["cuts_ms"]), (m, v["cuts_ms"])
        assert any(abs(o - t) <= AUDIO_TOL_MS for o in a["onsets_ms"]), (m, a["onsets_ms"])
    assert len(a["onsets_ms"]) == len(MARKERS)
    with db.session() as s:
        kfs = signals.run_keyframes(s, visual.id)
        assert kfs and len(kfs) <= len(v["scenes"])
        for k in kfs:                       # every keyframe sits inside its scene
            scene = v["scenes"][k.scene_idx]
            assert scene["start_ms"] <= k.pts_ms < scene["end_ms"]
            path = settings.thumbnails_dir / k.stored_filename
            assert path.read_bytes()[:3] == b"\xff\xd8\xff"
            assert hashlib.sha256(path.read_bytes()).hexdigest() == k.sha256
            assert k.width == 480


@requires_ffmpeg
def test_signals_are_measured_once_and_complete_runs_are_immutable(db, settings, media_dir,
                                                                     monkeypatch):
    rec = _cleared(db, settings, media_dir / "basic.mp4")
    audio, visual = _measure(db, settings, rec.id)
    with db.session() as s:
        assert signals.request(s, settings, rec.id, SignalKind.AUDIO) is None
        assert signals.request(s, settings, rec.id, SignalKind.VISUAL) is None
    drain(db, settings)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SignalRun)) == 2
    conn = sqlite3.connect(settings.db_path)
    for sql in ("UPDATE signal_runs SET events_json = '[]'", "DELETE FROM signal_runs",
                "UPDATE keyframes SET pts_ms = 0", "DELETE FROM keyframes"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()
    monkeypatch.setitem(signals.VISUAL_CONFIG, "scdet_threshold", 20.0)   # new config
    with db.session() as s:
        job = signals.request(s, settings, rec.id, SignalKind.VISUAL)
        assert job is not None and job.status == "PENDING"
    drain(db, settings)
    with db.session() as s:
        runs = s.scalars(select(SignalRun).where(SignalRun.kind == "visual")).all()
        assert len(runs) == 2 and all(r.status == "COMPLETE" for r in runs)
        assert s.get(SignalRun, visual.id).events_json == visual.events_json   # old kept


@requires_ffmpeg
def test_silent_recording_measures_visuals_only(db, settings, media_dir):
    rec = _cleared(db, settings, media_dir / "noaudio.mp4")
    with db.session() as s:
        assert signals.request_checked(s, settings, rec.id) == {
            "queued": {"audio": False, "visual": True}}
    drain(db, settings)
    with db.session() as s:
        assert signals.current(s, rec.id, SignalKind.VISUAL) is not None
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.job_type == JobType.AUDIO_SIGNALS.value)) == 0
        bundle = analysis_evidence.window(s, settings, rec.id, 0, 1000)
    assert bundle["audio"] == {"available": False, "reason": "no audio track"}
    assert bundle["transcript"] == {"available": False, "reason": "no audio track"}


# ------------------------------------------------------------------- evidence layer
@requires_ffmpeg
def test_evidence_window_is_bounded_traceable_and_stable(db, settings, tmp_path):
    rec = _cleared(db, settings, marker_clip(tmp_path / "m.mp4", MARKERS, 10.0))
    _measure(db, settings, rec.id)
    with db.session() as s:
        bundle = analysis_evidence.window(s, settings, rec.id, 3000, 9000, max_events=50)
        again = analysis_evidence.window(s, settings, rec.id, 3000, 9000, max_events=50)
        assert bundle["fingerprint"] == again["fingerprint"]
        assert bundle["schema"] == "analysis_evidence.v1"
        assert bundle["transcript"] == {"available": False, "reason": "not transcribed yet"}
        assert bundle["ocr"]["available"] is False
        assert bundle["window"]["source_start_ms"] == 3000
        assert all(e["end_ms"] >= 3000 and e["start_ms"] < 9000 for e in bundle["events"])
        assert {e["kind"] for e in bundle["events"]} <= {"measured", "inferred"}
        cuts = [e for e in bundle["events"] if e["type"] == "scene_cut"]
        assert cuts and all(any(abs(e["start_ms"] - m * 1000) <= 2 * FRAME_MS
                                for m in MARKERS) for e in cuts)
        for item in bundle["events"] + bundle["visual"]["scenes"] + bundle["visual"]["keyframes"]:
            resolved = analysis_evidence.resolve(s, item["id"])
            assert resolved["id"] == item["id"]
        assert len(bundle["visual"]["motion_mafd"]["values"]) <= 120
        with pytest.raises(NotFoundError):
            analysis_evidence.resolve(s, "v:not-a-run:0")
        small = analysis_evidence.window(s, settings, rec.id, 0, 10_000, max_events=2)
        assert len(small["events"]) == 2 and small["events_truncated"] is True


@requires_ffmpeg
def test_signals_and_evidence_need_analyze_rights(db, settings, media_dir):
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    drain(db, settings)
    with TestClient(create_app(settings)) as client:
        base = f"/api/v1/recordings/{rec.id}"
        assert client.post(f"{base}/signals").status_code == 403
        assert client.get(f"{base}/signals").status_code == 403
        assert client.get(f"{base}/evidence", params={"start_ms": 0, "end_ms": 1000}
                          ).status_code == 403
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.job_type.in_(["AUDIO_SIGNALS", "VISUAL_SIGNALS"]))) == 0


@requires_ffmpeg
def test_signals_api_and_keyframe_images(db, settings, tmp_path):
    rec = _cleared(db, settings, marker_clip(tmp_path / "m.mp4", MARKERS, 10.0))
    with TestClient(create_app(settings)) as client:
        base = f"/api/v1/recordings/{rec.id}"
        assert client.post(f"{base}/signals").json() == {
            "queued": {"audio": True, "visual": True}}
        drain(db, settings)
        state = client.get(f"{base}/signals").json()
        assert state["visual"]["run"]["scenes"] >= len(MARKERS)
        assert state["audio"]["run"]["measured_events"] == len(MARKERS)
        ev = client.get(f"{base}/evidence", params={"start_ms": 0, "end_ms": 10_000}).json()
        kf = ev["visual"]["keyframes"][0]["id"].removeprefix("kf:")
        image = client.get(f"{base}/keyframes/{kf}")
        assert image.status_code == 200 and image.content[:3] == b"\xff\xd8\xff"
        assert str(settings.thumbnails_dir) not in json.dumps(ev)
        assert client.get(f"/api/v1/recordings/{'0' * 36}/keyframes/{kf}").status_code == 404
        with db.session() as s:
            assert s.get(Keyframe, kf) is not None


# ------------------------------------------------------------------------- migration
def _alembic(settings):
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


@requires_ffmpeg
def test_downgrade_refuses_while_signal_runs_exist(db, settings, media_dir):
    from alembic import command

    rec = _cleared(db, settings, media_dir / "noaudio.mp4")
    with db.session() as s:
        signals.request(s, settings, rec.id, SignalKind.VISUAL)
    drain(db, settings)
    db.dispose()
    with pytest.raises(Exception, match="signal runs"):
        command.downgrade(_alembic(settings), "0010")
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0012",)]
    assert conn.execute("SELECT count(*) FROM signal_runs").fetchone() == (1,)
    conn.close()


def test_signal_migration_round_trips_on_an_empty_database(db, settings):
    from alembic import command

    db.dispose()
    command.downgrade(_alembic(settings), "0010")
    conn = sqlite3.connect(settings.db_path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert "signal_runs" not in names and "keyframes" not in names
    command.upgrade(_alembic(settings), "head")
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {"signal_runs", "keyframes"} <= {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master")}
    conn.close()
