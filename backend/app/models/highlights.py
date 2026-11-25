"""M5/M6 AI highlight models: requests, AI runs and candidates.

* ``highlight_requests``: "find N highlights in this recording for this game and channel",
  with the prompt and scoring profile it resolved to. Processed by the ``AI_HIGHLIGHTS``
  job; the latest request of a recording is the one the job works on.
* ``ai_runs``: one row per provider call batch, with everything needed to reproduce and
  audit it (provider identity and digest, prompt id/version/SHA-256, output contract,
  evidence fingerprint, every attempt's metrics and errors, the full last raw answer,
  cost 0). A finished run is immutable (trigger).
* ``highlight_candidates``: every validated candidate, kept or rejected, with its
  component scores, editorial score (an estimate of editorial quality, **not** a
  probability of going viral), model confidence, evidence references and the reason it was
  rejected. Times come from stored evidence ids, never from the model.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, UUIDStr, new_uuid, utcnow


class RequestStatus(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"          # finished; may hold fewer clips than requested
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"      # a newer request for the recording replaced it


class RunStatus(enum.StrEnum):
    ACCEPTED = "accepted"
    FAILED_VALIDATION = "failed_validation"
    PROVIDER_ERROR = "provider_error"
    CANCELLED = "cancelled"


class CandidateStatus(enum.StrEnum):
    SELECTED = "selected"
    REJECTED = "rejected"


class ReviewStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _check(col: str, e: type[enum.Enum]) -> str:
    return f"{col} IN ({','.join(repr(m.value) for m in e)})"


class HighlightRequest(Base):
    __tablename__ = "highlight_requests"
    __table_args__ = (
        CheckConstraint(_check("status", RequestStatus), name="status_valid"),
        CheckConstraint("target_count BETWEEN 1 AND 20", name="target_count_range"),
        Index("ix_highlight_requests_recording", "recording_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    recording_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("recordings.id", ondelete="RESTRICT"), nullable=False)
    game: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str | None] = mapped_column(String(64))
    prompt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=RequestStatus.PENDING.value)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    summary_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(UUIDStr)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AIRun(Base):
    __tablename__ = "ai_runs"
    __table_args__ = (
        CheckConstraint(_check("status", RunStatus), name="status_valid"),
        CheckConstraint("cost_usd = 0", name="cost_zero"),
        Index("ix_ai_runs_request", "request_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("highlight_requests.id", ondelete="RESTRICT"), nullable=False)
    batch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_digest: Mapped[str | None] = mapped_column(String(128))
    identity_json: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    record_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_last: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    wall_s: Mapped[float] = mapped_column(Float, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class HighlightCandidate(Base):
    __tablename__ = "highlight_candidates"
    __table_args__ = (
        CheckConstraint(_check("status", CandidateStatus), name="status_valid"),
        CheckConstraint(_check("review_status", ReviewStatus), name="review_status_valid"),
        CheckConstraint("end_ms > start_ms", name="span_valid"),
        CheckConstraint("editorial_score BETWEEN 0 AND 100", name="score_range"),
        Index("ix_highlight_candidates_request", "request_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("highlight_requests.id", ondelete="RESTRICT"), nullable=False)
    ai_run_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("ai_runs.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    window_id: Mapped[str] = mapped_column(String(32), nullable=False)
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    start_id: Mapped[str] = mapped_column(String(96), nullable=False)
    end_id: Mapped[str] = mapped_column(String(96), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    hook: Mapped[str] = mapped_column(String(160), nullable=False)
    what_happens: Mapped[str] = mapped_column(Text, nullable=False)
    why_entertaining: Mapped[str] = mapped_column(Text, nullable=False)
    scores_json: Mapped[str] = mapped_column(Text, nullable=False)
    editorial_score: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    uncertainty: Mapped[str] = mapped_column(Text, nullable=False, default="")
    transcript_id: Mapped[str | None] = mapped_column(UUIDStr)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False,
                                               default=ReviewStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
