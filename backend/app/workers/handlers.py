"""Job handlers. Each is idempotent and writes to the database only through fenced
transactions (via the JobContext / queue.complete)."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLarge,
    PermanentJobError,
    RightsRefused,
    RightsRefusedJobError,
    ServiceUnavailable,
    StorageOffline,
    StorageOfflineJobError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.db.session import Database
from app.models import Job, JobType, Recording, RecordingStatus, Severity
from app.services import media, queue, reservations, url_download
from app.services.acquisition import AcquisitionUnavailable
from app.services.events import record_event
from app.services.ingest import FinalizeError, finalize_recording
from app.services.locations import get_layout
from app.services.storage import safe_join

log = get_logger("handlers")

# Storage locations each job type reads or writes. While any is offline the job is not
# started (claim loop) and an attempt that loses its drive mid-way is deferred, not failed.
REQUIRED_KINDS: dict[JobType, tuple[str, ...]] = {
    JobType.INGEST: ("temp", "recordings"),
    JobType.METADATA: ("recordings",),
    # Thumbnails are written next to their final (internal) location, never via temp, so a
    # temp location on another drive can never make the final rename cross devices.
    JobType.THUMBNAIL: ("recordings",),
    # The download and the import's .part file are both on temp; INGEST then moves it.
    JobType.ACQUIRE: ("temp",),
}

THUMB_TMP_PREFIX = ".job-"           # thumbnails/.job-<job>-<token>.tmp.jpg
THUMB_RESERVE_BYTES = 4 * 1024 * 1024


def require_storage(ctx: JobContext) -> None:
    try:
        get_layout(ctx.settings).require_online(*REQUIRED_KINDS[ctx.claimed.job_type])
    except StorageOffline as e:
        raise StorageOfflineJobError(e.message) from e


class JobContext:
    """Everything a handler needs; enforces fencing for progress and child tracking."""

    def __init__(self, db: Database, settings: Settings, claimed: queue.ClaimedJob):
        self.db = db
        self.settings = settings
        self.claimed = claimed
        self.lost = threading.Event()      # lease lost (fence failed)
        self.cancel = threading.Event()    # shutdown requested
        self._last_progress = -1
        self._last_progress_ts = 0.0

    @property
    def job_id(self) -> str:
        return self.claimed.id

    def should_stop(self) -> bool:
        return self.lost.is_set() or self.cancel.is_set()

    def check(self) -> None:
        if self.lost.is_set():
            raise queue.LeaseLost(self.job_id)
        if self.cancel.is_set():
            raise media.CommandCancelled("shutdown")

    def progress(self, percent: int) -> None:
        now = time.monotonic()
        if percent == self._last_progress or (now - self._last_progress_ts < 0.5
                                              and percent < 100):
            return
        try:
            queue.set_progress(self.db, self.job_id, self.claimed.token, percent)
        except queue.LeaseLost:
            self.lost.set()
            raise
        self._last_progress, self._last_progress_ts = percent, now

    def _on_spawn(self, pid: int, started_at: str | None) -> None:
        try:
            queue.set_process(self.db, self.job_id, self.claimed.token, pid, started_at)
        except queue.LeaseLost:
            self.lost.set()  # run_command sees should_stop() and kills the group

    def run(self, args: list[str], timeout: float) -> media.CommandResult:
        try:
            return media.run_command(args, timeout, on_spawn=self._on_spawn,
                                     should_cancel=self.should_stop)
        finally:
            if not self.lost.is_set():
                try:
                    queue.set_process(self.db, self.job_id, self.claimed.token, None, None)
                except queue.LeaseLost:
                    self.lost.set()

    # Thin wrappers so handlers get fenced child tracking automatically.
    def probe(self, path: Path) -> media.MediaInfo:
        return media.probe(self.settings.ffprobe_path, path,
                           self.settings.ffprobe_timeout_seconds,
                           on_spawn=self._on_spawn, should_cancel=self.should_stop)


def _load_recording(ctx: JobContext) -> Recording:
    with ctx.db.session() as s:
        rec = s.get(Recording, ctx.claimed.recording_id)
        if rec is None:
            raise PermanentJobError("recording no longer exists")
        s.expunge(rec)
        return rec


# ----------------------------------------------------------------------------- INGEST

def handle_ingest(ctx: JobContext) -> None:
    require_storage(ctx)
    rec = _load_recording(ctx)
    try:
        outcome = finalize_recording(ctx.settings, rec, attempt_token=ctx.claimed.token,
                                     progress=ctx.progress, should_stop=ctx.should_stop,
                                     db=ctx.db,
                                     holder=f"job:{ctx.job_id}:{ctx.claimed.token}")
    except StorageOffline as e:
        raise StorageOfflineJobError(e.message) from e
    except FinalizeError as e:
        if e.permanent:
            raise PermanentJobError(str(e)) from e
        raise
    except InterruptedError:
        ctx.check()
        raise
    ctx.check()

    def apply(s: Session) -> None:
        r = s.get(Recording, rec.id)
        r.staging_filename = None
        if r.status in (RecordingStatus.STAGED.value, RecordingStatus.FAILED.value):
            r.status = RecordingStatus.PROCESSING.value
            r.error_message = None
        record_event(s, Severity.INFO, "worker", "recording_stored",
                     f"Recording stored and verified ({outcome})",
                     job_id=ctx.job_id, recording_id=rec.id)

    queue.complete(ctx.db, ctx.claimed, apply)


# --------------------------------------------------------------------------- METADATA

def handle_metadata(ctx: JobContext) -> None:
    require_storage(ctx)
    rec = _load_recording(ctx)
    path = ctx.settings.managed_path(rec.file_path)
    if not path.exists():
        raise PermanentJobError("recording file is missing")
    ctx.progress(10)
    try:
        info = ctx.probe(path)
    except media.InvalidMedia as e:
        raise PermanentJobError(str(e)) from e
    ctx.check()

    def apply(s: Session) -> None:
        r = s.get(Recording, rec.id)
        r.container_format = info.container_format
        r.duration_ms = info.duration_ms
        r.width, r.height = info.width, info.height
        r.fps_num, r.fps_den = info.fps_num, info.fps_den
        r.is_variable_frame_rate = info.is_variable_frame_rate
        r.video_codec, r.audio_codec = info.video_codec, info.audio_codec
        r.has_audio = info.has_audio
        r.status = RecordingStatus.READY.value
        r.error_message = None

    queue.complete(ctx.db, ctx.claimed, apply)


# -------------------------------------------------------------------------- THUMBNAIL

def thumbnail_path(settings: Settings, recording_id: str) -> Path:
    return safe_join(settings.thumbnails_dir, f"{recording_id}.jpg")


def handle_thumbnail(ctx: JobContext) -> None:
    require_storage(ctx)
    rec = _load_recording(ctx)
    s = ctx.settings
    dst = thumbnail_path(s, rec.id)

    def mark_ready(sess: Session) -> None:
        sess.get(Recording, rec.id).thumbnail_status = "READY"

    if media.is_valid_jpeg(dst):  # idempotent: never regenerate a valid thumbnail
        queue.complete(ctx.db, ctx.claimed, mark_ready)
        return

    if s.env == "test" and s.test_slow_ffmpeg_seconds > 0:
        ctx.run([s.ffmpeg_path, "-v", "error", "-nostdin", "-re", "-f", "lavfi", "-i",
                 f"testsrc2=size=64x64:duration={s.test_slow_ffmpeg_seconds}",
                 "-f", "null", "-"], timeout=s.test_slow_ffmpeg_seconds + 30)
        ctx.check()

    src = s.managed_path(rec.file_path)
    tmp = safe_join(s.thumbnails_dir,
                    f"{THUMB_TMP_PREFIX}{ctx.job_id}-{ctx.claimed.token}.tmp.jpg")
    kw: dict = {"on_spawn": ctx._on_spawn, "should_cancel": ctx.should_stop}
    errors = []
    # Floor check for the (internal) thumbnails volume, like every other write.
    layout = get_layout(s)
    res = reservations.reserve_on_volume(
        ctx.db, reservations.volume_for_path(layout, s.thumbnails_dir), s.thumbnails_dir,
        s.min_free_disk_bytes, "thumbnails", f"job:{ctx.job_id}:{ctx.claimed.token}",
        THUMB_RESERVE_BYTES, lease_seconds=s.reservation_lease_seconds)
    try:
        for seek in dict.fromkeys([media.thumbnail_seek_seconds(rec.duration_ms), 0.0]):
            try:
                media.render_thumbnail(s.ffmpeg_path, src, tmp, seek=seek,
                                       width=s.thumbnail_width, height=s.thumbnail_height,
                                       timeout=s.ffmpeg_timeout_seconds, **kw)
                break
            except (media.MediaToolTimeout, media.CommandCancelled, media.MediaToolMissing):
                raise
            except media.MediaError as e:
                errors.append(f"seek {seek}s: {e}")
        else:
            raise media.MediaError("; ".join(errors))
        ctx.check()

        def publish(sess: Session) -> None:
            os.replace(tmp, dst)  # atomic; inside the fenced transaction
            mark_ready(sess)

        queue.complete(ctx.db, ctx.claimed, publish)
    finally:
        tmp.unlink(missing_ok=True)
        reservations.release(ctx.db, res)


def handle_acquire(ctx: JobContext) -> None:
    """Fetch the source's saved URL into a linked recording (``url_download.acquire``).

    Retried: download and network failures, a full disk. Permanent, and never retried:
    rights refusals (recorded with provenance by the worker), URL fetching switched off,
    no downloader installed, a file over the size limit, the bytes already belonging to
    another source. Idempotent across attempts and crashes: the workspace is the job id
    (cleared first), a finished acquisition is returned as is, and identical bytes
    imported by a crashed attempt are linked rather than imported twice."""
    require_storage(ctx)
    ctx.check()
    try:
        result = url_download.acquire(
            ctx.db, ctx.settings, ctx.claimed.source_id, actor="system",
            should_cancel=ctx.should_stop, on_spawn=ctx._on_spawn, progress=ctx.progress,
            workspace=ctx.job_id)
    except RightsRefused as e:
        raise RightsRefusedJobError(e.message) from e
    except StorageOffline as e:
        raise StorageOfflineJobError(e.message) from e
    except (AcquisitionUnavailable, ValidationFailed, PayloadTooLarge, ConflictError,
            NotFoundError) as e:
        raise PermanentJobError(f"{e.code}: {e.message}") from e
    except ServiceUnavailable as e:
        if e.code in ("downloader_missing", "ffmpeg_missing"):
            raise PermanentJobError(f"{e.code}: {e.message}") from e
        raise                                   # the platform/network: retry with backoff
    finally:
        if not ctx.lost.is_set():
            try:
                queue.set_process(ctx.db, ctx.job_id, ctx.claimed.token, None, None)
            except queue.LeaseLost:
                ctx.lost.set()
    ctx.check()

    def link_job(s: Session) -> None:
        s.get(Job, ctx.job_id).recording_id = result.recording_id

    queue.complete(ctx.db, ctx.claimed, link_job)


HANDLERS: dict[JobType, Callable[[JobContext], None]] = {
    JobType.INGEST: handle_ingest,
    JobType.METADATA: handle_metadata,
    JobType.THUMBNAIL: handle_thumbnail,
    JobType.ACQUIRE: handle_acquire,
}
