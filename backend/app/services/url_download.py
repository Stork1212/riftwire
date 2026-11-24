"""Acquire a saved, permission-checked platform video as a linked recording.

One saved source URL becomes one recording, through the same path as every other import:

1. **Permission, before any network access:** the source's current rights allow
   ``acquire`` by ``url_fetch``. That needs the method in the licence, written platform
   permission on record, and URL acquisition switched on (``check_acquire``).
2. **Download** into a reserved temporary folder on the temp location (size, time and
   quality limits; live streams and playlists refused).
3. **Re-check** the rights (``fence``): if they changed during the download, nothing is
   imported and the download is discarded.
4. **Import** the file through ``UploadSession`` / ``complete_upload``, the same code path as
   a dashboard upload. That covers content validation, duplicate detection by SHA-256, the
   storage floor and the INGEST job.
5. **Link** it to the source with ``attach_media(method="url_fetch")``. That re-checks the
   rights in the same transaction as the link, records the actual method on the recording,
   and writes ``attached_to_source`` / ``media_attached`` provenance plus an
   ``acquired_by_url`` event on the source (URL, downloader and version, SHA-256, size).

Idempotent per source: when a recording fetched from this source already exists it is
returned and nothing is downloaded again. The temporary download is always removed.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    PayloadTooLarge,
    ServiceUnavailable,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.db.session import Database
from app.models import AcquisitionMethod, Recording, Severity
from app.services import acquisition, media, queue, reservations, rights, sources
from app.services.events import record_event
from app.services.ingest import UploadSession, complete_upload
from app.services.locations import get_layout, require_no_maintenance

log = get_logger("url_download")

URL_FETCH = AcquisitionMethod.URL_FETCH.value
MAX_FILE = 256 * 1024 * 1024
MAX_WORKSPACE = 3 * MAX_FILE          # video + audio parts + merged file
TIMEOUT = 300
CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class AcquireResult:
    recording_id: str
    source_id: str
    job_id: str | None           # the INGEST job; None when already acquired
    already_acquired: bool


def _existing(db: Database, source_id: str) -> Recording | None:
    with db.session() as s:
        return s.scalar(select(Recording).where(Recording.source_id == source_id,
                                                Recording.acquisition_method == URL_FETCH))


def _tools(settings: Settings) -> tuple[str, str, list[str]]:
    downloader = shutil.which("yt-dlp")
    if downloader is None:
        raise ServiceUnavailable("yt-dlp is required on the RIFTWIRE Mac for authorized "
                                 "downloads.", code="downloader_missing")
    ffmpeg = shutil.which(settings.ffmpeg_path)
    if ffmpeg is None:
        raise ServiceUnavailable("FFmpeg is required to combine video and audio into an MP4.",
                                 code="ffmpeg_missing")
    node = shutil.which("node")
    return downloader, ffmpeg, (["--js-runtimes", f"node:{node}"] if node else [])


def _version(downloader: str) -> str:
    try:
        r = media.run_command([downloader, "--version"], 15)
        return r.stdout.decode("utf-8", "replace").strip()[:40] or "unknown"
    except media.MediaError:
        return "unknown"


def _download(downloader: str, ffmpeg: str, js_runtime: list[str], url: str,
              folder: Path, should_cancel: Callable[[], bool] | None,
              on_spawn: Callable[[int, str | None], None] | None) -> Path:
    output = folder / "video.mp4"

    def over_limit() -> bool:
        total = 0
        for item in folder.iterdir():
            try:
                total += item.stat().st_size
            except FileNotFoundError:
                continue
        return total >= MAX_WORKSPACE or bool(should_cancel and should_cancel())

    try:
        result = media.run_command([
            downloader, "--ignore-config", "--no-playlist", "--no-progress", *js_runtime,
            "--match-filter", "!is_live", "--socket-timeout", "15", "--retries", "1",
            "--max-filesize", str(MAX_FILE), "--ffmpeg-location", ffmpeg,
            "-f", "bv[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]",
            "--merge-output-format", "mp4", "-o", str(output), "--", url,
        ], TIMEOUT, should_cancel=over_limit, on_spawn=on_spawn)
    except media.CommandCancelled as exc:
        if should_cancel and should_cancel():
            raise
        raise PayloadTooLarge("The download exceeded the temporary storage limit.") from exc
    except media.MediaError as exc:
        raise ServiceUnavailable("The platform download could not finish.",
                                 code="download_failed") from exc
    if result.returncode != 0 or not output.is_file():
        raise ServiceUnavailable("The platform did not provide a downloadable MP4 within the "
                                 "quality and size limits.", code="download_unavailable")
    if not 0 < output.stat().st_size <= MAX_FILE:
        raise PayloadTooLarge("The downloaded MP4 exceeds 256 MiB or is empty.")
    return output


def _import(db: Database, settings: Settings, path: Path, filename: str):
    size = path.stat().st_size
    up = UploadSession(settings, filename, size, db)
    try:
        with open(path, "rb") as f:
            while data := f.read(CHUNK):
                up.write(data)
        digest = up.finish()
    except BaseException:
        up.abort()
        raise
    return complete_upload(db, settings, up, digest)


@contextmanager
def _workspace(root: Path, name: str | None) -> Iterator[Path]:
    """A fresh download folder under the temp location. A named one (the job id) is
    cleared first, so an attempt that crashed leaves nothing behind for the next."""
    if name is None:
        with TemporaryDirectory(prefix="source-download-", dir=root) as tmp:
            yield Path(tmp)
        return
    folder = root / f"source-download-{name}"
    if folder.parent != root or not name.replace("-", "").isalnum():
        raise ValidationFailed("invalid workspace name")
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def acquire(db: Database, settings: Settings, source_id: str, *, actor: str = "owner",
            should_cancel: Callable[[], bool] | None = None,
            on_spawn: Callable[[int, str | None], None] | None = None,
            progress: Callable[[int], None] | None = None,
            workspace: str | None = None) -> AcquireResult:
    """Download the source's saved URL and return the linked recording (see module doc).
    The keyword hooks let the worker run it as a job: cancellation, child-process
    tracking for orphan cleanup, progress, and a per-job workspace."""
    step = progress or (lambda _p: None)
    with db.session() as s:
        src = sources.get_source(s, source_id)
        if not src.url_canonical:
            raise ValidationFailed("Save a YouTube or Twitch video link before acquiring.")
        parsed = sources.parse_url(src.url_canonical)
        decision = acquisition.check_acquire(s, settings, source_id, URL_FETCH, fetched=True)
    done = _existing(db, source_id)
    if done is not None:
        return AcquireResult(done.id, source_id, None, True)

    downloader, ffmpeg, js_runtime = _tools(settings)
    require_no_maintenance(settings)
    layout = get_layout(settings)
    layout.require_online("temp")
    root = layout.path("temp")
    volume = reservations.volume_for_path(layout, root)
    reservation = reservations.reserve_on_volume(
        db, volume, root, max(settings.min_free_disk_bytes, layout.floor_for_volume(volume)),
        "url-download", f"url:{source_id}", MAX_WORKSPACE + 32 * 1024 * 1024,
        lease_seconds=max(settings.reservation_lease_seconds, TIMEOUT + 600))
    try:
        with _workspace(root, workspace) as tmp:
            version = _version(downloader)
            step(5)
            path = _download(downloader, ffmpeg, js_runtime, parsed.canonical_url, tmp,
                             should_cancel, on_spawn)
            step(70)
            with db.session() as s:            # rights changed during the download?
                rights.fence(s, decision)
            filename = f"{parsed.platform}-{parsed.video_id}.mp4"
            try:
                result = _import(db, settings, path, filename)
            except ConflictError as e:         # identical bytes already imported
                existing = e.details.get("existing_recording_id")
                with db.session() as s:
                    rec = s.get(Recording, existing) if existing else None
                    if rec is not None and rec.source_id is None:
                        # Imported earlier without a link: link it now, as fetched.
                        acquisition.attach_media(s, settings, source_id, rec.id, URL_FETCH,
                                                 actor=actor, fetched=True)
                        _provenance(s, source_id, parsed.canonical_url, downloader,
                                    version, rec, actor)
                        return AcquireResult(rec.id, source_id, None, True)
                    if rec is not None and rec.source_id == source_id:
                        return AcquireResult(rec.id, source_id, None, True)
                raise
            rec_id = result.recording.id
            step(95)
            try:
                with db.session() as s:
                    acquisition.attach_media(s, settings, source_id, rec_id, URL_FETCH,
                                             actor=actor, fetched=True)
                    _provenance(s, source_id, parsed.canonical_url, downloader, version,
                                s.get(Recording, rec_id), actor)
            except AppError as e:
                # Rights changed between import and link: the recording exists but is not
                # linked, so it can never be analysed, edited or published (as for uploads).
                with db.session() as s:
                    record_event(s, Severity.WARNING, "url_download", "acquire_unlinked",
                                 f"Fetched media for source {source_id} was not linked: "
                                 f"{e.message}", recording_id=rec_id)
                raise
            log.info("source acquired", extra={"recording_id": rec_id,
                                               "data": {"source_id": source_id}})
            return AcquireResult(rec_id, source_id, result.job_id, False)
    finally:
        reservations.release(db, reservation)


def queue_for_new_source(s, settings: Settings, source) -> None:
    """A newly saved link is acquired without a prompt when URL fetching is on (owner
    acquisition policy, 2026-11-24). A refusal never blocks saving the source: it is
    recorded as a system event and shown on the source's page."""
    if not settings.url_acquisition_enabled or source.url_canonical is None:
        return
    try:
        queue.enqueue_acquire(s, settings, source.id, settings.job_max_attempts)
    except AppError as e:
        record_event(s, Severity.WARNING, "url_download", "acquire_not_queued",
                     f"Source {source.id} saved but not queued for acquisition: {e.message}")


def _provenance(s, source_id: str, url: str, downloader: str, version: str,
                rec: Recording, actor: str) -> None:
    rights.provenance(s, "source", source_id, "acquired_by_url", actor=actor, url=url,
                      downloader=Path(downloader).name, downloader_version=version,
                      recording_id=rec.id, sha256=rec.sha256, bytes=rec.file_size_bytes)
