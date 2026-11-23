"""Crash-safe recording ingestion (ARCHITECTURE §6).

Request path (API):  stream → temp/<id>.part → validate → temp/<id>.staged →
                     one transaction: recording(STAGED) + INGEST job.
Finalize (worker):   idempotent link → fsync → unlink into recordings/<id>.<ext>.
Reconciler:          cleans temp leftovers, re-enqueues STAGED rows, reports orphans.
Nothing in recordings/ is ever deleted or overwritten.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    InsufficientStorage,
    PayloadTooLarge,
    UnsupportedMedia,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.core.testhooks import crashpoint
from app.db.base import utcnow
from app.db.session import Database
from app.models import Job, JobStatus, JobType, Recording, RecordingStatus, Severity
from app.services import media
from app.services.events import record_event
from app.services.queue import enqueue
from app.services.storage import (
    SpaceReservations,
    file_extension,
    free_bytes,
    fsync_dir,
    fsync_file,
    safe_join,
    sanitize_display_filename,
    sha256_file,
)

log = get_logger("ingest")

SPACE_CHECK_EVERY = 256 * 1024 * 1024
RESERVATIONS = SpaceReservations()


# ============================================================================ upload

class UploadSession:
    """Receives one streamed upload into temp/<import_id>.part.

    Synchronous by design (no FastAPI imports); the API calls ``write`` from a thread.
    """

    def __init__(self, settings: Settings, filename: str, content_length: int | None):
        self.settings = settings
        self.original_filename = sanitize_display_filename(filename)
        self.ext = file_extension(filename)
        self.import_id = str(uuid.uuid4())
        self.part_path = settings.temp_dir / f"{self.import_id}.part"
        self.size = 0
        self._hash = hashlib.sha256()
        self._fh = None
        self._since_check = 0
        self._content_length = content_length
        self._reserved = False

        if self.ext not in settings.allowed_extensions:
            raise UnsupportedMedia(
                f"Unsupported file type '.{self.ext or '?'}'. Allowed: "
                + ", ".join("." + e for e in settings.allowed_extensions),
                details={"allowed": settings.allowed_extensions})
        if content_length is not None:
            if content_length <= 0:
                raise ValidationFailed("Empty upload")
            if content_length > settings.max_upload_bytes:
                raise PayloadTooLarge(
                    f"File is {content_length} bytes; the limit is "
                    f"{settings.max_upload_bytes} bytes",
                    details={"max_upload_bytes": settings.max_upload_bytes})
        needed = content_length or 0
        free = free_bytes(settings.temp_dir)
        if not RESERVATIONS.try_reserve(self.import_id, needed, free,
                                        settings.min_free_disk_bytes):
            raise InsufficientStorage(
                "Not enough free disk space: importing this file would leave less than "
                f"the {settings.min_free_disk_bytes // 1024**3} GiB safety minimum.",
                details={"free_bytes": free, "required_bytes": needed,
                         "min_free_bytes": settings.min_free_disk_bytes})
        self._reserved = True
        self._fh = open(self.part_path, "xb")  # noqa: SIM115  exclusive create; closed in finish/abort

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > self.settings.max_upload_bytes:
            raise PayloadTooLarge("Upload exceeds the maximum allowed size",
                                  details={"max_upload_bytes": self.settings.max_upload_bytes})
        if self._content_length is not None and self.size > self._content_length:
            raise ValidationFailed("Upload is larger than its declared Content-Length")
        self._hash.update(chunk)
        self._fh.write(chunk)
        self._since_check += len(chunk)
        if self._since_check >= SPACE_CHECK_EVERY:
            self._since_check = 0
            free = free_bytes(self.settings.temp_dir)
            if free < self.settings.min_free_disk_bytes:
                raise InsufficientStorage(
                    "Free disk space fell below the safety minimum during upload",
                    details={"free_bytes": free,
                             "min_free_bytes": self.settings.min_free_disk_bytes})

    def finish(self) -> str:
        """Close and fsync the part file. Returns the SHA-256 hex digest."""
        if self._content_length is not None and self.size != self._content_length:
            raise ValidationFailed("Upload incomplete: received fewer bytes than declared")
        if self.size == 0:
            raise ValidationFailed("Empty upload")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        return self._hash.hexdigest()

    def abort(self) -> None:
        """Remove the partial file (temp only). Safe to call more than once."""
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        try:
            self.part_path.unlink()
        except FileNotFoundError:
            pass
        self.release()

    def release(self) -> None:
        if self._reserved:
            RESERVATIONS.release(self.import_id)
            self._reserved = False


@dataclass
class ImportResult:
    recording: Recording
    job_id: str


def existing_by_hash(db: Database, sha256: str) -> Recording | None:
    with db.session() as s:
        return s.scalar(select(Recording).where(Recording.sha256 == sha256))


def _duplicate_error(rec: Recording) -> ConflictError:
    return ConflictError(
        f"This recording was already imported as '{rec.original_filename}'.",
        code="duplicate_recording",
        details={"existing_recording_id": rec.id, "sha256": rec.sha256})


def complete_upload(db: Database, settings: Settings, up: UploadSession,
                    sha256: str) -> ImportResult:
    """Validate the finished .part file, stage it and register the intent row.

    On any failure the temp file is removed; nothing outside temp/ is touched.
    """
    try:
        dup = existing_by_hash(db, sha256)
        if dup is not None:
            raise _duplicate_error(dup)
        try:
            info = media.probe(settings.ffprobe_path, up.part_path,
                               settings.ffprobe_timeout_seconds)
            media.verify_decodable(settings.ffmpeg_path, up.part_path,
                                   settings.ffmpeg_timeout_seconds,
                                   duration_ms=info.duration_ms)
        except media.InvalidMedia as e:
            raise ValidationFailed(f"Invalid or unsupported media: {e}",
                                   code="invalid_media") from e
        except media.MediaToolMissing as e:
            raise ValidationFailed("FFmpeg/ffprobe is not installed; cannot validate media",
                                   code="ffmpeg_missing", status_code=503) from e
        except media.MediaToolTimeout as e:
            raise ValidationFailed(f"Media inspection timed out: {e}",
                                   code="inspection_timeout", status_code=503) from e

        ext = media.choose_extension(info, up.ext)
        rec_id = up.import_id
        stored = f"{rec_id}.{ext}"
        staged_name = f"{rec_id}.staged"
        staged_path = settings.temp_dir / staged_name
        os.rename(up.part_path, staged_path)  # atomic; marks "validated"
        fsync_dir(settings.temp_dir)
        crashpoint("after_stage_rename")
        try:
            with db.session() as s:
                rec = Recording(
                    id=rec_id, original_filename=up.original_filename, stored_filename=stored,
                    file_path=f"recordings/{stored}", staging_filename=staged_name,
                    sha256=sha256, file_size_bytes=up.size,
                    container_format=info.container_format, duration_ms=info.duration_ms,
                    width=info.width, height=info.height, fps_num=info.fps_num,
                    fps_den=info.fps_den, is_variable_frame_rate=info.is_variable_frame_rate,
                    video_codec=info.video_codec, audio_codec=info.audio_codec,
                    has_audio=info.has_audio, imported_at=utcnow(),
                    status=RecordingStatus.STAGED.value,
                )
                s.add(rec)
                s.flush()
                job = enqueue(s, rec_id, JobType.INGEST, settings.job_max_attempts)
                record_event(s, Severity.INFO, "api", "recording_imported",
                             f"Imported '{up.original_filename}' ({up.size} bytes)",
                             recording_id=rec_id, job_id=job.id)
                job_id = job.id
        except IntegrityError:
            staged_path.unlink(missing_ok=True)
            dup = existing_by_hash(db, sha256)
            if dup is not None:
                raise _duplicate_error(dup) from None
            raise
        crashpoint("after_intent_commit")
        return ImportResult(rec, job_id)
    finally:
        up.release()
        if up.part_path.exists():
            up.part_path.unlink(missing_ok=True)


# ============================================================================ finalize

class FinalizeError(Exception):
    def __init__(self, message: str, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


def _make_readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def finalize_recording(settings: Settings, rec: Recording, *, progress=None,
                       should_stop=None) -> str:
    """Move a STAGED recording into recordings/ (idempotent; never overwrites).

    Performs only filesystem work; the caller records the DB result in a fenced
    transaction. Returns a short description of what happened.
    """
    dst = safe_join(settings.data_dir, rec.file_path)
    src = (safe_join(settings.temp_dir, rec.staging_filename)
           if rec.staging_filename else None)
    total = max(1, rec.file_size_bytes)

    def prog(done: int) -> None:
        if progress:
            progress(int(done * 90 / total))

    def verify(path: Path) -> None:
        digest = sha256_file(path, progress=prog, should_stop=should_stop)
        if digest != rec.sha256:
            raise FinalizeError(f"SHA-256 mismatch for {path.name}: file is corrupt",
                                permanent=True)

    src_exists = src is not None and src.exists()
    dst_exists = dst.exists()

    if src_exists and not dst_exists:
        verify(src)
        crashpoint("finalize_before_link")
        try:
            os.link(src, dst)  # refuses to replace an existing file
        except FileExistsError:
            dst_exists = True
        except OSError as e:
            if e.errno in (errno.EPERM, errno.ENOTSUP, errno.EXDEV):
                # Filesystem without hard links: exclusive-create copy fallback.
                _copy_exclusive(src, dst)
                dst_exists = True
            else:
                raise
        else:
            fsync_file(dst)
            fsync_dir(dst.parent)
            crashpoint("finalize_after_link")
            src.unlink()
            fsync_dir(src.parent)
            crashpoint("finalize_after_unlink")
            _make_readonly(dst)
            return "moved"
    if src_exists and dst_exists:
        verify(dst)
        try:
            src.unlink()
        except FileNotFoundError:
            pass
        fsync_dir(settings.temp_dir)
        _make_readonly(dst)
        return "deduplicated_temp_copy"
    if dst_exists:
        verify(dst)
        _make_readonly(dst)
        return "already_final"
    raise FinalizeError("recording file is missing from both temp and recordings directories",
                        permanent=True)


def _copy_exclusive(src: Path, dst: Path) -> None:
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        while chunk := fi.read(1024 * 1024):
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())
    fsync_dir(dst.parent)


# ============================================================================ reconcile

@dataclass
class ReconcileReport:
    removed_temp: list[str]
    reenqueued: list[str]
    orphan_files: list[str]
    missing_files: list[str]


def reconcile(db: Database, settings: Settings, component: str,
              now_ts: float | None = None) -> ReconcileReport:
    """Startup reconciler (§6.5). Deletes only stale files in temp/."""
    now_ts = now_ts or time.time()
    report = ReconcileReport([], [], [], [])
    with db.session() as s:
        staged_names = set(s.scalars(select(Recording.staging_filename)
                                     .where(Recording.staging_filename.is_not(None))))
        known_files = set(s.scalars(select(Recording.stored_filename)))

    for p in sorted(settings.temp_dir.iterdir()):
        if not p.is_file():
            continue
        age = now_ts - p.stat().st_mtime
        if age < settings.stale_temp_seconds:
            continue
        stale_kind = (p.suffix == ".part"
                      or (p.suffix == ".staged" and p.name not in staged_names)
                      or ".tmp." in p.name)
        if stale_kind:
            p.unlink(missing_ok=True)
            report.removed_temp.append(p.name)

    with db.session() as s:
        for rec in s.scalars(select(Recording).where(
                Recording.status == RecordingStatus.STAGED.value)):
            job = s.scalar(select(Job).where(Job.recording_id == rec.id,
                                             Job.job_type == JobType.INGEST.value))
            if job is None:
                enqueue(s, rec.id, JobType.INGEST, settings.job_max_attempts)
                report.reenqueued.append(rec.id)
            elif job.status == JobStatus.CANCELLED.value:
                pass  # owner cancelled; leave as is

        for p in sorted(settings.recordings_dir.iterdir()):
            if p.is_file() and not p.name.startswith(".") and p.name not in known_files:
                report.orphan_files.append(p.name)
        for rec in s.scalars(select(Recording).where(
                Recording.status == RecordingStatus.READY.value)):
            if not (settings.data_dir / rec.file_path).exists():
                report.missing_files.append(rec.id)

        if report.removed_temp:
            record_event(s, Severity.INFO, component, "temp_cleanup",
                         f"Removed {len(report.removed_temp)} stale temporary file(s)")
        for rid in report.reenqueued:
            record_event(s, Severity.WARNING, component, "ingest_reenqueued",
                         "STAGED recording had no INGEST job; re-enqueued", recording_id=rid)
        for name in report.orphan_files:
            record_event(s, Severity.WARNING, component, "orphan_recording_file",
                         f"File in recordings/ has no database record (kept): {name}")
        for rid in report.missing_files:
            record_event(s, Severity.ERROR, component, "recording_file_missing",
                         "READY recording's file is missing from recordings/",
                         recording_id=rid)
    return report

