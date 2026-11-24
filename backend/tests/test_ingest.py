from __future__ import annotations

import os
import stat
import time
import tracemalloc

import pytest
from sqlalchemy import func, select

from app.core.errors import (
    ConflictError,
    InsufficientStorage,
    PayloadTooLarge,
    UnsupportedMedia,
    ValidationFailed,
)
from app.models import Job, Recording, SystemEvent
from app.services import ingest
from app.services.ingest import UploadSession, reconcile
from app.workers.worker import WorkerProcess
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file

pytestmark = requires_ffmpeg


def drain(db, settings, max_jobs=20):
    """Run queued jobs in-process until the queue is empty."""
    from app.services import queue
    w = WorkerProcess(settings, db)
    for _ in range(max_jobs):
        c = queue.claim_next(db, w.id, settings.job_lease_seconds)
        if c is None:
            return
        w._execute(c)


def count(db, model):
    with db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def temp_files(settings):
    return sorted(p.name for p in settings.temp_dir.iterdir())


def get_rec(db, rid):
    with db.session() as s:
        return s.get(Recording, rid)


# ------------------------------------------------------------------ happy path

def test_import_stages_then_pipeline_completes(db, settings, media_dir):
    res = import_file(db, settings, media_dir / "basic.mp4", "My GTA clip.mp4")
    rec = res.recording
    assert rec.status == "STAGED"
    assert temp_files(settings) == [f"{rec.id}.staged"]
    assert list(settings.recordings_dir.iterdir()) == []  # API never writes recordings/

    drain(db, settings)

    rec = get_rec(db, rec.id)
    assert rec.status == "READY" and rec.thumbnail_status == "READY"
    assert rec.original_filename == "My GTA clip.mp4"
    assert rec.stored_filename == f"{rec.id}.mp4"
    assert rec.staging_filename is None
    final = settings.data_dir / rec.file_path
    assert final.exists() and temp_files(settings) == []
    assert not (final.stat().st_mode & stat.S_IWUSR)  # read-only after finalize
    assert (rec.width, rec.height, rec.fps_rational) == (320, 240, "30000/1001")
    assert rec.has_audio and rec.audio_codec == "aac" and rec.video_codec == "h264"
    assert (settings.thumbnails_dir / f"{rec.id}.jpg").exists()
    with db.session() as s:
        jobs = s.scalars(select(Job).where(Job.recording_id == rec.id)).all()
        assert sorted((j.job_type, j.status) for j in jobs) == [
            ("INGEST", "COMPLETED"), ("METADATA", "COMPLETED"), ("THUMBNAIL", "COMPLETED")]


@pytest.mark.parametrize("name", ["basic.mov", "basic.mkv", "basic.webm", "noaudio.mp4",
                                  "short.mp4", "vfr.mp4"])
def test_all_supported_formats(db, settings, media_dir, name):
    rec = import_file(db, settings, media_dir / name).recording
    drain(db, settings)
    rec = get_rec(db, rec.id)
    assert rec.status == "READY", rec.error_message
    assert rec.thumbnail_status == "READY"
    assert rec.stored_filename.endswith("." + name.rsplit(".", 1)[1])


def test_content_mismatched_extension_stored_by_content(db, settings, media_dir):
    import shutil
    src = settings.data_dir / "mislabeled.mp4"
    shutil.copy(media_dir / "basic.mkv", src)
    rec = import_file(db, settings, src).recording
    assert rec.stored_filename.endswith(".mkv")


# ------------------------------------------------------------------ rejection

def test_duplicate_detected(db, settings, media_dir):
    first = import_file(db, settings, media_dir / "basic.mp4").recording
    with pytest.raises(ConflictError) as e:
        import_file(db, settings, media_dir / "basic.mp4", "renamed.mp4")
    assert e.value.code == "duplicate_recording"
    assert e.value.details["existing_recording_id"] == first.id
    assert count(db, Recording) == 1
    assert temp_files(settings) == [f"{first.id}.staged"]


@pytest.mark.parametrize("name", ["garbage.mp4", "text.mp4", "image_as.mp4", "audio_only.mp4",
                                  "truncated.mp4"])
def test_invalid_media_rejected(db, settings, media_dir, name):
    with pytest.raises(ValidationFailed) as e:
        import_file(db, settings, media_dir / name)
    assert e.value.code == "invalid_media"
    assert count(db, Recording) == 0 and temp_files(settings) == []


