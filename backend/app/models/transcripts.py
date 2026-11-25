"""M4 shared transcription models (docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md §3).

One transcript per (recording, transcription configuration). Raw words and segments are
immutable once written (triggers in migration 0010); timing corrections are a separate,
versioned layer that never touches them. All times are integer milliseconds relative to
the recording.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
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


class TranscriptStatus(enum.StrEnum):
    PENDING = "PENDING"        # created; chunks being transcribed
    COMPLETE = "COMPLETE"      # words and segments written; immutable
    FAILED = "FAILED"


class ChunkStatus(enum.StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"


def _check(col: str, e: type[enum.Enum]) -> str:
    return f"{col} IN ({','.join(repr(m.value) for m in e)})"


class TranscriptionConfig(Base):
    """An immutable transcription configuration, identified by the SHA-256 of its canonical
    JSON (engine, model id and checksum, language, chunking, decoding options, audio format,
    merge version). The engine's package version is not part of it: each chunk stores the
    version that produced it (raw output) and the transcript lists every version used, so a
    transcript resumed across an engine upgrade says so."""

    __tablename__ = "transcription_configs"
    __table_args__ = (UniqueConstraint("fingerprint"),)

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint("recording_id", "config_id"),
        CheckConstraint(_check("status", TranscriptStatus), name="status_valid"),
        CheckConstraint("coverage_ms >= 0", name="coverage_nonneg"),
        Index("ix_transcripts_recording", "recording_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    recording_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("recordings.id", ondelete="RESTRICT"), nullable=False)
    config_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("transcription_configs.id", ondelete="RESTRICT"), nullable=False)
    recording_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=TranscriptStatus.PENDING.value)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engine_version: Mapped[str | None] = mapped_column(String(64))
    job_id: Mapped[str | None] = mapped_column(UUIDStr)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TranscriptChunk(Base):
    """One transcribed audio chunk: its span (overlap included) and the engine's untouched
    output (gzip JSON). A DONE chunk is never transcribed again (resume)."""

    __tablename__ = "transcript_chunks"
    __table_args__ = (
        CheckConstraint(_check("status", ChunkStatus), name="status_valid"),
        CheckConstraint("end_ms > start_ms", name="span_valid"),
    )

    transcript_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("transcripts.id", ondelete="RESTRICT"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    core_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    core_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False,
                                        default=ChunkStatus.PENDING.value)
    audio_sha256: Mapped[str | None] = mapped_column(String(64))
    raw_json_gz: Mapped[bytes | None] = mapped_column(LargeBinary)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TranscriptWord(Base):
    __tablename__ = "transcript_words"
    __table_args__ = (CheckConstraint("end_ms >= start_ms", name="span_valid"),)

    transcript_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("transcripts.id", ondelete="RESTRICT"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    prob: Mapped[float | None] = mapped_column(Float)
    chunk_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_idx: Mapped[int | None] = mapped_column(Integer)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (CheckConstraint("end_ms >= start_ms", name="span_valid"),)

    transcript_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("transcripts.id", ondelete="RESTRICT"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    first_word_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    last_word_idx: Mapped[int] = mapped_column(Integer, nullable=False)


class TranscriptCorrection(Base):
    """A versioned timing correction. Never alters raw words; used only when ``validated``
    (and asked for). Validation needs authorized real commentary (M0 decision)."""

    __tablename__ = "transcript_corrections"
    __table_args__ = (
        UniqueConstraint("transcript_id", "version"),
        CheckConstraint("method IN ('constant_offset')", name="method_valid"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    transcript_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("transcripts.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
