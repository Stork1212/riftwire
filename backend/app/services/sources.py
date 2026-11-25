"""Creators, channels, games, brand channels and sources (M2). Business logic only; the
API layer validates shapes and maps errors. Every change writes provenance."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.db.base import utcnow
from app.models import (
    AcquisitionMethod,
    BrandChannel,
    ChannelPlatform,
    Creator,
    CreatorChannel,
    CreatorRelationship,
    Game,
    PublishPlatform,
    RightsCoverage,
    RightsRecord,
    RightsRecordChannel,
    Source,
    SourceAssignment,
    SourceKind,
    SourceStatus,
)
from app.services import matching, rights
from app.services.source_urls import ParsedSourceUrl, SourceUrlError, parse_source_url

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _name(v: str | None, what: str, n: int = 128) -> str:
    v = (v or "").strip()
    if not v or len(v) > n:
        raise ValidationFailed(f"{what} is required (at most {n} characters)")
    return v


def _slug(v: str | None) -> str:
    v = (v or "").strip().lower()
    if not SLUG.match(v):
        raise ValidationFailed("slug: lower-case letters, digits and dashes (max 63)")
    return v


def _flush_unique(s: Session, what: str) -> None:
    try:
        with s.begin_nested():
            s.flush()
    except IntegrityError:
        raise ConflictError(f"{what} already exists") from None


# --------------------------------------------------------------------------- games

def create_game(s: Session, slug: str, name: str) -> Game:
    g = Game(slug=_slug(slug), name=_name(name, "name"))
    s.add(g)
    _flush_unique(s, f"A game with slug '{g.slug}'")
    rights.provenance(s, "game", g.id, "created", slug=g.slug, name=g.name)
    return g


def update_game(s: Session, game_id: str, *, name: str | None = None,
                active: bool | None = None) -> Game:
    g = s.get(Game, game_id)
    if g is None:
        raise NotFoundError("Game not found")
    if name is not None:
        g.name = _name(name, "name")
    if active is not None:
        g.active = active
    g.updated_at = utcnow()
    rights.provenance(s, "game", g.id, "updated", name=g.name, active=g.active)
    return g


# --------------------------------------------------------------------------- brand channels

def create_brand_channel(s: Session, slug: str, name: str, platform: str,
                         handle: str | None = None, monetized: bool = False) -> BrandChannel:
    if platform not in {p.value for p in PublishPlatform}:
        raise ValidationFailed(f"unknown platform '{platform}'")
    b = BrandChannel(slug=_slug(slug), name=_name(name, "name"), platform=platform,
                     handle=(handle or "").strip() or None, monetized=bool(monetized))
    s.add(b)
    _flush_unique(s, f"A brand channel with slug '{b.slug}'")
    rights.provenance(s, "brand_channel", b.id, "created", slug=b.slug, platform=platform,
                      monetized=b.monetized)
    return b


def update_brand_channel(s: Session, bc_id: str, *, name: str | None = None,
                         handle: str | None = None, monetized: bool | None = None,
                         active: bool | None = None) -> BrandChannel:
    b = s.get(BrandChannel, bc_id)
    if b is None:
        raise NotFoundError("Brand channel not found")
    if name is not None:
        b.name = _name(name, "name")
    if handle is not None:
        b.handle = handle.strip() or None
    if monetized is not None:
        b.monetized = monetized
    if active is not None:
        b.active = active
    b.updated_at = utcnow()
    rights.provenance(s, "brand_channel", b.id, "updated", monetized=b.monetized,
                      active=b.active)
    return b


# --------------------------------------------------------------------------- creators

def create_creator(s: Session, display_name: str, relationship: str,
                   contact: str | None = None, notes: str | None = None) -> Creator:
    if relationship not in {r.value for r in CreatorRelationship}:
        raise ValidationFailed(f"unknown relationship '{relationship}'")
    c = Creator(display_name=_name(display_name, "display_name"), relationship=relationship,
                contact=(contact or "").strip()[:256] or None, notes=notes)
    s.add(c)
    s.flush()
    rights.provenance(s, "creator", c.id, "created", display_name=c.display_name,
                      relationship=relationship)
    return c


def update_creator(s: Session, creator_id: str, *, display_name: str | None = None,
                   contact: str | None = None, notes: str | None = None,
                   archived: bool | None = None) -> Creator:
    c = s.get(Creator, creator_id)
    if c is None:
        raise NotFoundError("Creator not found")
    if display_name is not None:
        c.display_name = _name(display_name, "display_name")
    if contact is not None:
        c.contact = contact.strip()[:256] or None
    if notes is not None:
        c.notes = notes
    if archived is not None:
        c.archived_at = utcnow() if archived else None
    c.updated_at = utcnow()
    rights.provenance(s, "creator", c.id, "updated", archived=c.archived_at is not None)
    return c


def add_channel(s: Session, creator_id: str, platform: str, *, handle: str | None = None,
                platform_channel_id: str | None = None) -> CreatorChannel:
    c = s.get(Creator, creator_id)
    if c is None:
        raise NotFoundError("Creator not found")
    if platform not in {p.value for p in ChannelPlatform}:
        raise ValidationFailed(f"unknown platform '{platform}'")
    handle = (handle or "").strip().lstrip("@") or None
    pid = (platform_channel_id or "").strip() or None
    if platform == "twitch" and handle:
        handle = handle.lower()
        if not re.fullmatch(r"[a-z0-9_]{4,25}", handle):
            raise ValidationFailed("Twitch login: 4–25 letters, digits or underscores")
    if platform != "local" and not handle and not pid:
        raise ValidationFailed("a handle or a channel id is required")
    url = None
    if platform == "twitch" and handle:
        url = f"https://www.twitch.tv/{handle}"
    elif platform == "youtube" and pid:
        url = f"https://www.youtube.com/channel/{pid}"
    elif platform == "youtube" and handle:
        url = f"https://www.youtube.com/@{handle}"
    ch = CreatorChannel(creator_id=creator_id, platform=platform, handle=handle,
                        platform_channel_id=pid, url_canonical=url,
                        verified_by_owner_at=utcnow())
    s.add(ch)
    _flush_unique(s, "This channel")
    rights.provenance(s, "creator", creator_id, "channel_added", channel_id=ch.id,
                      platform=platform, handle=handle, platform_channel_id=pid)
    return ch


def channel_coverage(s: Session, channel: CreatorChannel) -> list[RightsRecord]:
    """Current records that cover every source from ``channel`` (standing licences)."""
    now = utcnow()
    recs = list(s.scalars(select(RightsRecord).where(
        RightsRecord.creator_id == channel.creator_id,
        RightsRecord.coverage == RightsCoverage.CREATOR.value)))
    recs += list(s.scalars(select(RightsRecord).join(
        RightsRecordChannel, RightsRecordChannel.rights_record_id == RightsRecord.id).where(
        RightsRecordChannel.creator_channel_id == channel.id,
        RightsRecord.coverage == RightsCoverage.CHANNELS.value)))
    return [r for r in recs if rights.record_state(r, now) == rights.OK]


# --------------------------------------------------------------------------- sources

def parse_url(url: str) -> ParsedSourceUrl:
    try:
        return parse_source_url(url)
    except SourceUrlError as e:
        raise ValidationFailed(str(e), code=e.code) from None


def resolve(s: Session, url: str) -> dict:
    """Read-only: what the pasted URL is, whether it is already a source, which creator
    channel it matches and whether a standing licence covers that channel."""
    p = parse_url(url)
    m = matching.match(s, matching.Hint(parsed_url=p))
    out: dict = {"parsed": p, "match": m, "existing_source_id": m.source_id,
                 "channel": None, "covered_by": []}
    if m.creator_channel_id:
        ch = s.get(CreatorChannel, m.creator_channel_id)
        out["channel"] = ch
        out["covered_by"] = [r.id for r in channel_coverage(s, ch)]
    return out


def _check_channel(s: Session, kind: str, channel_id: str | None,
                   parsed: ParsedSourceUrl | None) -> CreatorChannel:
    if channel_id is None:
        raise ValidationFailed("Choose the creator channel this video belongs to",
                               code="creator_channel_required")
    ch = s.get(CreatorChannel, channel_id)
    if ch is None:
        raise ValidationFailed("unknown creator channel")
    if ch.platform != kind:
        raise ValidationFailed(f"a {kind} source needs a {kind} creator channel")
    if parsed is not None and parsed.channel_login and ch.handle \
            and ch.handle.lower() != parsed.channel_login.lower():
        raise ValidationFailed("the link belongs to a different Twitch channel")
    creator = s.get(Creator, ch.creator_id)
    if creator is None or creator.archived_at is not None:
        raise ConflictError("the creator is archived")
    return ch


def create_source(s: Session, *, url: str | None = None, kind: str | None = None,
                  creator_channel_id: str | None = None, game_id: str | None = None,
                  title_user: str | None = None, acquisition_method: str | None = None,
                  brand_channel_ids: list[str] | None = None,
                  actor: str = "owner") -> Source:
    """Save a source. Saving grants nothing: a saved link is classified as public footage
    (``rights.status_summary``) until a creator's permission or a licence covers it."""
    parsed = parse_url(url) if url else None
    if parsed is not None:
        kind = parsed.platform
        existing = s.scalar(select(Source).where(
            Source.url_canonical == parsed.canonical_url))
        if existing is not None:
            raise ConflictError("This video is already a source",
                                details={"source_id": existing.id})
        if creator_channel_id is None:
            m = matching.match(s, matching.Hint(parsed_url=parsed))
            if m.status == matching.MATCHED:
                creator_channel_id = m.creator_channel_id
    elif kind != SourceKind.LOCAL.value:
        raise ValidationFailed("a URL is required unless the source is local")
    ch = _check_channel(s, kind, creator_channel_id, parsed)
    method = acquisition_method or (AcquisitionMethod.LOCAL_RECORDING.value
                                    if kind == "local" else
                                    AcquisitionMethod.PLATFORM_EXPORT.value)
    if method not in {a.value for a in AcquisitionMethod}:
        raise ValidationFailed(f"unknown acquisition method '{method}'")
    if game_id is not None and s.get(Game, game_id) is None:
        raise ValidationFailed("unknown game")
    now = utcnow()
    src = Source(kind=kind, platform_video_id=parsed.video_id if parsed else None,
                 url_canonical=parsed.canonical_url if parsed else None,
                 start_hint_ms=parsed.start_ms if parsed else None,
                 creator_channel_id=ch.id, title_user=(title_user or "").strip()[:255] or None,
                 game_id=game_id, status=SourceStatus.ACTIVE.value,
                 acquisition_method=method, created_at=now, updated_at=now)
    s.add(src)
    _flush_unique(s, "This video")
    rights.provenance(s, "source", src.id, "created", actor=actor, kind=kind,
                      url=src.url_canonical, creator_channel_id=ch.id,
                      acquisition_method=method, game_id=game_id)
    if brand_channel_ids:
        set_assignments(s, src.id, brand_channel_ids, actor=actor)
    return src