def test_unsupported_extension(db, settings, media_dir):
    with pytest.raises(UnsupportedMedia):
        import_file(db, settings, media_dir / "image.png")
    assert temp_files(settings) == []


def test_upload_too_large(db, settings, media_dir):
    settings.max_upload_bytes = 1000
    with pytest.raises(PayloadTooLarge):
        import_file(db, settings, media_dir / "basic.mp4")
    assert temp_files(settings) == []


def test_insufficient_disk_space_up_front(db, settings, media_dir):
    settings.min_free_disk_bytes = 10**18
    with pytest.raises(InsufficientStorage) as e:
        import_file(db, settings, media_dir / "basic.mp4")
    assert e.value.status_code == 507
    assert temp_files(settings) == []


def test_disk_space_dropping_during_upload(db, settings, media_dir, monkeypatch):
    calls = {"n": 0}

    def fake_free(_):
        calls["n"] += 1
        return 10**12 if calls["n"] == 1 else 0

    monkeypatch.setattr(ingest, "free_bytes", fake_free)
    monkeypatch.setattr(ingest, "SPACE_CHECK_EVERY", 1024)
    settings.min_free_disk_bytes = 1
    with pytest.raises(InsufficientStorage):
        import_file(db, settings, media_dir / "basic.mp4", chunk=4096)
    assert temp_files(settings) == []


def test_incomplete_upload_rejected(db, settings, media_dir):
    up = UploadSession(settings, "x.mp4", 10_000)
    up.write(b"\0" * 100)
    with pytest.raises(ValidationFailed):
        up.finish()
    up.abort()
    assert temp_files(settings) == []


def test_interrupted_upload_cleans_part_file(settings):
    up = UploadSession(settings, "x.mp4", None)
    up.write(b"\0" * 4096)
    assert temp_files(settings) == [f"{up.import_id}.part"]
    up.abort()
    up.abort()  # idempotent
    assert temp_files(settings) == []
    assert ingest.RESERVATIONS.total() == 0


def test_path_traversal_filename_is_metadata_only(db, settings, media_dir):
    rec = import_file(db, settings, media_dir / "basic.mp4", "../../../../etc/evil.mp4").recording
    assert rec.original_filename == "evil.mp4"
    assert rec.stored_filename == f"{rec.id}.mp4"
    drain(db, settings)
    assert (settings.recordings_dir / rec.stored_filename).exists()


def test_large_upload_streams_with_bounded_memory(settings):
    """64 MiB streamed through the upload path must not be held in memory."""
    chunk = os.urandom(1024 * 1024)
    up = UploadSession(settings, "big.mp4", 64 * 1024 * 1024)
    tracemalloc.start()
    for _ in range(64):
        up.write(chunk)
    up.finish()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert (settings.temp_dir / f"{up.import_id}.part").stat().st_size == 64 * 1024 * 1024
    assert peak < 8 * 1024 * 1024, f"peak traced memory {peak} bytes"
    up.abort()


# ------------------------------------------------------------------ crash matrix (§6.4)

def _stage(db, settings, media_dir, name="basic.mp4"):
    rec = import_file(db, settings, media_dir / name).recording
    return rec, settings.temp_dir / rec.staging_filename, settings.data_dir / rec.file_path


def test_crash_after_link_before_unlink(db, settings, media_dir):
    rec, src, dst = _stage(db, settings, media_dir)
    os.link(src, dst)  # state left by a crash between link and unlink
    drain(db, settings)
    assert get_rec(db, rec.id).status == "READY"
    assert dst.exists() and not src.exists()


def test_crash_after_unlink_before_db_update(db, settings, media_dir):
    rec, src, dst = _stage(db, settings, media_dir)
    os.link(src, dst)
    src.unlink()
    drain(db, settings)
    assert get_rec(db, rec.id).status == "READY"


def test_both_files_missing_marks_failed_without_deleting(db, settings, media_dir):
    rec, src, dst = _stage(db, settings, media_dir)
    src.unlink()
    drain(db, settings)
    r = get_rec(db, rec.id)
    assert r.status == "FAILED" and "missing" in r.error_message
    with db.session() as s:
        j = s.scalar(select(Job).where(Job.recording_id == rec.id))
        assert j.status == "FAILED" and j.attempt_count == 1  # permanent: not retried


