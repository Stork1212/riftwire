"""M2 HTTP layer: creators, channels, games, brand channels, sources, rights, evidence and
provenance. Parsing, status codes and serialization only; logic lives in services/."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.routes import CFG, DB, Limit, Offset, rec_out, require_schema
from app.core.errors import NotFoundError, ValidationFailed
from app.db.base import utcnow
from app.models import (
    BrandChannel,
    Creator,
    CreatorChannel,
    Game,
    ProvenanceEvent,
    Recording,
    RightsEvidence,
    RightsRecord,
    RightsRecordChannel,
    RightsSourceLink,
    Source,
    SourceAssignment,
)
from app.schemas.api import AcquireOut, Page
from app.schemas.rights import (
    AcquisitionMethodOut,
    AssignmentsIn,
    AttachIn,
    AttestIn,
    BrandChannelIn,
    BrandChannelOut,
    BrandChannelPatch,
    ChannelIn,
    CreatorChannelOut,
    CreatorIn,
    CreatorOut,
    CreatorPatch,
    DecisionOut,
    EvidenceOut,
    GameIn,
    GameOut,
    GamePatch,
    MatchOut,
    ProvenanceOut,
    ResolveIn,
    ResolveOut,
    RevokeIn,
    RightsIn,
    RightsRecordOut,
    SourceDetail,
    SourceIn,
    SourceOut,
    SourcePatch,
    SupersedeIn,
)
from app.services import acquisition, evidence, rights, sources, url_download
from app.services.sources import channel_coverage

m2 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_schema)])

EVIDENCE_CHUNK = 256 * 1024


# --------------------------------------------------------------------------- serializers

def page(items: list, total: int, limit: int, offset: int) -> dict:
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def decision_out(d: rights.Decision) -> DecisionOut:
    return DecisionOut(allowed=d.allowed, reason=d.reason, message=d.message, use=d.use,
                       source_id=d.source_id, record_id=d.record_id, version=d.version,
                       obligations=list(d.obligations), action=d.action)


def creator_out(s: Session, c: Creator) -> CreatorOut:
    chans = list(s.scalars(select(CreatorChannel).where(CreatorChannel.creator_id == c.id)
                           .order_by(CreatorChannel.created_at)))
    now_ok = sum(1 for r in s.scalars(select(RightsRecord).where(
        RightsRecord.creator_id == c.id)) if rights.record_state(
        r, utcnow()) == rights.OK)
    return CreatorOut(id=c.id, display_name=c.display_name, relationship=c.relationship,
                      contact=c.contact, notes=c.notes, archived=c.archived_at is not None,
                      created_at=c.created_at, updated_at=c.updated_at,
                      channels=[CreatorChannelOut.model_validate(x) for x in chans],
                      current_rights=now_ok)


def evidence_out(e: RightsEvidence) -> EvidenceOut:
    return EvidenceOut(
        id=e.id, rights_record_id=e.rights_record_id, kind=e.kind, reference=e.reference,
        original_filename=e.original_filename, has_file=e.stored_name is not None,
        sha256=e.sha256, size_bytes=e.size_bytes, created_at=e.created_at,
        file_url=(f"/api/v1/rights/{e.rights_record_id}/evidence/{e.id}/file"
                  if e.stored_name else None))


def record_out(s: Session, r: RightsRecord) -> RightsRecordOut:
    scope = rights.parse_scope(r.scope_json)
    return RightsRecordOut(
        id=r.id, lineage_id=r.lineage_id, version=r.version, creator_id=r.creator_id,
        basis=r.basis, coverage=r.coverage,
        scope=scope.model_dump(mode="json") if scope else None,
        scope_sha256=r.scope_sha256, valid_from=r.valid_from, valid_until=r.valid_until,
        attestation_text=r.attestation_text, attested_at=r.attested_at,
        superseded_by_id=r.superseded_by_id, revoked_at=r.revoked_at,
        revoked_reason=r.revoked_reason, created_at=r.created_at,
        state=rights.record_state(r, utcnow()),
        channel_ids=list(s.scalars(select(RightsRecordChannel.creator_channel_id).where(
            RightsRecordChannel.rights_record_id == r.id))),
        source_ids=list(s.scalars(select(RightsSourceLink.source_id).where(
            RightsSourceLink.rights_record_id == r.id))),
        evidence=[evidence_out(e) for e in s.scalars(select(RightsEvidence).where(
            RightsEvidence.rights_record_id == r.id).order_by(RightsEvidence.created_at))])


def source_out(s: Session, src: Source, cls=SourceOut, **extra):
    ch = s.get(CreatorChannel, src.creator_channel_id) if src.creator_channel_id else None
    creator = s.get(Creator, ch.creator_id) if ch else None
    game = s.get(Game, src.game_id) if src.game_id else None
    label = None
    if ch is not None:
        label = f"{ch.platform}: {ch.handle or ch.platform_channel_id or 'local'}"
    return cls(
        id=src.id, kind=src.kind, platform_video_id=src.platform_video_id,
        url_canonical=src.url_canonical, start_hint_ms=src.start_hint_ms,
        creator_channel_id=src.creator_channel_id, creator_id=creator.id if creator else None,
        creator_name=creator.display_name if creator else None, channel_label=label,
        title_user=src.title_user, game_id=src.game_id, game_name=game.name if game else None,
        status=src.status, acquisition_method=src.acquisition_method,
        created_at=src.created_at, updated_at=src.updated_at,
        rights_status=rights.status_summary(s, src),
        authorized_uses=rights.authorized_uses(s, src),
        brand_channel_ids=list(s.scalars(select(SourceAssignment.brand_channel_id).where(
            SourceAssignment.source_id == src.id))),
        recording_count=int(s.scalar(select(func.count()).select_from(Recording).where(
            Recording.source_id == src.id)) or 0),
        **extra)


def provenance_out(p: ProvenanceEvent) -> ProvenanceOut:
    try:
        details = json.loads(p.details_json or "{}")
    except ValueError:
        details = {}
    return ProvenanceOut(id=p.id, entity=p.entity, entity_id=p.entity_id, action=p.action,
                         actor=p.actor, details=details, at=p.at)


# --------------------------------------------------------------------------- games

@m2.get("/games", response_model=list[GameOut])
def list_games(db: DB):
    with db.session() as s:
        return [GameOut.model_validate(g) for g in s.scalars(select(Game).order_by(Game.name))]


@m2.post("/games", response_model=GameOut, status_code=201)
def create_game(body: GameIn, db: DB):
    with db.session() as s:
        return GameOut.model_validate(sources.create_game(s, body.slug, body.name))


@m2.patch("/games/{game_id}", response_model=GameOut)
def patch_game(game_id: str, body: GamePatch, db: DB):
    with db.session() as s:
        return GameOut.model_validate(sources.update_game(s, game_id, **body.model_dump()))


# --------------------------------------------------------------------------- brand channels

@m2.get("/brand-channels", response_model=list[BrandChannelOut])
def list_brand_channels(db: DB):
    with db.session() as s:
        return [BrandChannelOut.model_validate(b)
                for b in s.scalars(select(BrandChannel).order_by(BrandChannel.name))]


@m2.post("/brand-channels", response_model=BrandChannelOut, status_code=201)
def create_brand_channel(body: BrandChannelIn, db: DB):
    with db.session() as s:
        return BrandChannelOut.model_validate(sources.create_brand_channel(
            s, body.slug, body.name, body.platform, body.handle, body.monetized))


@m2.patch("/brand-channels/{bc_id}", response_model=BrandChannelOut)
def patch_brand_channel(bc_id: str, body: BrandChannelPatch, db: DB):
    with db.session() as s:
        return BrandChannelOut.model_validate(
            sources.update_brand_channel(s, bc_id, **body.model_dump()))


# --------------------------------------------------------------------------- creators

@m2.get("/creators", response_model=Page[CreatorOut])
def list_creators(db: DB, limit: Limit = 100, offset: Offset = 0,
                  include_archived: bool = False):
    with db.session() as s:
        q = select(Creator)
        if not include_archived:
            q = q.where(Creator.archived_at.is_(None))
        total = s.scalar(select(func.count()).select_from(q.subquery()))
        rows = s.scalars(q.order_by(Creator.display_name).limit(limit).offset(offset))
        return page([creator_out(s, c) for c in rows], total, limit, offset)


@m2.post("/creators", response_model=CreatorOut, status_code=201)
def create_creator(body: CreatorIn, db: DB):
    with db.session() as s:
        c = sources.create_creator(s, body.display_name, body.relationship, body.contact,
                                   body.notes)
        return creator_out(s, c)


@m2.get("/creators/{creator_id}", response_model=CreatorOut)
def get_creator(creator_id: str, db: DB):
    with db.session() as s:
        c = s.get(Creator, creator_id)
        if c is None:
            raise NotFoundError("Creator not found")
        return creator_out(s, c)


@m2.patch("/creators/{creator_id}", response_model=CreatorOut)
def patch_creator(creator_id: str, body: CreatorPatch, db: DB):
    with db.session() as s:
        return creator_out(s, sources.update_creator(s, creator_id, **body.model_dump()))


@m2.post("/creators/{creator_id}/channels", response_model=CreatorChannelOut,
         status_code=201)
def add_channel(creator_id: str, body: ChannelIn, db: DB):
    with db.session() as s:
        return CreatorChannelOut.model_validate(sources.add_channel(
            s, creator_id, body.platform, handle=body.handle,
            platform_channel_id=body.platform_channel_id))


@m2.get("/creators/{creator_id}/rights", response_model=list[RightsRecordOut])
def creator_rights(creator_id: str, db: DB):
    with db.session() as s:
        if s.get(Creator, creator_id) is None:
            raise NotFoundError("Creator not found")
        rows = s.scalars(select(RightsRecord).where(RightsRecord.creator_id == creator_id)
                         .order_by(RightsRecord.created_at.desc()))
        return [record_out(s, r) for r in rows]


# --------------------------------------------------------------------------- rights

@m2.post("/rights", response_model=RightsRecordOut, status_code=201)
def create_rights(body: RightsIn, db: DB):
    with db.session() as s:
        r = rights.create_record(
            s, creator_id=body.creator_id, basis=body.basis, coverage=body.coverage,
            scope=body.scope, valid_from=body.valid_from, valid_until=body.valid_until,
            channel_ids=body.channel_ids, source_ids=body.source_ids,
            attestation_text=body.attestation_text, attest=body.attest)
        return record_out(s, r)


@m2.get("/rights/{record_id}", response_model=RightsRecordOut)
def get_rights(record_id: str, db: DB):
    with db.session() as s:
        return record_out(s, rights.current_record(s, record_id))


@m2.post("/rights/{record_id}/attest", response_model=RightsRecordOut)
def attest_rights(record_id: str, body: AttestIn, db: DB):
    with db.session() as s:
        r = rights.current_record(s, record_id)
        rights.attest_record(s, r, body.attestation_text)
        return record_out(s, r)


@m2.post("/rights/{record_id}/supersede", response_model=RightsRecordOut, status_code=201)
def supersede_rights(record_id: str, body: SupersedeIn, db: DB):
    with db.session() as s:
        r = rights.supersede(s, record_id, scope=body.scope,
                             attestation_text=body.attestation_text, basis=body.basis,
                             valid_from=body.valid_from, valid_until=body.valid_until,
                             channel_ids=body.channel_ids, source_ids=body.source_ids)
        return record_out(s, r)


@m2.post("/rights/{record_id}/revoke", response_model=RightsRecordOut)
def revoke_rights(record_id: str, body: RevokeIn, db: DB):
    with db.session() as s:
        return record_out(s, rights.revoke(s, record_id, body.reason))


@m2.post("/rights/{record_id}/evidence", response_model=EvidenceOut, status_code=201)
async def add_evidence(record_id: str, request: Request, db: DB, cfg: CFG,
                       kind: Annotated[str, Query(max_length=32)],
                       reference: Annotated[str | None, Query(max_length=2000)] = None,
                       filename: Annotated[str | None, Query(max_length=255)] = None):
    """Optional file as the raw request body (like recording imports), streamed."""
    raw = request.headers.get("content-length")
    if raw is None and "chunked" in request.headers.get("transfer-encoding", "").lower():
        raise ValidationFailed("Evidence files need a Content-Length header")
    try:
        length = int(raw) if raw else 0
    except ValueError:
        raise ValidationFailed("Invalid Content-Length header") from None
    if length == 0 or length > cfg.evidence_max_file_bytes:   # no file, or refused unread
        ev = await run_in_threadpool(evidence.add_evidence, db, cfg, record_id, kind=kind,
                                     reference=reference, filename=filename,
                                     content_length=length, chunks=())
        return evidence_out(ev)
    body = bytearray()
    async for chunk in request.stream():   # ≤ 25 MB by the check above
        body += chunk
        if len(body) > cfg.evidence_max_file_bytes:
            raise ValidationFailed("The file is larger than its declared length")
    data = bytes(body)
    chunks = (data[i:i + EVIDENCE_CHUNK] for i in range(0, len(data), EVIDENCE_CHUNK))
    ev = await run_in_threadpool(evidence.add_evidence, db, cfg, record_id, kind=kind,
                                 reference=reference, filename=filename,
                                 content_length=length, chunks=chunks)
    return evidence_out(ev)


@m2.get("/rights/{record_id}/evidence/{evidence_id}/file")
def evidence_file(record_id: str, evidence_id: str, db: DB, cfg: CFG):
    with db.session() as s:
        ev = s.get(RightsEvidence, evidence_id)
        if ev is None or ev.rights_record_id != record_id:
            raise NotFoundError("Evidence not found")
        path = evidence.evidence_file(cfg, ev)
        name = ev.original_filename or ev.stored_name
    return FileResponse(path, filename=name)


# --------------------------------------------------------------------------- sources

@m2.get("/acquisition-methods", response_model=list[AcquisitionMethodOut])
def acquisition_methods(cfg: CFG):
    return [AcquisitionMethodOut(method=p.method,
                                 available=acquisition.is_available(p.method, cfg),
                                 description=p.description)
            for p in acquisition.PROVIDERS.values()]


@m2.post("/sources/resolve", response_model=ResolveOut)
def resolve_source(body: ResolveIn, db: DB):
    """Read-only: identify a pasted URL, find an existing source or a matching creator
    channel, and say whether a current licence covers that channel."""
    with db.session() as s:
        r = sources.resolve(s, body.url)
        p, m, ch = r["parsed"], r["match"], r["channel"]
        existing = s.get(Source, m.source_id) if m.source_id else None
        creator = s.get(Creator, ch.creator_id) if ch else None
        return ResolveOut(
            platform=p.platform, media=p.media, video_id=p.video_id,
            canonical_url=p.canonical_url, start_ms=p.start_ms, channel_login=p.channel_login,
            existing_source=source_out(s, existing) if existing else None,
            match=MatchOut(status=m.status, matcher=m.matcher, reason=m.reason,
                           creator_channel_id=m.creator_channel_id,
                           candidates=list(m.candidates)),
            channel=CreatorChannelOut.model_validate(ch) if ch else None,
            creator_name=creator.display_name if creator else None,
            covered=bool(r["covered_by"]), covering_record_ids=r["covered_by"])


@m2.get("/sources", response_model=Page[SourceOut])
def list_sources(db: DB, limit: Limit = 50, offset: Offset = 0,
                 status: str | None = None, creator_id: str | None = None,
                 game_id: str | None = None):
    with db.session() as s:
        q = select(Source)
        if status:
            q = q.where(Source.status == status)
        if game_id:
            q = q.where(Source.game_id == game_id)
        if creator_id:
            q = q.join(CreatorChannel, CreatorChannel.id == Source.creator_channel_id).where(
                CreatorChannel.creator_id == creator_id)
        total = s.scalar(select(func.count()).select_from(q.subquery()))
        rows = list(s.scalars(q.order_by(Source.created_at.desc()).limit(limit)
                              .offset(offset)))
        return page([source_out(s, x) for x in rows], total, limit, offset)


@m2.post("/sources", response_model=SourceOut, status_code=201)
def create_source(body: SourceIn, db: DB):
    with db.session() as s:
        src = sources.create_source(
            s, url=body.url, kind=body.kind, creator_channel_id=body.creator_channel_id,
            game_id=body.game_id, title_user=body.title_user,
            acquisition_method=body.acquisition_method,
            brand_channel_ids=body.brand_channel_ids)
        return source_out(s, src)


@m2.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str, db: DB, cfg: CFG):
    with db.session() as s:
        src = sources.get_source(s, source_id)
        recs = sorted(rights.covering_records(s, src), key=lambda r: r.created_at,
                      reverse=True)
        recordings = list(s.scalars(select(Recording).where(Recording.source_id == src.id)
                                    .order_by(Recording.imported_at.desc())))
        return source_out(
            s, src, SourceDetail,
            analyze=decision_out(rights.evaluate(s, src.id, "analyze")),
            acquire=decision_out(rights.evaluate(
                s, src.id, "acquire", url_acquisition_enabled=cfg.url_acquisition_enabled)),
            records=[record_out(s, r) for r in recs],
            recordings=[rec_out(r) for r in recordings])


@m2.patch("/sources/{source_id}", response_model=SourceOut)
def patch_source(source_id: str, body: SourcePatch, db: DB):
    with db.session() as s:
        return source_out(s, sources.update_source(s, source_id, **body.model_dump()))


@m2.put("/sources/{source_id}/assignments", response_model=SourceOut)
def put_assignments(source_id: str, body: AssignmentsIn, db: DB):
    with db.session() as s:
        sources.set_assignments(s, source_id, body.brand_channel_ids)
        return source_out(s, sources.get_source(s, source_id))


@m2.post("/sources/{source_id}/acquire", response_model=AcquireOut)
def acquire_source(source_id: str, db: DB, cfg: CFG):
    """Fetch the saved link and import it as a recording linked to this source."""
    r = url_download.acquire(db, cfg, source_id)
    with db.session() as s:
        rec = s.get(Recording, r.recording_id)
        return AcquireOut(recording=rec_out(rec), source_id=r.source_id, job_id=r.job_id,
                          already_acquired=r.already_acquired)


@m2.post("/sources/{source_id}/recordings", response_model=SourceOut)
def attach_recording(source_id: str, body: AttachIn, db: DB, cfg: CFG):
    with db.session() as s:
        acquisition.attach_media(s, cfg, source_id, body.recording_id,
                                 body.acquisition_method)
        return source_out(s, sources.get_source(s, source_id))


@m2.get("/sources/{source_id}/rights-check", response_model=DecisionOut)
def rights_check(source_id: str, db: DB, cfg: CFG,
                 use: Annotated[str, Query(pattern="^(acquire|analyze|edit|publish)$")],
                 platform: str | None = None, brand_channel_id: str | None = None,
                 clip_ms: Annotated[int | None, Query(ge=0)] = None,
                 method: str | None = None):
    with db.session() as s:
        sources.get_source(s, source_id)
        return decision_out(rights.evaluate(
            s, source_id, use, platform=platform, brand_channel_id=brand_channel_id,
            clip_ms=clip_ms, method=method,
            url_acquisition_enabled=cfg.url_acquisition_enabled))


@m2.get("/sources/{source_id}/provenance", response_model=list[ProvenanceOut])
def source_provenance(source_id: str, db: DB):
    """The source's chain: the source itself, its recordings, the rights records that cover
    it, and its creator."""
    with db.session() as s:
        src = sources.get_source(s, source_id)
        keys: list[tuple[str, str]] = [("source", src.id)]
        keys += [("recording", rid) for rid in s.scalars(
            select(Recording.id).where(Recording.source_id == src.id))]
        keys += [("rights", r.id) for r in rights.covering_records(s, src)]
        if src.creator_channel_id:
            ch = s.get(CreatorChannel, src.creator_channel_id)
            keys.append(("creator", ch.creator_id))
        rows: list[ProvenanceEvent] = []
        for entity, eid in keys:
            rows += list(s.scalars(select(ProvenanceEvent).where(
                ProvenanceEvent.entity == entity, ProvenanceEvent.entity_id == eid)))
        rows.sort(key=lambda p: p.id)
        return [provenance_out(p) for p in rows]


@m2.get("/creator-channels/{channel_id}/coverage", response_model=list[RightsRecordOut])
def channel_rights(channel_id: str, db: DB):
    with db.session() as s:
        ch = s.get(CreatorChannel, channel_id)
        if ch is None:
            raise NotFoundError("Channel not found")
        return [record_out(s, r) for r in channel_coverage(s, ch)]
