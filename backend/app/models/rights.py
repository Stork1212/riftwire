"""M2 Rights & Source Management models (docs/M2_RIGHTS_SOURCES_PLAN.md §3, §12).

Everything here is configured by the owner through the dashboard: nothing is seeded (R5).
Rights records are immutable once attested, and provenance events are append-only. Both
rules are enforced by SQLite triggers created in migration 0004, not only in code (§12.6).
"""

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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, UUIDStr, new_uuid, utcnow


def _check(col: str, e: type[enum.Enum]) -> str:
    return f"{col} IN ({','.join(repr(m.value) for m in e)})"


class PublishPlatform(enum.StrEnum):
    YOUTUBE_SHORTS = "youtube_shorts"
    INSTAGRAM_REELS = "instagram_reels"
    FACEBOOK_REELS = "facebook_reels"
    X = "x"


class ChannelPlatform(enum.StrEnum):
    """Where a creator's footage lives."""

    YOUTUBE = "youtube"
    TWITCH = "twitch"
    LOCAL = "local"


class SourceKind(enum.StrEnum):
    LOCAL = "local"
    YOUTUBE = "youtube"
    TWITCH = "twitch"


class SourceStatus(enum.StrEnum):
    ACTIVE = "active"
    BLOCKED = "blocked"      # owner blocked it: nothing may use it
    ARCHIVED = "archived"    # hidden; nothing new may use it


class AcquisitionMethod(enum.StrEnum):
    """How media for a source is (or will be) obtained (R1, §12.2). M2 implements none of
    the network methods; it only records and enforces them."""

    LOCAL_RECORDING = "local_recording"    # the owner's own recordings
    CREATOR_FILE = "creator_file"          # original files supplied by the creator
    PLATFORM_EXPORT = "platform_export"    # owner's own YouTube Studio / Twitch download
    DIRECT_LINK = "direct_link"            # creator-provided authorized media URL
    PLATFORM_API = "platform_api"          # platform-authorized acquisition
    WATCH_FOLDER = "watch_folder"          # future automation
    URL_FETCH = "url_fetch"                # generic URL fetch: needs platform permission


class CreatorRelationship(enum.StrEnum):
    SELF = "self"
    PARTNER = "partner"
    LICENSOR = "licensor"
    # No relationship: a creator whose public footage was saved by link. Says nothing
    # about permission.
    UNAFFILIATED = "unaffiliated"


class RightsBasis(enum.StrEnum):
    OWNERSHIP = "ownership"
    CREATOR_PERMISSION = "creator_permission"
    LICENCE = "licence"
    # Legacy classification written automatically by b13b91f for saved links. Kept so that
    # immutable history stays valid; it authorizes nothing (rights.PUBLIC_WEB_USES is
    # empty) and no new records use it (owner decision 2026-11-25).
    PUBLIC_WEB = "public_web"


class RightsCoverage(enum.StrEnum):
    """What a record covers: all of the creator's listed channels (a standing licence),
    selected channels, or selected sources (§12.3)."""

    CREATOR = "creator"
    CHANNELS = "channels"
    SOURCES = "sources"


