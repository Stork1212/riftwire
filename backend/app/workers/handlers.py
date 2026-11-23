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
from app.core.errors import PermanentJobError
from app.core.logging import get_logger
from app.db.session import Database
from app.models import JobType, Recording, RecordingStatus, Severity
from app.services import media, queue
from app.services.events import record_event
from app.services.ingest import FinalizeError, finalize_recording
from app.services.storage import safe_join

log = get_logger("handlers")


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
    rec = _load_recording(ctx)
    try:
        outcome = finalize_recording(ctx.settings, rec, progress=ctx.progress,
                                     should_stop=ctx.should_stop)
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
    rec = _load_recording(ctx)
    path = safe_join(ctx.settings.data_dir, rec.file_path)
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

    src = safe_join(s.data_dir, rec.file_path)
    tmp = safe_join(s.temp_dir, f"job-{ctx.job_id}-{ctx.claimed.token}.tmp.jpg")
    kw: dict = {"on_spawn": ctx._on_spawn, "should_cancel": ctx.should_stop}
    errors = []
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


HANDLERS: dict[JobType, Callable[[JobContext], None]] = {
    JobType.INGEST: handle_ingest,
    JobType.METADATA: handle_metadata,
    JobType.THUMBNAIL: handle_thumbnail,
}