def test_never_overwrites_existing_final_file(db, settings, media_dir):
    rec, src, dst = _stage(db, settings, media_dir)
    dst.write_bytes(b"someone else's data")
    drain(db, settings)
    assert dst.read_bytes() == b"someone else's data"  # untouched
    assert src.exists()  # temp copy kept for investigation
    r = get_rec(db, rec.id)
    assert r.status == "FAILED" and "mismatch" in r.error_message


def test_corrupted_staged_file_detected(db, settings, media_dir):
    rec, src, dst = _stage(db, settings, media_dir)
    os.chmod(src, 0o644)
    with open(src, "r+b") as f:
        f.seek(100)
        f.write(b"XXXX")
    drain(db, settings)
    assert get_rec(db, rec.id).status == "FAILED"
    assert not dst.exists()


def test_reconcile_after_crash_before_commit(db, settings):
    staged = settings.temp_dir / "dead-import.staged"
    staged.write_bytes(b"x")
    fresh_part = settings.temp_dir / "inflight.part"
    fresh_part.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(staged, (old, old))
    rep = reconcile(db, settings, "test")
    assert rep.removed_temp == ["dead-import.staged"]
    assert fresh_part.exists()  # recent: may be an upload in progress


def test_reconcile_keeps_staged_files_with_rows(db, settings, media_dir):
    rec, src, _ = _stage(db, settings, media_dir)
    rep = reconcile(db, settings, "test", now_ts=time.time() + 10**6)
    assert rep.removed_temp == [] and src.exists()


def test_reconcile_reenqueues_staged_without_job(db, settings, media_dir):
    rec, _, _ = _stage(db, settings, media_dir)
    with db.session() as s:
        s.query(Job).delete()
    rep = reconcile(db, settings, "test")
    assert rep.reenqueued == [rec.id]
    drain(db, settings)
    assert get_rec(db, rec.id).status == "READY"


def test_reconcile_reports_but_keeps_orphan_recording(db, settings):
    orphan = settings.recordings_dir / "unknown.mp4"
    orphan.write_bytes(b"precious")
    rep = reconcile(db, settings, "test", now_ts=time.time() + 10**6)
    assert rep.orphan_files == ["unknown.mp4"] and orphan.exists()
    with db.session() as s:
        assert s.scalar(select(SystemEvent).where(
            SystemEvent.event_type == "orphan_recording_file")) is not None


# ------------------------------------------------------------------ review finding 2:
# crash-safe copy fallback for filesystems without hard links (PR #1 review)

import errno  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import threading  # noqa: E402

from app.services import queue  # noqa: E402
from app.services.ingest import STAGING_DIRNAME, promote_no_replace  # noqa: E402


def recordings_files(settings):
    return sorted(p.name for p in settings.recordings_dir.iterdir() if p.is_file())