class EvidenceKind(enum.StrEnum):
    EMAIL = "email"
    DOCUMENT = "document"
    PLATFORM_PERMISSION = "platform_permission"   # written permission from YouTube/Twitch
    NOTE = "note"


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BrandChannel(Base):
    """A RIFTWIRE output channel (e.g. RumbleFrame on YouTube Shorts)."""

    __tablename__ = "brand_channels"
    __table_args__ = (
        CheckConstraint(_check("platform", PublishPlatform), name="platform_valid"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(32))
    handle: Mapped[str | None] = mapped_column(String(128))
    monetized: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (
        CheckConstraint(_check("relationship", CreatorRelationship),
                        name="relationship_valid"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    display_name: Mapped[str] = mapped_column(String(128))
    relationship: Mapped[str] = mapped_column(String(16))
    contact: Mapped[str | None] = mapped_column(String(256))
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CreatorChannel(Base):
    __tablename__ = "creator_channels"
    __table_args__ = (
        CheckConstraint(_check("platform", ChannelPlatform), name="platform_valid"),
        UniqueConstraint("platform", "platform_channel_id", name="uq_channel_platform_id"),
        UniqueConstraint("platform", "handle", name="uq_channel_platform_handle"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    creator_id: Mapped[str] = mapped_column(UUIDStr, ForeignKey("creators.id"))
    platform: Mapped[str] = mapped_column(String(16))
    platform_channel_id: Mapped[str | None] = mapped_column(String(128))
    handle: Mapped[str | None] = mapped_column(String(128))
    url_canonical: Mapped[str | None] = mapped_column(String(512))
    verified_by_owner_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RightsRecord(Base):
    """One version of a rights statement. Draft until ``attested_at`` is set; after that
    its content can never change (DB trigger). Editing = a new version that supersedes it;
    revoking = a timestamp. Only the current version (attested, not superseded, not
    revoked, inside its validity window) can authorize anything (§12.4)."""

    __tablename__ = "rights_records"
    __table_args__ = (
        CheckConstraint(_check("basis", RightsBasis), name="basis_valid"),
        CheckConstraint(_check("coverage", RightsCoverage), name="coverage_valid"),
        CheckConstraint("basis != 'public_web' OR coverage = 'sources'",
                        name="public_web_per_source"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("lineage_id", "version", name="uq_rights_lineage_version"),
        Index("ix_rights_creator", "creator_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    # All versions of one statement share a lineage id.
    lineage_id: Mapped[str] = mapped_column(UUIDStr, default=new_uuid)
    version: Mapped[int] = mapped_column(Integer, default=1)
    creator_id: Mapped[str] = mapped_column(UUIDStr, ForeignKey("creators.id"))
    basis: Mapped[str] = mapped_column(String(32))
    coverage: Mapped[str] = mapped_column(String(16))
    scope_json: Mapped[str] = mapped_column(Text)
    scope_sha256: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attestation_text: Mapped[str | None] = mapped_column(Text)
    attested_by: Mapped[str | None] = mapped_column(String(64))
    attested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    superseded_by_id: Mapped[str | None] = mapped_column(
        UUIDStr, ForeignKey("rights_records.id"))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RightsRecordChannel(Base):
    """Channels covered by a record with coverage ``channels``."""

    __tablename__ = "rights_record_channels"

    rights_record_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("rights_records.id"), primary_key=True)
    creator_channel_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("creator_channels.id"), primary_key=True)


class RightsSourceLink(Base):
    """Sources covered by a record with coverage ``sources``."""

    __tablename__ = "rights_source_links"

    rights_record_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("rights_records.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        UUIDStr, ForeignKey("sources.id"), primary_key=True)


class RightsEvidence(Base):
    """Supporting documentation for a rights record. Never proof by itself: attaching
    evidence never changes a record's status (§12.6). Append-only (DB trigger)."""

    __tablename__ = "rights_evidence"
    __table_args__ = (
        CheckConstraint(_check("kind", EvidenceKind), name="kind_valid"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_nonneg"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    rights_record_id: Mapped[str] = mapped_column(UUIDStr, ForeignKey("rights_records.id"))
    kind: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    stored_name: Mapped[str | None] = mapped_column(String(64))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Source(Base):
    """One external video or local recording origin."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(_check("kind", SourceKind), name="kind_valid"),
        CheckConstraint(_check("status", SourceStatus), name="status_valid"),
        CheckConstraint(_check("acquisition_method", AcquisitionMethod),
                        name="acquisition_method_valid"),
        UniqueConstraint("url_canonical", name="uq_sources_url"),
        Index("ix_sources_channel", "creator_channel_id"),
    )

    id: Mapped[str] = mapped_column(UUIDStr, primary_key=True, default=new_uuid)
    kind: Mapped[str] = mapped_column(String(16))
    platform_video_id: Mapped[str | None] = mapped_column(String(128))
    url_canonical: Mapped[str | None] = mapped_column(String(512))
    start_hint_ms: Mapped[int | None] = mapped_column(BigInteger)
    creator_channel_id: Mapped[str | None] = mapped_column(
        UUIDStr, ForeignKey("creator_channels.id"))
    title_user: Mapped[str | None] = mapped_column(String(255))
    game_id: Mapped[str | None] = mapped_column(UUIDStr, ForeignKey("games.id"))
    status: Mapped[str] = mapped_column(String(16), default=SourceStatus.ACTIVE.value)
    acquisition_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SourceAssignment(Base):
    """Which RIFTWIRE brand channels may use material from a source."""

    __tablename__ = "source_assignments"

    source_id: Mapped[str] = mapped_column(UUIDStr, ForeignKey("sources.id"),
                                           primary_key=True)
    brand_channel_id: Mapped[str] = mapped_column(UUIDStr, ForeignKey("brand_channels.id"),
                                                  primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ProvenanceEvent(Base):
    """Append-only history of sources, rights, evidence, assignments and decisions.
    UPDATE and DELETE are rejected by DB triggers."""

    __tablename__ = "provenance_events"
    __table_args__ = (
        CheckConstraint("actor IN ('owner','system')", name="actor_valid"),
        Index("ix_provenance_entity", "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(16))
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
