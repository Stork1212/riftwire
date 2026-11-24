"""Download a saved, permission-checked platform video as a temporary MP4."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.config import Settings
from app.core.errors import PayloadTooLarge, ServiceUnavailable, ValidationFailed
from app.db.session import Database
from app.services import media, reservations, rights, sources
from app.services.locations import get_layout, require_no_maintenance

_MAX_FILE = 256 * 1024 * 1024
_MAX_WORKSPACE = 3 * _MAX_FILE
_TIMEOUT = 300


def prepare(db: Database, settings: Settings, source_id: str) -> tuple[Path, Callable[[], None]]:
    with db.session() as session:
        source = sources.get_source(session, source_id)
        if not source.url_canonical:
            raise ValidationFailed("Save a YouTube or Twitch video link before downloading.")
        parsed = sources.parse_url(source.url_canonical)
        decision = rights.require(session, source_id, "acquire", method="url_fetch",
                                  url_acquisition_enabled=settings.url_acquisition_enabled)

    downloader = shutil.which("yt-dlp")
    if downloader is None:
        raise ServiceUnavailable("yt-dlp is required on the RIFTWIRE Mac for authorized downloads.",
                                 code="downloader_missing")
    ffmpeg = shutil.which(settings.ffmpeg_path)
    if ffmpeg is None:
        raise ServiceUnavailable("FFmpeg is required to combine video and audio into an MP4.")
    node = shutil.which("node")
    js_runtime = ["--js-runtimes", f"node:{node}"] if node else []
    require_no_maintenance(settings)
    layout = get_layout(settings)
    layout.require_online("temp")
    root = layout.path("temp")
    volume = reservations.volume_for_path(layout, root)
    reservation = reservations.reserve_on_volume(
        db, volume, root, max(settings.min_free_disk_bytes, layout.floor_for_volume(volume)),
        "url-download", f"url:{source_id}", _MAX_WORKSPACE + 32 * 1024 * 1024,
        lease_seconds=max(settings.reservation_lease_seconds, _TIMEOUT + 600))
    folder: TemporaryDirectory | None = None

    def cleanup() -> None:
        try:
            if folder is not None:
                folder.cleanup()
        finally:
            reservations.release(db, reservation)

    try:
        folder = TemporaryDirectory(prefix="source-download-", dir=root)
        path = Path(folder.name)
        output = path / "video.mp4"

        def over_limit() -> bool:
            total = 0
            for item in path.iterdir():
                try:
                    total += item.stat().st_size
                except FileNotFoundError:
                    continue
            return total >= _MAX_WORKSPACE

        try:
            result = media.run_command([
                downloader, "--ignore-config", "--no-playlist", "--no-progress", *js_runtime,
                "--match-filter", "!is_live", "--socket-timeout", "15", "--retries", "1",
                "--max-filesize", str(_MAX_FILE), "--ffmpeg-location", ffmpeg,
                "-f", "bv[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]",
                "--merge-output-format", "mp4", "-o", str(output), "--", parsed.canonical_url,
            ], _TIMEOUT, should_cancel=over_limit)
        except media.CommandCancelled as exc:
            raise PayloadTooLarge("The download exceeded the temporary storage limit.") from exc
        except media.MediaError as exc:
            raise ServiceUnavailable("The platform download could not finish.") from exc
        if result.returncode != 0 or not output.is_file():
            raise ServiceUnavailable("The platform did not provide a downloadable MP4 within the "
                                     "quality and size limits.", code="download_unavailable")
        if not 0 < output.stat().st_size <= _MAX_FILE:
            raise PayloadTooLarge("The downloaded MP4 exceeds 256 MiB or is empty.")
        with db.session() as session:
            rights.fence(session, decision)
        reservations.release(db, reservation)
        return output, folder.cleanup
    except BaseException:
        cleanup()
        raise
