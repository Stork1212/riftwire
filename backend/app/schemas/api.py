"""API response models. Absolute filesystem paths are never included."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    recording_id: str
    recording_filename: str | None = None
    job_type: str
    status: str
    progress_percent: int
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    last_error: str | None
    retryable: bool = False


class RecordingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    original_filename: str
    stored_filename: str
    sha256: str
    file_size_bytes: int
    container_format: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    fps_rational: str | None
    is_variable_frame_rate: bool | None
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool | None
    imported_at: datetime
    status: str
    thumbnail_status: str
    error_message: str | None
    thumbnail_url: str
    source_id: str | None = None          # M2: NULL = rights not attested yet
    acquisition_method: str | None = None  # M2: how linked media was obtained; NULL = unknown


class RecordingDetail(RecordingOut):
    jobs: list[JobOut]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    severity: str
    component: str
    event_type: str
    message: str
    related_job_id: str | None
    related_recording_id: str | None


class ImportResponse(BaseModel):
    recording: RecordingOut
    job_id: str
    # M2: set when the import was linked to a source; ``source_error`` explains a refusal
    # that happened after the bytes were received (the recording is kept, unlinked).
    source_id: str | None = None
    source_error: str | None = None
