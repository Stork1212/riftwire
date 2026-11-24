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
from tests.helpers import import_file, user_files

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
    assert user_files(settings.recordings_dir) == [f"{rid}.mp4"]
    assert sha(settings.recordings_dir / f"{rid}.mp4") == digest
    assert user_files(settings.temp_dir) == []
    assert rec_status(db, rid) == ("READY", "READY")
    with db.session() as s:
        ingest = s.scalar(select(Job).where(Job.recording_id == rid, Job.job_type == "INGEST"))
        assert ingest.attempt_count == 2  # crashed attempt + recovered attempt


@pytest.mark.parametrize("point", ["transfer_copy_midway", "transfer_before_verify",
                                   "transfer_verified", "transfer_after_promote",
                                   "finalize_after_promote"])
def test_hard_crash_during_copy_fallback_recovers(db, settings, media_dir, point):
    """Cross-volume finalize (temp and recordings on different drives) uses the verified
    staged transfer. A hard crash at any point must never leave a partial file at the
    final path and must recover."""
    res = import_file(db, settings, media_dir / "basic.mp4")
    rid, digest = res.recording.id, res.recording.sha256
    crashed = start_worker(RIFTWIRE_TEST_FORCE_COPY_FALLBACK=1, RIFTWIRE_TEST_CRASH_AT=point)
    assert crashed.wait(60) == 137
    final = settings.recordings_dir / f"{rid}.mp4"
    if final.exists():  # only possible once promoted, and then it must be complete
        assert point in ("transfer_after_promote", "finalize_after_promote")
        assert sha(final) == digest
    w = start_worker(RIFTWIRE_TEST_FORCE_COPY_FALLBACK=1)
    try:
        wait_for(lambda: all_done(db, rid))
    finally:
        stop(w)
    files = user_files(settings.recordings_dir, files_only=True)
    assert files == [f"{rid}.mp4"] and sha(final) == digest
    staging = settings.recordings_dir / ".staging"
    leftovers = [p.name for p in staging.iterdir()] if staging.is_dir() else []
    assert not [n for n in leftovers if n.endswith(".partial")], leftovers
    assert user_files(settings.temp_dir) == []
    assert rec_status(db, rid) == ("READY", "READY")


def test_crash_between_stage_and_commit_leaves_only_temp(db, settings, media_dir):
    """API process dies after validating/staging but before the DB insert."""
    code = (
        "import sys; from app.core.config import get_settings; "
        "from app.db.session import Database; from tests.helpers import import_file; "
        "from pathlib import Path; from app.services.locations import load_layout; "
        "s=get_settings(); d=Database(s.database_url); load_layout(d, s, 'test'); "
        "import_file(d, s, Path(sys.argv[1]))"
    )
    env = dict(os.environ, RIFTWIRE_TEST_CRASH_AT="after_stage_rename")
    p = subprocess.run([sys.executable, "-c", code, str(media_dir / "basic.mp4")],
                       cwd=BACKEND_DIR, env=env, capture_output=True)
    assert p.returncode == 137
    staged = user_files(settings.temp_dir)
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


def test_zombie_process_counts_as_dead(tmp_path):
    """Regression (macOS run of 599f910): an exited-but-unreaped child is a zombie; it must
    be reported as killed/not running, never as "kill_failed"."""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        sig = media.process_start_signature(p.pid)
        os.kill(p.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while not media.process_is_zombie(p.pid) and time.monotonic() < deadline:
            time.sleep(0.02)  # wait until it is a zombie (exited, not yet reaped)
        assert media.process_is_zombie(p.pid)
        assert media.pid_alive(p.pid)            # kill(pid, 0) still succeeds on a zombie
        assert not media.pid_running(p.pid)      # ...but it is not running
        assert media.kill_orphan_if_matches(p.pid, sig, allowed_names=("sleep",)) \
            in ("not_running", "killed")
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(5)


def test_kill_with_bsd_killpg_semantics(monkeypatch):
    """Emulates macOS/BSD: killpg() on a group whose only member is a zombie raises ESRCH.
    This is what made test_pid_reuse_guard fail on the Mac with the previous code."""
    real_killpg = os.killpg

    def bsd_killpg(pgid, sig):
        if media.process_is_zombie(pgid):
            raise ProcessLookupError(3, "No such process")
        return real_killpg(pgid, sig)

    monkeypatch.setattr(media.os, "killpg", bsd_killpg)
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        sig = media.process_start_signature(p.pid)
        assert media.kill_orphan_if_matches(p.pid, sig, allowed_names=("sleep",)) == "killed"
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(5)


def test_unkillable_detection_still_reports_live_process(monkeypatch):
    """kill_failed is still reported when the process genuinely survives SIGKILL."""
    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        sig = media.process_start_signature(p.pid)
        monkeypatch.setattr(media, "kill_process_group", lambda pid, grace=0: None)
        monkeypatch.setattr(media, "KILL_GRACE_SECONDS", 0.2)
        assert media.kill_orphan_if_matches(p.pid, sig, allowed_names=("sleep",)) \
            == "kill_failed"
    finally:
        p.kill()
        p.wait(5)


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


def test_final_heartbeat_never_overwrites_stopped(db, settings, monkeypatch):
    """Regression (smoke test, 2026-11-24): the heartbeat thread's last beat (STOPPING) could
    land after the shutdown's STOPPED write. Made deterministic by slowing that last beat."""
    import threading

    from app.workers.worker import WorkerProcess
    settings.worker_poll_seconds = 0.05
    settings.worker_heartbeat_seconds = 0.05
    w = WorkerProcess(settings, db)
    w.startup()
    real_beat = w._beat

    def slow_final_beat():
        if w.stop_event.is_set():
            time.sleep(0.5)
        real_beat()
    monkeypatch.setattr(w, "_beat", slow_final_beat)
    t = threading.Thread(target=w.run)
    t.start()
    time.sleep(0.3)
    w.request_stop()
    t.join(30)
    time.sleep(0.6)                      # any stray beat would have written by now
    with db.session() as s:
        assert s.get(Worker, w.id).status == "STOPPED"
