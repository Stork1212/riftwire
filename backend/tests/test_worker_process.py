"""Crash-safety tests with real worker processes (ARCHITECTURE §7.8)."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Job, Recording, SystemEvent, Worker
from app.services import media
from tests.conftest import BACKEND_DIR, requires_ffmpeg
from tests.helpers import import_file

pytestmark = requires_ffmpeg


def start_worker(**env_overrides) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_overrides.items()})
    return subprocess.Popen([sys.executable, "-m", "app.workers.worker"], cwd=BACKEND_DIR,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def wait_for(pred, timeout=30.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def stop(proc: subprocess.Popen, sig=signal.SIGTERM, timeout=30) -> int:
    if proc.poll() is None:
        proc.send_signal(sig)
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise


def rec_status(db, rid):
    with db.session() as s:
        r = s.get(Recording, rid)
        return r.status, r.thumbnail_status


def all_done(db, rid):
    with db.session() as s:
        jobs = s.scalars(select(Job).where(Job.recording_id == rid)).all()
        return len(jobs) == 3 and all(j.status == "COMPLETED" for j in jobs)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- normal operation

def test_worker_processes_pipeline_and_stops_gracefully(db, settings, media_dir):
    rid = import_file(db, settings, media_dir / "basic.mp4").recording.id
    w = start_worker()
    try:
        wait_for(lambda: all_done(db, rid))
        assert rec_status(db, rid) == ("READY", "READY")
    finally:
        assert stop(w) == 0
    with db.session() as s:
        assert s.scalars(select(Worker.status)).all() == ["STOPPED"]


def test_second_worker_refused(db, settings):
    w1 = start_worker()
    try:
        with db.session():
            pass
        wait_for(lambda: (settings.data_dir / "worker.lock").exists())
        time.sleep(0.5)
        w2 = start_worker()
        assert w2.wait(30) == 3
    finally:
        stop(w1)


def test_worker_refuses_unmigrated_database(settings):
    w = start_worker()
    assert w.wait(30) == 2


def test_jobs_survive_restart(db, settings, media_dir):
    rid = import_file(db, settings, media_dir / "basic.mp4").recording.id
    # No worker running: job persisted as PENDING; start worker later.
    with db.session() as s:
        assert s.scalar(select(Job.status).where(Job.recording_id == rid)) == "PENDING"
    w = start_worker()
    try:
        wait_for(lambda: all_done(db, rid))
    finally:
        stop(w)


# ---------------------------------------------------------------- crash matrix (real kills)

@pytest.mark.parametrize("point", ["finalize_before_link", "finalize_after_link",
                                   "finalize_after_unlink"])
def test_hard_crash_during_finalize_recovers(db, settings, media_dir, point):
    res = import_file(db, settings, media_dir / "basic.mp4")
    rid, digest = res.recording.id, res.recording.sha256
    crashed = start_worker(RIFTWIRE_TEST_CRASH_AT=point)
    assert crashed.wait(60) == 137  # hard exit, no cleanup, lock released by OS
    w = start_worker()
    try:
        wait_for(lambda: all_done(db, rid))
    finally:
        stop(w)
    files = list(settings.recordings_dir.iterdir())
    assert [f.name for f in files] == [f"{rid}.mp4"]
    assert sha(files[0]) == digest
    assert [p.name for p in settings.temp_dir.iterdir()] == []
    assert rec_status(db, rid) == ("READY", "READY")
    with db.session() as s:
        ingest = s.scalar(select(Job).where(Job.recording_id == rid, Job.job_type == "INGEST"))
        assert ingest.attempt_count == 2  # crashed attempt + recovered attempt


def test_crash_between_stage_and_commit_leaves_only_temp(db, settings, media_dir):
    """API process dies after validating/staging but before the DB insert."""
    code = (
        "import sys; from app.core.config import get_settings; "
        "from app.db.session import Database; from tests.helpers import import_file; "
        "from pathlib import Path; s=get_settings(); "
        "import_file(Database(s.database_url), s, Path(sys.argv[1]))"
    )
    env = dict(os.environ, RIFTWIRE_TEST_CRASH_AT="after_stage_rename")
    p = subprocess.run([sys.executable, "-c", code, str(media_dir / "basic.mp4")],
                       cwd=BACKEND_DIR, env=env, capture_output=True)
    assert p.returncode == 137
    staged = [x.name for x in settings.temp_dir.iterdir()]
    assert len(staged) == 1 and staged[0].endswith(".staged")
    with db.session() as s:
        assert s.scalars(select(Recording)).all() == []
    # Startup reconciler removes it once stale; nothing in recordings/ is touched.
    from app.services.ingest import reconcile
    rep = reconcile(db, settings, "api", now_ts=time.time() + 10**6)
    assert rep.removed_temp == staged
    # Re-import works (no false duplicate).
    rid = import_file(db, settings, media_dir / "basic.mp4").recording.id
    assert rid


# ---------------------------------------------------------------- orphaned FFmpeg

def test_sigkilled_worker_orphan_ffmpeg_is_killed_before_retry(db, settings, media_dir):
    rid = import_file(db, settings, media_dir / "basic.mp4").recording.id
    w = start_worker(RIFTWIRE_TEST_SLOW_FFMPEG_SECONDS=60)

    def thumb_child():
        with db.session() as s:
            j = s.scalar(select(Job).where(Job.recording_id == rid,
                                           Job.job_type == "THUMBNAIL"))
            return (j.process_pid, j.process_started_at) if j and j.process_pid else None

    pid, started = wait_for(thumb_child, timeout=60)
    w.send_signal(signal.SIGKILL)
    w.wait(10)
    time.sleep(0.5)
    assert media.pid_alive(pid), "FFmpeg should survive its parent (own process group)"
    assert media.process_start_signature(pid) == started

    w2 = start_worker()
    try:
        wait_for(lambda: all_done(db, rid), timeout=60)
        assert not media.pid_alive(pid), "orphan must be killed by the recovering worker"
    finally:
        stop(w2)
        if media.pid_alive(pid):
            media.kill_process_group(pid)
    with db.session() as s:
        msgs = s.scalars(select(SystemEvent.message)
                         .where(SystemEvent.event_type == "lease_recovered")).all()
    assert any("orphan process: killed" in m for m in msgs)


def test_pid_reuse_guard(tmp_path):
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert media.kill_orphan_if_matches(p.pid, "Thu Jan  1 00:00:00 1970") == "pid_reused"
        assert media.pid_alive(p.pid)  # unrelated process NOT killed
        sig = media.process_start_signature(p.pid)
        # Right start time but wrong program name → still not killed.
        assert media.kill_orphan_if_matches(p.pid, sig) == "pid_reused"
        assert media.kill_orphan_if_matches(p.pid, sig, allowed_names=("sleep",)) == "killed"
        p.wait(5)
    finally:
        if p.poll() is None:
            p.kill()
    assert media.kill_orphan_if_matches(p.pid, None) == "not_running"


def test_graceful_shutdown_releases_running_job(db, settings, media_dir, monkeypatch):
    rid = import_file(db, settings, media_dir / "basic.mp4").recording.id
    w = start_worker(RIFTWIRE_TEST_SLOW_FFMPEG_SECONDS=60,
                     RIFTWIRE_WORKER_SHUTDOWN_GRACE_SECONDS=1)

    def thumb_running():
        with db.session() as s:
            return s.scalar(select(Job.process_pid).where(
                Job.recording_id == rid, Job.job_type == "THUMBNAIL"))

    pid = wait_for(thumb_running, timeout=60)
    assert stop(w, signal.SIGTERM) == 0
    assert not media.pid_alive(pid)
    with db.session() as s:
        j = s.scalar(select(Job).where(Job.recording_id == rid, Job.job_type == "THUMBNAIL"))
        assert j.status == "RETRY_WAIT" and j.lease_token is None