def update_source(s: Session, source_id: str, *, title_user: str | None = None,
                  game_id: str | None = None, clear_game: bool = False,
                  status: str | None = None) -> Source:
    src = get_source(s, source_id)
    changes: dict = {}
    if title_user is not None:
        src.title_user = title_user.strip()[:255] or None
        changes["title_user"] = src.title_user
    if clear_game:
        src.game_id = None
        changes["game_id"] = None
    elif game_id is not None:
        if s.get(Game, game_id) is None:
            raise ValidationFailed("unknown game")
        src.game_id = game_id
        changes["game_id"] = game_id
    if status is not None:
        if status not in {x.value for x in SourceStatus}:
            raise ValidationFailed(f"unknown status '{status}'")
        src.status = status
        changes["status"] = status
    src.updated_at = utcnow()
    rights.provenance(s, "source", src.id, "updated", **changes)
    return src


def set_assignments(s: Session, source_id: str, brand_channel_ids: list[str], *,
                    actor: str = "owner") -> list[str]:
    get_source(s, source_id)
    wanted = list(dict.fromkeys(brand_channel_ids))
    for bc in wanted:
        if s.get(BrandChannel, bc) is None:
            raise ValidationFailed(f"unknown brand channel '{bc}'")
    have = {a.brand_channel_id: a for a in s.scalars(select(SourceAssignment).where(
        SourceAssignment.source_id == source_id))}
    for bc, a in have.items():
        if bc not in wanted:
            s.delete(a)
    for bc in wanted:
        if bc not in have:
            s.add(SourceAssignment(source_id=source_id, brand_channel_id=bc,
                                   created_at=utcnow()))
    s.flush()
    rights.provenance(s, "source", source_id, "assignments_set", actor=actor,
                      brand_channel_ids=wanted)
    return wanted


def get_source(s: Session, source_id: str) -> Source:
    src = s.get(Source, source_id)
    if src is None:
        raise NotFoundError("Source not found")
    return src


def local_source_for_upload(s: Session, creator_channel_id: str, title: str,
                            method: str) -> Source:
    """One-step local import: a new local source on a channel the owner chose (its
    standing licence then covers it; no per-file attestation)."""
    return create_source(s, kind=SourceKind.LOCAL.value, creator_channel_id=creator_channel_id,
                         title_user=title, acquisition_method=method)


def count_sources(s: Session) -> int:
    return int(s.scalar(select(func.count()).select_from(Source)) or 0)
