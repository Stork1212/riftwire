"""Shared fixtures: isolated settings/data dir/database and synthetic media.

All media is generated with FFmpeg's lavfi sources (testsrc2 / sine). No real footage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.core import config as config_mod
from app.core.config import Settings
from app.db.session import Database

BACKEND_DIR = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg not installed")


def _ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-y", *args], check=True, timeout=120)


def make_clip(path: Path, *, duration: float = 2.0, size: str = "320x240",
              rate: str = "30000/1001", vcodec: str = "libx264", audio: bool = True,
              acodec: str = "aac", extra: tuple[str, ...] = ()) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    args += ["-c:v", vcodec]
    if vcodec == "libx264":
        args += ["-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    if vcodec.startswith("libvpx"):
        args += ["-deadline", "realtime", "-cpu-used", "8", "-b:v", "200k"]
    if audio:
        args += ["-c:a", acodec]
    args += [*extra, str(path)]
    _ffmpeg(*args)
    return path


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    """Synthetic media generated once per test session."""
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    make_clip(d / "basic.mp4")
    make_clip(d / "basic.mov", extra=("-brand", "qt  "))
    make_clip(d / "basic.mkv", acodec="libopus")
    make_clip(d / "basic.webm", vcodec="libvpx-vp9", acodec="libopus")
    make_clip(d / "noaudio.mp4", audio=False)
    make_clip(d / "short.mp4", duration=0.2, audio=False)
    make_clip(d / "portrait.mp4", size="240x426", audio=False)
    make_clip(d / "anamorphic.mp4", size="320x240", audio=False,
              extra=("-vf", "setsar=4/3"))
    # Rotation via display matrix (how phones and modern encoders signal it).
    _ffmpeg("-display_rotation:v:0", "90", "-i", str(d / "noaudio.mp4"), "-c", "copy",
            str(d / "rotated.mp4"))
    # Variable frame rate: concatenate segments at different rates.
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=1",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=1",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
            "-fps_mode", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset",
            "ultrafast", str(d / "vfr.mp4"))
    # Audio-only file in an MP4/M4A container (must be rejected: no video stream).
    _ffmpeg("-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac", str(d / "audio_only.mp4"))
    # Corrupted: valid header followed by garbage.
    good = (d / "basic.mp4").read_bytes()
    (d / "truncated.mp4").write_bytes(good[: len(good) // 3])
    (d / "garbage.mp4").write_bytes(os.urandom(64 * 1024))
    (d / "text.mp4").write_bytes(b"this is not a video\n" * 100)
    # A real image with a video extension.
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x64:duration=0.04", "-frames:v", "1",
            str(d / "image.png"))
    shutil.copy(d / "image.png", d / "image_as.mp4")
    return d


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Iterator[Settings]:
    """Isolated settings: temp data dir, temp DB, test env, tiny disk floor."""
    for key in list(os.environ):
        if key.startswith("RIFTWIRE_"):
            monkeypatch.delenv(key, raising=False)
    env = {
        "RIFTWIRE_ENV": "test",
        "RIFTWIRE_DATA_DIR": str(tmp_path / "data"),
        "RIFTWIRE_MIN_FREE_DISK_BYTES": "0",
        "RIFTWIRE_JOB_BACKOFF_BASE_SECONDS": "0",
        "RIFTWIRE_JOB_LEASE_SECONDS": "5",
        "RIFTWIRE_WORKER_POLL_SECONDS": "0.1",
        "RIFTWIRE_WORKER_HEARTBEAT_SECONDS": "0.5",
        "RIFTWIRE_STALE_TEMP_SECONDS": "3600",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    config_mod.reset_settings_cache()
    s = config_mod.get_settings()
    s.ensure_dirs()
    yield s
    config_mod.reset_settings_cache()


def run_migrations(database_url: str) -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@pytest.fixture
def db(settings) -> Iterator[Database]:
    run_migrations(settings.database_url)
    database = Database(settings.database_url)
    yield database
    database.dispose()
