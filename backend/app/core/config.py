"""Typed application settings.

All values can be overridden with environment variables prefixed ``RIFTWIRE_``
or via a ``.env`` file at the repository root. No absolute paths are hardcoded:
defaults are derived from the repository location.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

GIB = 1024**3


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RIFTWIRE_",
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    data_dir: Path = REPO_ROOT / "data"
    database_path: Path | None = None

    max_upload_bytes: int = Field(default=50 * GIB, gt=0)
    # Owner requirement: keep at least 60 GiB free on the data volume.
    min_free_disk_bytes: int = Field(default=60 * GIB, ge=0)
    upload_chunk_bytes: int = Field(default=1024 * 1024, ge=4096)
    allowed_extensions: list[str] = ["mp4", "mov", "mkv", "webm"]
    stale_temp_seconds: int = Field(default=3600, ge=0)

    worker_concurrency: int = Field(default=1, ge=1, le=16)
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    worker_heartbeat_seconds: float = Field(default=5.0, gt=0)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    job_lease_seconds: int = Field(default=60, ge=2)
    job_backoff_base_seconds: float = Field(default=5.0, ge=0)
    job_backoff_max_seconds: float = Field(default=600.0, ge=0)
    worker_shutdown_grace_seconds: float = Field(default=30.0, ge=0)
    worker_stale_after_seconds: float = Field(default=20.0, gt=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: float = Field(default=30.0, gt=0)
    ffmpeg_timeout_seconds: float = Field(default=120.0, gt=0)

    thumbnail_width: int = Field(default=480, ge=16, le=3840)
    thumbnail_height: int = Field(default=270, ge=16, le=2160)

    # ---- Test-only hooks (ignored unless env == "test") ----------------------
    # Name of a crash point at which the worker hard-exits (crash-safety tests).
    test_crash_at: str | None = None
    # Run an extra real-time FFmpeg child of this many seconds inside THUMBNAIL jobs.
    test_slow_ffmpeg_seconds: float = 0.0

    @field_validator("allowed_extensions")
    @classmethod
    def _normalise_ext(cls, v: list[str]) -> list[str]:
        return [e.lower().lstrip(".") for e in v]

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, v: Path) -> Path:
        v = Path(v).expanduser()
        if not v.is_absolute():
            v = (REPO_ROOT / v).resolve()
        return v

    # Derived locations -------------------------------------------------
    @property
    def db_path(self) -> Path:
        if self.database_path is not None:
            p = Path(self.database_path).expanduser()
            return p if p.is_absolute() else (REPO_ROOT / p).resolve()
        return self.data_dir / "riftwire.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def required_dirs(self) -> list[Path]:
        return [
            self.data_dir,
            self.recordings_dir,
            self.thumbnails_dir,
            self.temp_dir,
            self.exports_dir,
            self.logs_dir,
            self.db_path.parent,
        ]

    def ensure_dirs(self) -> None:
        for d in self.required_dirs():
            d.mkdir(parents=True, exist_ok=True)

    def safe_summary(self) -> dict:
        """Non-sensitive settings suitable for logs and the System page."""
        return {
            "env": self.env,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "data_dir": str(self.data_dir),
            "max_upload_bytes": self.max_upload_bytes,
            "min_free_disk_bytes": self.min_free_disk_bytes,
            "worker_concurrency": self.worker_concurrency,
            "job_max_attempts": self.job_max_attempts,
            "job_lease_seconds": self.job_lease_seconds,
            "log_level": self.log_level,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
