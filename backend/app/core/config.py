"""Typed application settings.

All values can be overridden with environment variables prefixed ``RIFTWIRE_``
or via a ``.env`` file at the repository root. No absolute paths are hardcoded:
defaults are derived from the repository location.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

GIB = 1024**3
MIN_FLOOR_BYTES = 60 * GIB
LOCATION_KINDS = ("recordings", "temp", "models", "exports", "assets")


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
    # Owner requirement: keep at least 60 GiB free on EVERY volume RIFTWIRE writes to.
    # Mandatory: values below MIN_FLOOR_BYTES are refused unless env == "test" (isolated
    # test runs only).
    min_free_disk_bytes: int = Field(default=60 * GIB, ge=0)
    # Disk reservations expire unless renewed (crashed holders never block space forever).
    reservation_lease_seconds: int = Field(default=900, ge=5)
    # How long a cached location verification is trusted by the worker's claim loop.
    storage_check_seconds: float = Field(default=2.0, ge=0)
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

    # ---- M2 rights & sources -------------------------------------------------
    # Evidence files (supporting documentation for rights records), internal disk.
    evidence_max_file_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    evidence_max_total_bytes: int = Field(default=1 * GIB, gt=0)
    # Global switch for URL fetching (acquisition method url_fetch, services/url_download).
    # Off by default (owner decision 2026-11-25, superseding 2026-11-24). Even when on, a
    # link is fetched only under a user-confirmed record that allows ``acquire`` by
    # ``url_fetch`` with written platform permission attached; public availability is
    # never permission.
    url_acquisition_enabled: bool = False

    # Local Whisper (M4 shared transcription): the Python of the venv that has mlx-whisper,
    # and the model directory (MLX ``base`` is the approved default; never downloaded).
    # The names keep their pre-M4 ``clip_`` prefix (RIFTWIRE_CLIP_WHISPER_*).
    clip_whisper_python: Path | None = None
    clip_whisper_model: Path | None = None
    # Chunking of the persistent TRANSCRIBE job. Part of the transcription configuration's
    # fingerprint: changing them yields a new transcript beside the old one.
    transcribe_chunk_seconds: int = Field(default=120, ge=10, le=1800)
    transcribe_overlap_seconds: int = Field(default=4, ge=0, le=30)
    transcribe_language: str = Field(default="en", min_length=2, max_length=8)
    transcribe_child_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    clip_outro_path: Path | None = None
    # Outro handling (services/outro.py). The duration always comes from the asset; this
    # optional brand value is validated against it (a mismatch refuses, never trims).
    clip_outro_duration_seconds: float | None = Field(default=None, gt=0, le=10)
    # continue: gameplay audio runs under the outro, outro audio mixed beneath (PR #6
    # handoff). outro: gameplay audio ends at the cut; the outro's audio or silence plays.
    clip_outro_audio: Literal["continue", "outro"] = "continue"
    clip_outro_fade_ms: int = Field(default=500, ge=0, le=10_000)
    clip_outro_sting_gain_db: float = Field(default=-6.0, ge=-60, le=0)

    thumbnail_width: int = Field(default=480, ge=16, le=3840)
    thumbnail_height: int = Field(default=270, ge=16, le=2160)

    # ---- Test-only hooks (ignored unless env == "test") ----------------------
    # Name of a crash point at which the worker hard-exits (crash-safety tests).
    test_crash_at: str | None = None
    # Run an extra real-time FFmpeg child of this many seconds inside THUMBNAIL jobs.
    test_slow_ffmpeg_seconds: float = 0.0
    # Force the no-hard-link copy fallback in INGEST finalize.
    test_force_copy_fallback: bool = False

    # Resolved storage layout (kind -> root), applied by services.locations.load_layout.
    _locations: dict[str, Path] = PrivateAttr(default_factory=dict)
    _layout: object | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _floor_is_mandatory(self) -> Settings:
        if self.min_free_disk_bytes < MIN_FLOOR_BYTES and self.env != "test":
            raise ValueError(
                f"RIFTWIRE_MIN_FREE_DISK_BYTES={self.min_free_disk_bytes} is below the "
                f"mandatory {MIN_FLOOR_BYTES // GIB} GiB floor (only RIFTWIRE_ENV=test "
                "may lower it)")
        return self

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

    # ---- configurable locations (M1): resolved through the loaded layout ------
    def default_location(self, kind: str) -> Path:
        return self.data_dir / kind

    def location(self, kind: str) -> Path:
        if kind not in LOCATION_KINDS:
            raise KeyError(kind)
        if kind in self._locations:
            return self._locations[kind]
        if self._layout is not None:
            # A layout is loaded but this kind is not registered (e.g. its drive could not
            # be identified): never fall back to the internal default path.
            from app.core.errors import StorageOffline

            raise StorageOffline(f"Storage location '{kind}' is not registered",
                                 details={"kind": kind, "reason": "not_registered"})
        return self.default_location(kind)  # no layout yet (tests/tools before startup)

    def apply_layout(self, roots: dict[str, Path], layout: object | None = None) -> None:
        self._locations = dict(roots)
        self._layout = layout

    @property
    def layout(self) -> object | None:
        return self._layout

    @property
    def recordings_dir(self) -> Path:
        return self.location("recordings")

    @property
    def temp_dir(self) -> Path:
        return self.location("temp")

    @property
    def models_dir(self) -> Path:
        return self.location("models")

    @property
    def exports_dir(self) -> Path:
        return self.location("exports")

    @property
    def assets_dir(self) -> Path:
        return self.location("assets")

    # ---- always internal ------------------------------------------------------
    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    def internal_dirs(self) -> list[Path]:
        """Directories that always live on the internal disk (database, logs, thumbnails,
        rights evidence)."""
        return [self.data_dir, self.thumbnails_dir, self.logs_dir, self.evidence_dir,
                self.db_path.parent]

    def required_dirs(self) -> list[Path]:
        return self.internal_dirs()

    def ensure_internal_dirs(self) -> None:
        for d in self.internal_dirs():
            d.mkdir(parents=True, exist_ok=True)

    def ensure_dirs(self) -> None:
        """Create the internal directories, plus the *default* (internal) folder of every
        location kind that is not configured elsewhere. Never creates a configured
        external location: that would write to the internal disk if the drive is absent."""
        for d in self.internal_dirs():
            d.mkdir(parents=True, exist_ok=True)
        for kind in LOCATION_KINDS:
            if self._locations.get(kind, self.default_location(kind)) \
                    == self.default_location(kind):
                self.default_location(kind).mkdir(parents=True, exist_ok=True)

    def managed_path(self, rel: str) -> Path:
        """Resolve a stored relative path such as ``recordings/<id>.mp4`` through the
        layout: the first component names the location kind. Guards against traversal."""
        from app.services.storage import safe_join  # local import: avoid a cycle

        kind, sep, rest = rel.partition("/")
        if not sep or kind not in LOCATION_KINDS or not rest:
            raise ValueError(f"not a managed location path: {rel!r}")
        return safe_join(self.location(kind), rest)

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
