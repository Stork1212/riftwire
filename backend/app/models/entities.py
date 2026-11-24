"""ORM models for recordings, jobs, system events and worker heartbeats."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UTCDateTime, UUIDStr, new_uuid, utcnow


class RecordingStatus(enum.StrEnum):
    STAGED = "STAGED"          # row committed; file still in temp/, awaiting INGEST finalize
    PROCESSING = "PROCESSING"  # file finalized in recordings/; metadata pending
    READY = "READY"            # verified + metadata extracted
    FAILED = "FAILED"          # a required processing stage failed permanently


class ThumbnailStatus(enum.StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class JobType(enum.StrEnum):
    INGEST = "INGEST"
    METADATA = "METADATA"
    THUMBNAIL = "THUMBNAIL"


class JobStatus(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    CANCELLED = "CANCELLED"


class Severity(enum.StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _enum_check(col: str, e: type[enum.Enum]) -> str:
    vals = ",".join(f"'{m.value}'" for m in e)
    return f"{col} IN ({vals})"


class Recording(Base):
    __tablename__ = "recordings"
    __table_args__ = (
        UniqueConstraint("sha256"),
        UniqueConstraint("stored_filename"),
        CheckConstraint(_enum_check("status", RecordingStatus), name="status_valid"),
        CheckConstraint(_enum_check("thumbnail_status", ThumbnailStatus),
                        name="thumbnail_status_valid"),
        CheckConstraint("file_size_bytes >= 0", name="size_nonneg"),
        Index("ix_recordings_imported_at", "imported_at"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(64))
    # Path relative to the data directory (portable if the data dir moves).
    file_path: Mapped[str] = mapped_column(String(512))
    # File name inside temp/ while STAGED; NULL once finalized into recordings/.
    staging_filename: Mapped[str | None] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))
    file_size_bytes: Mapped[int] = mapped_column(BigInteger)

    container_format: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # Rational frame rate stored exactly (e.g. 30000/1001).
    fps_num: Mapped[int | None] = mapped_column(Integer)
    fps_den: Mapped[int | None] = mapped_column(Integer)
    is_variable_frame_rate: Mapped[bool | None] = mapped_column(Boolean)
    video_codec: Mapped[str | None] = mapped_column(String(64))
    audio_codec: Mapped[str | None] = mapped_column(String(64))
    has_audio: Mapped[bool | None] = mapped_column(Boolean)

    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default=RecordingStatus.STAGED.value)
    thumbnail_status: Mapped[str] = mapped_column(
        String(16), default=ThumbnailStatus.PENDING.value
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    jobs: Mapped[list[Job]] = relationship(back_populates="recording")

    @property
    def fps(self) -> float | None:
        if self.fps_num and self.fps_den:
            return self.fps_num / self.fps_den
        return None

    @property
    def fps_rational(self) -> str | None:
        if self.fps_num and self.fps_den:
            return f"{self.fps_num}/{self.fps_den}"
        return None


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # One job of each type per recording: makes enqueueing idempotent.
        UniqueConstraint("recording_id", "job_type", name="uq_jobs_recording_type"),
        CheckConstraint(_enum_check("status", JobStatus), name="status_valid"),
        CheckConstraint(_enum_check("job_type", JobType), name="job_type_valid"),
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="progress_range"),
        CheckConstraint("attempt_count >= 0", name="attempts_nonneg"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_pos"),
        Index("ix_jobs_claimable", "status", "next_attempt_at"),
        Index("ix_jobs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    recording_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("recordings.id", ondelete="CASCADE")
    )
    job_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING.value)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    # Per-attempt fencing token: every write by a running attempt must present it.
    lease_token: Mapped[str | None] = mapped_column(String(64))
    # FFmpeg/ffprobe child of the current attempt (for orphan cleanup).
    process_pid: Mapped[int | None] = mapped_column(Integer)
    process_started_at: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)

    recording: Mapped[Recording] = relationship(back_populates="jobs")


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (
        CheckConstraint(_enum_check("severity", Severity), name="severity_valid"),
        Index("ix_system_events_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    severity: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    related_job_id: Mapped[str | None] = mapped_column(
        UUIDStr, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    related_recording_id: Mapped[str | None] = mapped_column(
        UUIDStr, ForeignKey("recordings.id", ondelete="SET NULL")
    )


class Worker(Base):
    """Worker process heartbeat registry (drives the dashboard's worker status)."""

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    pid: Mapped[int] = mapped_column(Integer)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    current_job_id: Mapped[str | None] = mapped_column(UUIDStr)


# ============================================================================ storage (M1)

class LocationKind(enum.StrEnum):
    """Configurable storage locations. The database, logs and thumbnails stay internal."""

    RECORDINGS = "recordings"
    TEMP = "temp"
    MODELS = "models"
    EXPORTS = "exports"
    ASSETS = "assets"


class StorageLocation(Base):
    """Where each location kind lives. The database row is the authority; every process
    loads it at startup (``services.locations.load_layout``) before touching any file."""

    __tablename__ = "storage_locations"
    __table_args__ = (
        CheckConstraint(_enum_check("kind", LocationKind), name="kind_valid"),
        CheckConstraint("floor_bytes >= 0", name="floor_nonneg"),
    )

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024))
    # Identity written into the location's marker file; must match on every verification.
    location_uuid: Mapped[str] = mapped_column(UUIDStr)
    # Groups locations on the same physical volume (shared reservations and floor).
    volume_uuid: Mapped[str] = mapped_column(UUIDStr)
    # The OS's own volume identifier (macOS APFS VolumeUUID via diskutil), when available.
    hw_volume_uuid: Mapped[str | None] = mapped_column(String(64))
    fs_type: Mapped[str | None] = mapped_column(String(32))
    floor_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DiskReservation(Base):
    """Bytes promised to in-flight writes, per volume. Shared by the API and the worker.

    ``bytes`` is what the holder may still write. Rows expire unless renewed, so a crashed
    holder never blocks space forever.
    """

    __tablename__ = "disk_reservations"
    __table_args__ = (
        CheckConstraint("bytes >= 0", name="bytes_nonneg"),
        Index("ix_disk_reservations_volume", "volume_uuid", "expires_at"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    volume_uuid: Mapped[str] = mapped_column(UUIDStr)
    location_kind: Mapped[str] = mapped_column(String(16))
    holder: Mapped[str] = mapped_column(String(128))
    bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MigrationStatus(enum.StrEnum):
    COPYING = "COPYING"      # files being copied/verified (resumable)
    SWITCHED = "SWITCHED"    # all verified; location now points at the destination
    ABORTED = "ABORTED"      # stopped by the owner; nothing switched


class StorageMigration(Base):
    __tablename__ = "storage_migrations"
    __table_args__ = (
        CheckConstraint(_enum_check("status", MigrationStatus), name="status_valid"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(String(16))
    source_path: Mapped[str] = mapped_column(String(1024))
    dest_path: Mapped[str] = mapped_column(String(1024))
    dest_location_uuid: Mapped[str] = mapped_column(UUIDStr)
    status: Mapped[str] = mapped_column(String(16), default=MigrationStatus.COPYING.value)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class StorageMigrationItem(Base):
    """One file of a migration: the resumable journal (one row per relative path)."""

    __tablename__ = "storage_migration_items"
    __table_args__ = (
        UniqueConstraint("migration_id", "rel_path", name="uq_migration_item_path"),
        CheckConstraint("state IN ('PENDING','VERIFIED')", name="state_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("storage_migrations.id", ondelete="CASCADE"))
    rel_path: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
