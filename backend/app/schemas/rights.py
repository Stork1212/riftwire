"""M2 API models: creators, channels, games, brand channels, sources, rights, evidence,
provenance. Inputs reject unknown fields; outputs never include filesystem paths."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.api import RecordingOut


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- inputs

class GameIn(In):
    slug: str = Field(max_length=63)
    name: str = Field(max_length=128)


class GamePatch(In):
    name: str | None = Field(default=None, max_length=128)
    active: bool | None = None


class BrandChannelIn(In):
    slug: str = Field(max_length=63)
    name: str = Field(max_length=128)
    platform: str
    handle: str | None = Field(default=None, max_length=128)
    monetized: bool = False


class BrandChannelPatch(In):
    name: str | None = Field(default=None, max_length=128)
    handle: str | None = Field(default=None, max_length=128)
    monetized: bool | None = None
    active: bool | None = None


class CreatorIn(In):
    display_name: str = Field(max_length=128)
    relationship: str
    contact: str | None = Field(default=None, max_length=256)
    notes: str | None = Field(default=None, max_length=5000)


class CreatorPatch(In):
    display_name: str | None = Field(default=None, max_length=128)
    contact: str | None = Field(default=None, max_length=256)
    notes: str | None = Field(default=None, max_length=5000)
    archived: bool | None = None


class ChannelIn(In):
    platform: str
    handle: str | None = Field(default=None, max_length=128)
    platform_channel_id: str | None = Field(default=None, max_length=128)


class RightsIn(In):
    creator_id: str
    basis: str
    coverage: str
    scope: dict[str, Any]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    channel_ids: list[str] = Field(default_factory=list, max_length=100)
    source_ids: list[str] = Field(default_factory=list, max_length=1000)
    attestation_text: str | None = Field(default=None, max_length=5000)
    attest: bool = True


class AttestIn(In):
    attestation_text: str = Field(max_length=5000)


class SupersedeIn(In):
    scope: dict[str, Any]
    attestation_text: str = Field(max_length=5000)
    basis: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    channel_ids: list[str] | None = Field(default=None, max_length=100)
    source_ids: list[str] | None = Field(default=None, max_length=1000)


class RevokeIn(In):
    reason: str = Field(min_length=1, max_length=2000)


class ResolveIn(In):
    url: str = Field(max_length=4096)


class SourceIn(In):
    url: str | None = Field(default=None, max_length=4096)
    kind: str | None = None
    creator_channel_id: str | None = None
    game_id: str | None = None
    title_user: str | None = Field(default=None, max_length=255)
    acquisition_method: str | None = None
    brand_channel_ids: list[str] = Field(default_factory=list, max_length=50)


class SourcePatch(In):
    title_user: str | None = Field(default=None, max_length=255)
    game_id: str | None = None
    clear_game: bool = False
    status: str | None = None


class AssignmentsIn(In):
    brand_channel_ids: list[str] = Field(max_length=50)


class AttachIn(In):
    recording_id: str
    acquisition_method: str | None = None


# --------------------------------------------------------------------------- outputs

class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class GameOut(Out):
    id: str
    slug: str
    name: str
    active: bool
    created_at: datetime


class BrandChannelOut(Out):
    id: str
    slug: str
    name: str
    platform: str
    handle: str | None
    monetized: bool
    active: bool
    created_at: datetime


class CreatorChannelOut(Out):
    id: str
    creator_id: str
    platform: str
    handle: str | None
    platform_channel_id: str | None
    url_canonical: str | None
    verified_by_owner_at: datetime | None
    created_at: datetime


class CreatorOut(Out):
    id: str
    display_name: str
    relationship: str
    contact: str | None
    notes: str | None
    archived: bool
    created_at: datetime
    updated_at: datetime
    channels: list[CreatorChannelOut] = Field(default_factory=list)
    current_rights: int = 0


class EvidenceOut(Out):
    id: str
    rights_record_id: str
    kind: str
    reference: str | None
    original_filename: str | None
    has_file: bool
    sha256: str | None
    size_bytes: int | None
    created_at: datetime
    file_url: str | None


class RightsRecordOut(Out):
    id: str
    lineage_id: str
    version: int
    creator_id: str
    basis: str
    coverage: str
    scope: dict[str, Any] | None
    scope_sha256: str
    valid_from: datetime
    valid_until: datetime | None
    attestation_text: str | None
    attested_at: datetime | None
    superseded_by_id: str | None
    revoked_at: datetime | None
    revoked_reason: str | None
    created_at: datetime
    state: str
    channel_ids: list[str]
    source_ids: list[str]
    evidence: list[EvidenceOut] = Field(default_factory=list)


class DecisionOut(BaseModel):
    allowed: bool
    reason: str
    message: str
    use: str
    source_id: str | None
    record_id: str | None
    version: int | None
    obligations: list[dict[str, Any]]
    action: str | None = None


class SourceOut(Out):
    id: str
    kind: str
    platform_video_id: str | None
    url_canonical: str | None
    start_hint_ms: int | None
    creator_channel_id: str | None
    creator_id: str | None
    creator_name: str | None
    channel_label: str | None
    title_user: str | None
    game_id: str | None
    game_name: str | None
    status: str
    acquisition_method: str
    created_at: datetime
    updated_at: datetime
    rights_status: str
    authorized_uses: list[str]
    brand_channel_ids: list[str]
    recording_count: int


class SourceDetail(SourceOut):
    analyze: DecisionOut
    acquire: DecisionOut
    records: list[RightsRecordOut]
    recordings: list[RecordingOut]


class MatchOut(BaseModel):
    status: str
    matcher: str | None
    reason: str
    creator_channel_id: str | None
    candidates: list[str]


class ResolveOut(BaseModel):
    platform: str
    media: str
    video_id: str
    canonical_url: str
    start_ms: int | None
    channel_login: str | None
    existing_source: SourceOut | None
    match: MatchOut
    channel: CreatorChannelOut | None
    creator_name: str | None
    covered: bool
    covering_record_ids: list[str]


class ProvenanceOut(Out):
    id: int
    entity: str
    entity_id: str
    action: str
    actor: str
    details: dict[str, Any]
    at: datetime


class AcquisitionMethodOut(BaseModel):
    method: str
    available: bool
    description: str