def staging_files(settings):
    d = settings.recordings_dir / STAGING_DIRNAME
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def sha_file(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_copy_fallback_promotes_complete_verified_file(db, settings, media_dir):
    settings.test_force_copy_fallback = True
    rec, src, dst = _stage(db, settings, media_dir)
    drain(db, settings)
    r = get_rec(db, rec.id)
    assert r.status == "READY", r.error_message
    assert recordings_files(settings) == [dst.name] and sha_file(dst) == rec.sha256
    assert not (dst.stat().st_mode & stat.S_IWUSR)
    assert not src.exists()
    assert [n for n in staging_files(settings) if n.endswith(".partial")] == []
    reconcile(db, settings, "test")  # lock file no longer needed once not STAGED
    assert staging_files(settings) == []


def test_real_link_errno_triggers_fallback(db, settings, media_dir, monkeypatch):
    def no_links(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(ingest.os, "link", no_links)
    rec, src, dst = _stage(db, settings, media_dir)
    drain(db, settings)
    assert get_rec(db, rec.id).status == "READY" and sha_file(dst) == rec.sha256


def test_interrupted_copy_never_exposes_partial_final_file(db, settings, media_dir,
                                                           monkeypatch):
    """A copy interrupted mid-way leaves only an attempt-scoped staging file, never dst."""
    settings.test_force_copy_fallback = True
    rec, src, dst = _stage(db, settings, media_dir)

    class Interrupted(BaseException):
        pass

    def crash_midway(name):
        if name == "finalize_copy_midway":
            raise Interrupted  # BaseException: bypasses normal error handling, like a crash

    monkeypatch.setattr(ingest, "crashpoint", crash_midway)
    w = WorkerProcess(settings, db)
    c = queue.claim_next(db, w.id, settings.job_lease_seconds)
    from app.workers.handlers import JobContext, handle_ingest
    with pytest.raises(Interrupted):
        handle_ingest(JobContext(db, settings, c))
    assert not dst.exists()                   # final path never holds partial data
    assert src.exists()                       # original upload untouched
    monkeypatch.undo()
    settings.test_force_copy_fallback = True
    # Recovery: the dead attempt's lease is recovered, its staging file cleaned, retry copies.
    from datetime import timedelta

    from app.db.base import utcnow
    queue.recover_stuck_jobs(db, kill_orphan=lambda p, s: "not_running", backoff_base=0,
                             backoff_cap=0, now=utcnow() + timedelta(seconds=3600))
    reconcile(db, settings, "test")
    assert [n for n in staging_files(settings) if n.endswith(".partial")] == []
    drain(db, settings)
    assert get_rec(db, rec.id).status == "READY"
    assert recordings_files(settings) == [dst.name] and sha_file(dst) == rec.sha256


def test_crash_after_promote_before_temp_unlink(db, settings, media_dir):
    settings.test_force_copy_fallback = True
    rec, src, dst = _stage(db, settings, media_dir)
    dst.write_bytes(src.read_bytes())  # state left by a crash right after promotion
    drain(db, settings)
    assert get_rec(db, rec.id).status == "READY" and not src.exists()
    assert sha_file(dst) == rec.sha256


def test_promote_never_overwrites(tmp_path):
    staged, dst = tmp_path / "s.partial", tmp_path / "final.mp4"
    staged.write_bytes(b"new")
    dst.write_bytes(b"existing")
    ino = dst.stat().st_ino
    assert promote_no_replace(staged, dst, tmp_path / "x.lock") is False
    assert dst.read_bytes() == b"existing" and dst.stat().st_ino == ino
    assert staged.exists()  # caller removes its own staging file


def test_promote_is_serialized_with_other_attempts(tmp_path):
    """The existence check and rename happen under the lock, so an attempt that was
    waiting for the lock sees a destination created meanwhile and does not overwrite it."""
    staged, dst, lock = tmp_path / "s.partial", tmp_path / "final.mp4", tmp_path / "x.lock"
    staged.write_bytes(b"from zombie attempt")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)           # another attempt is promoting
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "r", promote_no_replace(staged, dst, lock)))
    t.start()
    t.join(0.5)
    assert t.is_alive()                       # blocked on the lock
    dst.write_bytes(b"promoted by the other attempt")
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    t.join(5)
    assert result["r"] is False and dst.read_bytes() == b"promoted by the other attempt"


def test_reconcile_keeps_staging_file_of_active_attempt(db, settings, media_dir):
    rec, _, _ = _stage(db, settings, media_dir)
    c = queue.claim_next(db, "w", 60)
    sdir = settings.recordings_dir / STAGING_DIRNAME
    sdir.mkdir()
    live = sdir / f"{rec.id}.{c.token}.partial"
    dead = sdir / f"{rec.id}.{'0' * 32}.partial"
    live.write_bytes(b"x")
    dead.write_bytes(b"x")
    reconcile(db, settings, "test")
    assert live.exists() and not dead.exists()


def test_reconcile_lists_files_before_reading_tokens(db, settings, media_dir, monkeypatch):
    """A staging file created by an attempt claimed *during* reconcile is never deleted."""
    rec, _, _ = _stage(db, settings, media_dir)
    sdir = settings.recordings_dir / STAGING_DIRNAME
    sdir.mkdir()
    real = ingest.active_lease_tokens
    created = {}

    def claim_during_reconcile(database):
        c = queue.claim_next(database, "w", 60)
        p = sdir / f"{rec.id}.{c.token}.partial"
        p.write_bytes(b"x")
        created["p"] = p
        return real(database)

    monkeypatch.setattr(ingest, "active_lease_tokens", claim_during_reconcile)
    reconcile(db, settings, "test")
    assert created["p"].exists()


def test_job_temp_outputs_cleaned_by_token_not_age(db, settings, media_dir):
    rec, _, _ = _stage(db, settings, media_dir)
    c = queue.claim_next(db, "w", 60)
    live = settings.temp_dir / f"job-{c.id}-{c.token}.tmp.jpg"
    dead = settings.temp_dir / f"job-{c.id}-{'f' * 32}.tmp.jpg"
    live.write_bytes(b"x")
    dead.write_bytes(b"x")
    reconcile(db, settings, "test")
    assert live.exists() and not dead.exists()
