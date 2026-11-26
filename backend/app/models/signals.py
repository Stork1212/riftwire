"""M4 audio and visual signal models (docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md §3, §5, §6).

One signal run per (recording, kind, configuration). A complete run (its compressed series,
its measured and inferred events and its keyframes) is immutable (triggers in migration
0011). All times are integer milliseconds relative to the recording, taken from the
decoder's presentation timestamps (variable frame rate safe).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, UUIDStr, new_uuid, utcnow


class SignalKind(enum.StrEnum):
    AUDIO = "audio"
    VISUAL = "visual"


class SignalStatus(enum.StrEnum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"


def _check(col: str, e: type[enum.Enum]) -> str:
    return f"{col} IN ({','.join(repr(m.value) for m in e)})"


class SignalRun(Base):
    __tablename__ = "signal_runs"
    __table_args__ = (
        UniqueConstraint("recording_id", "kind", "config_fingerprint"),
        CheckConstraint(_check("kind", SignalKind), name="kind_valid"),
        CheckConstraint(_check("status", SignalStatus), name="status_valid"),
        Index("ix_signal_runs_recording", "recording_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    recording_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("recordings.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    recording_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=SignalStatus.PENDING.value)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    series_gz: Mapped[bytes | None] = mapped_column(LargeBinary)
    events_json: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(UUIDStr)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Keyframe(Base):
    __tablename__ = "keyframes"
    __table_args__ = (
        UniqueConstraint("signal_run_id", "idx"),
        UniqueConstraint("stored_filename"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    signal_run_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("signal_runs.id", ondelete="RESTRICT"), nullable=False)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    pts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    scene_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
