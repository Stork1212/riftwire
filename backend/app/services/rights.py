"""Rights evaluation and enforcement (M2, docs/M2_RIGHTS_SOURCES_PLAN.md §5, §12).

Fail closed, always:

* Only the **current** version of a rights record can authorize: attested, not
  superseded, not revoked, and inside its validity window (§12.4).
* Scope is parsed strictly (unknown keys or versions make the record unusable).
* Every constraint the scope carries is enforced, or the action is refused where RIFTWIRE
  cannot enforce it (territories).
* Attribution is returned as an *obligation* the consumer must confirm it satisfied.

Enforcement points (§5): the API, job enqueue, the worker before a gated job runs, and a
**transactional fence** at every output boundary: completion of a gated job re-checks, inside
the same ``BEGIN IMMEDIATE`` transaction that records the output, that the record the job
ran under is still current. Revocation and supersession are also ``BEGIN IMMEDIATE``
writes, so SQLite serializes the two: an output either lands before a revocation or is
refused (§12.5).

Records and provenance are immutable in the database itself (migration 0004 triggers).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, RightsRefused, ValidationFailed
from app.db.base import utcnow
from app.db.session import Database
from app.models import (
    AcquisitionMethod,
    BrandChannel,
    Creator,
    CreatorChannel,
    EvidenceKind,
    JobType,
    ProvenanceEvent,
    PublishPlatform,
    Recording,
    RightsBasis,
    RightsCoverage,
    RightsEvidence,
    RightsRecord,
    RightsRecordChannel,
    RightsSourceLink,
    Source,
    SourceAssignment,
    SourceStatus,
)

Use = Literal["acquire", "analyze", "edit", "publish"]
USES: tuple[str, ...] = ("acquire", "analyze", "edit", "publish")
# The only uses a public_web record can authorize, whatever its scope says.
PUBLIC_WEB_USES: frozenset[str] = frozenset({"acquire", "analyze"})
PUBLIC_WEB_ATTESTATION = ("Publicly accessible footage, recorded automatically when the link "
                          "was saved. Not creator-confirmed: covers fetching and analysis "
                          "only, never editing or publishing.")

# Which use each job type needs. ``None`` = technical housekeeping, not rights-gated (R2).
# A test fails if a job type is missing here, so a new gated job type cannot be added
# without declaring its use.
JOB_USE: dict[JobType, str | None] = {
    JobType.INGEST: None,
    JobType.METADATA: None,
    JobType.THUMBNAIL: None,
    # Gated, but at the source level: the job has no recording yet. ``enqueue_acquire`` and
    # ``url_download.acquire`` check ``acquire`` by ``url_fetch`` before queueing, before
    # the download, after it, and again in the transaction that links the recording.
    JobType.ACQUIRE: None,
    JobType.HIGHLIGHT_SCAN: "analyze",
}

# Reason codes (stable; API, UI, events).
OK = "ok"
NO_SOURCE = "no_source"
SOURCE_BLOCKED = "source_blocked"
NOT_ATTESTED_MEDIA = "media_not_linked_to_source"
NO_RIGHTS_RECORD = "no_rights_record"
NOT_ATTESTED = "not_attested"
SUPERSEDED = "superseded"
REVOKED = "revoked"
EXPIRED = "expired"
NOT_YET_VALID = "not_yet_valid"
CREATOR_ARCHIVED = "creator_archived"
SCOPE_UNSUPPORTED = "scope_unsupported"
USE_NOT_IN_SCOPE = "use_not_in_scope"
METHOD_NOT_PERMITTED = "acquisition_method_not_permitted"
METHOD_UNKNOWN = "acquisition_method_unknown"
PLATFORM_PERMISSION_MISSING = "platform_permission_missing"
URL_ACQUISITION_DISABLED = "url_acquisition_disabled"
PLATFORM_NOT_IN_SCOPE = "platform_not_in_scope"
BRAND_CHANNEL_REQUIRED = "brand_channel_required"
BRAND_CHANNEL_NOT_ASSIGNED = "brand_channel_not_assigned"
BRAND_CHANNEL_NOT_IN_SCOPE = "brand_channel_not_in_scope"
MONETIZATION_NOT_PERMITTED = "monetization_not_permitted"
CLIP_LENGTH_REQUIRED = "clip_length_required"
CLIP_TOO_LONG = "clip_too_long"
TERRITORIES_UNSUPPORTED = "territories_unsupported"
CREATOR_PERMISSION_NEEDED = "creator_permission_needed"
RIGHTS_CHANGED = "rights_changed"
OBLIGATION_UNSATISFIED = "obligation_unsatisfied"

REASON_TEXT = {
    OK: "authorized",
    NO_SOURCE: "the source does not exist",
    SOURCE_BLOCKED: "the source is blocked or archived",
    NOT_ATTESTED_MEDIA: "this recording is not linked to a source with rights",
    NO_RIGHTS_RECORD: "no rights record covers this source",
    NOT_ATTESTED: "the rights record has not been attested",
    SUPERSEDED: "the rights record was superseded by a newer version",
    REVOKED: "the rights were revoked",
    EXPIRED: "the rights have expired",
    NOT_YET_VALID: "the rights are not valid yet",
    CREATOR_ARCHIVED: "the creator is archived",
    SCOPE_UNSUPPORTED: "the rights scope is not understood (unsupported version or field)",
    USE_NOT_IN_SCOPE: "this use is not covered by the rights",
    METHOD_NOT_PERMITTED: "this acquisition method is not permitted by the rights",
    METHOD_UNKNOWN: "how this recording's footage was obtained is not on record",
    PLATFORM_PERMISSION_MISSING: "URL fetching needs written platform permission on record",
    CREATOR_PERMISSION_NEEDED: "this footage is only on record as public web footage; "
                               "nobody has given permission to edit or publish it",
    URL_ACQUISITION_DISABLED: "URL fetching is switched off",
    PLATFORM_NOT_IN_SCOPE: "publishing to this platform is not covered",
    BRAND_CHANNEL_REQUIRED: "a brand channel must be specified",
    BRAND_CHANNEL_NOT_ASSIGNED: "the source is not assigned to this brand channel",
    BRAND_CHANNEL_NOT_IN_SCOPE: "the rights do not cover this brand channel",
    MONETIZATION_NOT_PERMITTED: "the rights do not allow monetized use",
    CLIP_LENGTH_REQUIRED: "the clip length must be known (the rights limit clip length)",
    CLIP_TOO_LONG: "the clip is longer than the rights allow",
    TERRITORIES_UNSUPPORTED: "the rights are limited to territories RIFTWIRE cannot enforce",
    RIGHTS_CHANGED: "the rights changed while the job ran",
    OBLIGATION_UNSATISFIED: "an obligation of the rights was not satisfied",
}

# What the owner can do about each refusal (shown in the API and the dashboard).
REASON_ACTION = {
    NO_SOURCE: "Check the link or pick an existing source.",
    SOURCE_BLOCKED: "Reactivate the source on its page if it was blocked by mistake.",
    NOT_ATTESTED_MEDIA: "Link the recording to a source (Import with a rights link, or "
                        "attach it on the source's page).",
    NO_RIGHTS_RECORD: "Add a licence for this creator on Creators & Rights.",
    NOT_ATTESTED: "Attest the draft licence on Creators & Rights.",
    SUPERSEDED: "A newer version exists; it is used automatically if it covers this.",
    REVOKED: "Attest a new licence on Creators & Rights if permission was granted again.",
    EXPIRED: "Attest a renewed licence on Creators & Rights.",
    NOT_YET_VALID: "Wait for the licence's start date, or correct it on Creators & Rights.",
    CREATOR_ARCHIVED: "Unarchive the creator on Creators & Rights.",
    SCOPE_UNSUPPORTED: "Edit the licence on Creators & Rights to save a supported version.",
    USE_NOT_IN_SCOPE: "Edit the licence on Creators & Rights to allow this use.",
    METHOD_NOT_PERMITTED: "Use a permitted way of obtaining the footage, or edit the licence "
                          "to allow this method.",
    METHOD_UNKNOWN: "On the source's page, record how this recording's footage was obtained.",
    PLATFORM_PERMISSION_MISSING: "Attach written platform permission as evidence first.",
    CREATOR_PERMISSION_NEEDED: "Ask the creator; when they agree, record their permission "
                               "on Creators & Rights.",
    URL_ACQUISITION_DISABLED: "URL fetching is not available in this version.",
    PLATFORM_NOT_IN_SCOPE: "Edit the licence to allow this platform.",
    BRAND_CHANNEL_REQUIRED: "Choose the brand channel.",
    BRAND_CHANNEL_NOT_ASSIGNED: "Assign the source to this brand channel on its page.",
    BRAND_CHANNEL_NOT_IN_SCOPE: "Edit the licence to include this brand channel.",
    MONETIZATION_NOT_PERMITTED: "Use a non-monetized channel, or edit the licence.",
    CLIP_LENGTH_REQUIRED: "Specify the clip length.",
    CLIP_TOO_LONG: "Shorten the clip to the licence's maximum.",
    TERRITORIES_UNSUPPORTED: "Remove the territory limit only if the licence really allows "
                             "worldwide use.",
    RIGHTS_CHANGED: "Check the licence, then retry the job.",
    OBLIGATION_UNSATISFIED: "Satisfy the licence's obligations (e.g. attribution).",
}

SPECIFICITY = {RightsCoverage.SOURCES.value: 0, RightsCoverage.CHANNELS.value: 1,
               RightsCoverage.CREATOR.value: 2}


# --------------------------------------------------------------------------- scope

class Scope(BaseModel):
    """Version-1 scope of a rights record. Unknown keys are rejected (fail closed)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1]
    uses: list[Use] = Field(min_length=1)
    acquisition_methods: list[AcquisitionMethod] = Field(default_factory=list)
    publish_platforms: list[PublishPlatform] = Field(default_factory=list)
    brand_channels: list[str] | Literal["*"] = Field(default_factory=list)
    monetization: bool = False
    attribution_required: str | None = Field(default=None, max_length=500)
    max_clip_seconds: int | None = Field(default=None, gt=0, le=36_000)
    territories: list[str] | None = None
    notes: str = Field(default="", max_length=2000)

    @field_validator("uses", "acquisition_methods", "publish_platforms")
    @classmethod
    def _unique(cls, v: list) -> list:
        if len(set(v)) != len(v):
            raise ValueError("duplicate entries")
        return v

    @field_validator("attribution_required")
    @classmethod
    def _attr(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v


def canonical_scope(scope: Scope) -> str:
    return json.dumps(scope.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def parse_scope(raw: str) -> Scope | None:
    try:
        return Scope.model_validate_json(raw)
    except (ValidationError, ValueError):
        return None


# --------------------------------------------------------------------------- decisions

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    use: str
    source_id: str | None
    record_id: str | None = None
    lineage_id: str | None = None
    version: int | None = None
    scope_sha256: str | None = None
    obligations: tuple[dict, ...] = ()
    params: dict = field(default_factory=dict)

    @property
    def message(self) -> str:
        return REASON_TEXT.get(self.reason, self.reason)

    @property
    def action(self) -> str | None:
        return None if self.allowed else REASON_ACTION.get(self.reason)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["obligations"] = list(self.obligations)
        d["message"] = self.message
        d["action"] = self.action
        return d

    def refuse(self) -> RightsRefused:
        msg = f"Not authorized: {self.message}."
        if self.action:
            msg += f" {self.action}"
        return RightsRefused(msg, details={"reason": self.reason, "use": self.use,
                                           "source_id": self.source_id,
                                           "record_id": self.record_id,
                                           "action": self.action})


def record_state(r: RightsRecord, at: datetime) -> str:
    if r.attested_at is None:
        return NOT_ATTESTED
    if r.revoked_at is not None:
        return REVOKED
    if r.superseded_by_id is not None:
        return SUPERSEDED
    if r.valid_from > at:
        return NOT_YET_VALID
    if r.valid_until is not None and r.valid_until <= at:
        return EXPIRED
    return OK


def covering_records(s: Session, source: Source) -> list[RightsRecord]:
    """Every record (any state) whose coverage includes ``source``: records naming the
    source (coverage ``sources``), records naming its channel (``channels``), and standing
    licences for its creator (``creator``)."""
    ids: set[str] = set(s.scalars(
        select(RightsSourceLink.rights_record_id)
        .join(RightsRecord, RightsRecord.id == RightsSourceLink.rights_record_id)
        .where(RightsSourceLink.source_id == source.id,
               RightsRecord.coverage == RightsCoverage.SOURCES.value)))
    if source.creator_channel_id is not None:
        ch = s.get(CreatorChannel, source.creator_channel_id)
        if ch is not None:
            ids |= set(s.scalars(select(RightsRecord.id).where(
                RightsRecord.creator_id == ch.creator_id,
                RightsRecord.coverage == RightsCoverage.CREATOR.value)))
            ids |= set(s.scalars(
                select(RightsRecordChannel.rights_record_id)
                .join(RightsRecord, RightsRecord.id == RightsRecordChannel.rights_record_id)
                .where(RightsRecordChannel.creator_channel_id == ch.id,
                       RightsRecord.coverage == RightsCoverage.CHANNELS.value)))
    if not ids:
        return []
    return list(s.scalars(select(RightsRecord).where(RightsRecord.id.in_(ids))))


def _has_platform_permission(s: Session, record_id: str) -> bool:
    return s.scalar(select(RightsEvidence.id).where(
        RightsEvidence.rights_record_id == record_id,
        RightsEvidence.kind == EvidenceKind.PLATFORM_PERMISSION.value).limit(1)) is not None


def _check_record(s: Session, r: RightsRecord, scope: Scope, source: Source, use: str, *,
                  platform: str | None, brand_channel_id: str | None, clip_ms: int | None,
                  method: str | None, media_method: str | None,
                  url_acquisition_enabled: bool) -> str:
    if use not in scope.uses:
        return USE_NOT_IN_SCOPE
    # A public_web record covers only footage RIFTWIRE fetched itself, so its source-level
    # answers are about fetched media; a recording is always judged by its own method.
    declared = (AcquisitionMethod.URL_FETCH.value if r.basis == RightsBasis.PUBLIC_WEB.value
                else source.acquisition_method)
    if use == "acquire":
        m = method or declared
        if m not in scope.acquisition_methods:
            return METHOD_NOT_PERMITTED
        if m == AcquisitionMethod.URL_FETCH.value:
            if not url_acquisition_enabled:
                return URL_ACQUISITION_DISABLED
            # Public footage may be fetched (owner acquisition policy, 2026-11-24); other
            # bases keep the written-platform-permission requirement (R1).
            if (r.basis != RightsBasis.PUBLIC_WEB.value
                    and not _has_platform_permission(s, r.id)):
                return PLATFORM_PERMISSION_MISSING
        return OK
    # Media must have been obtained by a method these rights permit (R1). For a recording
    # this is its actual method, always known here (``evaluate_recording`` refuses when it
    # is not); the source's declared method applies only to source-level questions.
    if (media_method or declared) not in scope.acquisition_methods:
        return METHOD_NOT_PERMITTED
    if use in ("edit", "publish"):
        if scope.max_clip_seconds is not None:
            if clip_ms is None:
                return CLIP_LENGTH_REQUIRED
            if clip_ms > scope.max_clip_seconds * 1000:
                return CLIP_TOO_LONG
        if brand_channel_id is None and use == "publish":
            return BRAND_CHANNEL_REQUIRED
        if brand_channel_id is not None:
            bc = s.get(BrandChannel, brand_channel_id)
            if bc is None or not bc.active:
                return BRAND_CHANNEL_NOT_ASSIGNED
            if s.get(SourceAssignment, (source.id, brand_channel_id)) is None:
                return BRAND_CHANNEL_NOT_ASSIGNED
            if scope.brand_channels != "*" and brand_channel_id not in scope.brand_channels:
                return BRAND_CHANNEL_NOT_IN_SCOPE
            if bc.monetized and not scope.monetization:
                return MONETIZATION_NOT_PERMITTED
    if use == "publish":
        if platform is None or platform not in scope.publish_platforms:
            return PLATFORM_NOT_IN_SCOPE
        bc = s.get(BrandChannel, brand_channel_id)
        if bc is not None and bc.platform != platform:
            return PLATFORM_NOT_IN_SCOPE
        if scope.territories is not None:
            return TERRITORIES_UNSUPPORTED
    return OK


REFUSAL_PRIORITY = (REVOKED, EXPIRED, NOT_YET_VALID, SUPERSEDED, NOT_ATTESTED)


def evaluate(s: Session, source_id: str | None, use: str, *, platform: str | None = None,
             brand_channel_id: str | None = None, clip_ms: int | None = None,
             method: str | None = None, at: datetime | None = None,
             url_acquisition_enabled: bool = False,
             media_method: str | None = None) -> Decision:
    """Pure read: may ``use`` happen for ``source_id`` now? Never raises for a refusal."""
    if use not in USES:
        raise ValidationFailed(f"unknown use '{use}'")
    at = at or utcnow()
    params = {k: v for k, v in (("platform", platform), ("brand_channel_id", brand_channel_id),
                                ("clip_ms", clip_ms), ("method", method),
                                ("media_method", media_method)) if v is not None}
    if url_acquisition_enabled and use == "acquire" and method == "url_fetch":
        params["url_acquisition_enabled"] = True

    def no(reason: str, r: RightsRecord | None = None) -> Decision:
        return Decision(False, reason, use, source_id, r.id if r else None,
                        r.lineage_id if r else None, r.version if r else None,
                        r.scope_sha256 if r else None, (), params)

    source = s.get(Source, source_id) if source_id else None
    if source is None:
        return no(NO_SOURCE)
    if source.status != SourceStatus.ACTIVE.value:
        return no(SOURCE_BLOCKED)
    recs = covering_records(s, source)
    if not recs:
        return no(NO_RIGHTS_RECORD)
    states = {r.id: record_state(r, at) for r in recs}
    active = sorted((r for r in recs if states[r.id] == OK),
                    key=lambda r: (SPECIFICITY[r.coverage], -r.attested_at.timestamp()))
    if not active:
        for reason in REFUSAL_PRIORITY:
            hit = [r for r in recs if states[r.id] == reason]
            if hit:
                return no(reason, max(hit, key=lambda r: r.created_at))
        return no(NO_RIGHTS_RECORD)
    first_refusal: Decision | None = None
    for r in active:
        scope: Scope | None = None
        creator = s.get(Creator, r.creator_id)
        if r.basis == RightsBasis.PUBLIC_WEB.value and use not in PUBLIC_WEB_USES:
            reason = CREATOR_PERMISSION_NEEDED        # whatever the scope says
        elif creator is None or creator.archived_at is not None:
            reason = CREATOR_ARCHIVED
        else:
            scope = parse_scope(r.scope_json)
            reason = SCOPE_UNSUPPORTED if scope is None else _check_record(
                s, r, scope, source, use, platform=platform,
                brand_channel_id=brand_channel_id, clip_ms=clip_ms, method=method,
                media_method=media_method, url_acquisition_enabled=url_acquisition_enabled)
        if reason == OK:
            assert scope is not None
            obligations: list[dict] = []
            if scope.attribution_required and use == "publish":
                obligations.append({"type": "attribution", "text": scope.attribution_required})
            return Decision(True, OK, use, source.id, r.id, r.lineage_id, r.version,
                            r.scope_sha256, tuple(obligations), params)
        # The public_web record's "ask the creator" is the least informative answer: a
        # creator's permission or licence explains its own refusal better.
        if first_refusal is None or (first_refusal.reason == CREATOR_PERMISSION_NEEDED
                                     and r.basis != RightsBasis.PUBLIC_WEB.value):
            first_refusal = no(reason, r)
    assert first_refusal is not None
    if first_refusal.reason == CREATOR_PERMISSION_NEEDED and all(
            r.basis == RightsBasis.PUBLIC_WEB.value for r in active):
        # Only public footage is left. If a creator's permission or licence existed and
        # lapsed, say so: "revoked" is more useful than "ask the creator".
        for reason in REFUSAL_PRIORITY:
            hit = [r for r in recs if states[r.id] == reason
                   and r.basis != RightsBasis.PUBLIC_WEB.value]
            if hit:
                return no(reason, max(hit, key=lambda r: r.created_at))
    return first_refusal


def evaluate_recording(s: Session, recording_id: str, use: str, **kw) -> Decision:
    rec = s.get(Recording, recording_id)
    if rec is None or rec.source_id is None:
        return Decision(False, NOT_ATTESTED_MEDIA, use, None)
    kw.setdefault("media_method", rec.acquisition_method)
    if kw["media_method"] is None and use != "acquire":
        # Fail closed: never fall back to the source's declared method for real media
        # (independent audit of 63a2e5e, finding 2).
        return Decision(False, METHOD_UNKNOWN, use, rec.source_id)
    return evaluate(s, rec.source_id, use, **kw)


def require(s: Session, source_id: str | None, use: str, **kw) -> Decision:
    d = evaluate(s, source_id, use, **kw)
    if not d.allowed:
        raise d.refuse()
    return d


def require_obligations(decision: Decision, satisfied: Iterable[str]) -> None:
    """A consumer confirms which obligation types it satisfied. Any obligation not
    confirmed (including types this code does not know) refuses the action."""
    done = set(satisfied)
    missing = [o["type"] for o in decision.obligations if o.get("type") not in done]
    if missing:
        raise RightsRefused(f"Obligations not satisfied: {', '.join(missing)}",
                            details={"reason": OBLIGATION_UNSATISFIED, "missing": missing})


# --------------------------------------------------------------------------- fence

def fence(s: Session, decision: Decision, at: datetime | None = None) -> None:
    """Inside the transaction that records an output: the record the work was authorized
    under must still be the current version and still allow the same use with the same
    parameters. Raises ``RightsRefused`` (``rights_changed`` or the new reason)."""
    if not decision.allowed or decision.record_id is None:
        raise decision.refuse()
    r = s.get(RightsRecord, decision.record_id)
    if r is None or record_state(r, at or utcnow()) != OK:
        reason = record_state(r, at or utcnow()) if r is not None else NO_RIGHTS_RECORD
        raise RightsRefused(f"Rights changed while the work ran: {REASON_TEXT[reason]}",
                            details={"reason": RIGHTS_CHANGED, "now": reason,
                                     "record_id": decision.record_id})
    again = evaluate(s, decision.source_id, decision.use, at=at, **decision.params)
    if not again.allowed or again.record_id != decision.record_id \
            or again.scope_sha256 != decision.scope_sha256:
        raise RightsRefused("Rights changed while the work ran",
                            details={"reason": RIGHTS_CHANGED, "now": again.reason,
                                     "record_id": decision.record_id})


def authorize_output(db: Database, decision: Decision,
                     write: Callable[[Session], None]) -> None:
    """Run ``write`` (DB writes and/or a no-overwrite ``link`` of a staged file) inside one
    ``BEGIN IMMEDIATE`` transaction, after the fence. If the fence refuses, ``write`` never
    runs."""
    with db.session() as s:
        fence(s, decision)
        write(s)


# --------------------------------------------------------------------------- provenance

def provenance(s: Session, entity: str, entity_id: str, action: str, *,
               actor: str = "owner", **details: object) -> None:
    s.add(ProvenanceEvent(entity=entity, entity_id=str(entity_id), action=action, actor=actor,
                          details_json=json.dumps(details, sort_keys=True, default=str),
                          at=utcnow()))


# --------------------------------------------------------------------------- lifecycle

def _scope_from(data: dict | Scope) -> Scope:
    if isinstance(data, Scope):
        return data
    try:
        return Scope.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed("Invalid rights scope",
                               details={"errors": e.errors(include_url=False)}) from None


def _check_scope_refs(s: Session, scope: Scope) -> None:
    if scope.brand_channels != "*":
        for bc in scope.brand_channels:
            if s.get(BrandChannel, bc) is None:
                raise ValidationFailed(f"unknown brand channel '{bc}'")


def _link_coverage(s: Session, r: RightsRecord, channel_ids: list[str],
                   source_ids: list[str]) -> None:
    if r.coverage == RightsCoverage.CHANNELS.value:
        if not channel_ids:
            raise ValidationFailed("coverage 'channels' needs at least one channel")
        for cid in dict.fromkeys(channel_ids):
            ch = s.get(CreatorChannel, cid)
            if ch is None or ch.creator_id != r.creator_id:
                raise ValidationFailed(f"channel '{cid}' does not belong to this creator")
            s.add(RightsRecordChannel(rights_record_id=r.id, creator_channel_id=cid))
    elif channel_ids:
        raise ValidationFailed("channels can only be listed with coverage 'channels'")
    if r.coverage == RightsCoverage.SOURCES.value:
        if not source_ids:
            raise ValidationFailed("coverage 'sources' needs at least one source")
        for sid in dict.fromkeys(source_ids):
            src = s.get(Source, sid)
            if src is None:
                raise ValidationFailed(f"unknown source '{sid}'")
            ch = s.get(CreatorChannel, src.creator_channel_id) \
                if src.creator_channel_id else None
            if ch is None or ch.creator_id != r.creator_id:
                # A creator can only license their own footage (review finding B).
                raise ValidationFailed(f"source '{sid}' does not belong to this creator")
            s.add(RightsSourceLink(rights_record_id=r.id, source_id=sid))
    elif source_ids:
        raise ValidationFailed("sources can only be listed with coverage 'sources'")


def create_record(s: Session, *, creator_id: str, basis: str, coverage: str,
                  scope: dict | Scope, valid_from: datetime | None = None,
                  valid_until: datetime | None = None, channel_ids: list[str] = (),
                  source_ids: list[str] = (), attestation_text: str | None = None,
                  attest: bool = True, lineage_id: str | None = None,
                  version: int = 1) -> RightsRecord:
    if basis == RightsBasis.PUBLIC_WEB.value:
        raise ValidationFailed("public_web records are written automatically when a link is "
                               "saved; record a creator's permission or a licence instead",
                               code="public_web_automatic")
    creator = s.get(Creator, creator_id)
    if creator is None:
        raise NotFoundError("Creator not found")
    if creator.archived_at is not None:
        raise ConflictError("The creator is archived")
    if basis not in {b.value for b in RightsBasis}:
        raise ValidationFailed(f"unknown basis '{basis}'")
    if coverage not in {c.value for c in RightsCoverage}:
        raise ValidationFailed(f"unknown coverage '{coverage}'")
    sc = _scope_from(scope)
    _check_scope_refs(s, sc)
    now = utcnow()
    vf = valid_from or now
    if valid_until is not None and valid_until <= vf:
        raise ValidationFailed("valid_until must be after valid_from")
    text = canonical_scope(sc)
    r = RightsRecord(creator_id=creator_id, basis=basis, coverage=coverage, scope_json=text,
                     scope_sha256=hashlib.sha256(text.encode()).hexdigest(), valid_from=vf,
                     valid_until=valid_until, created_at=now, version=version)
    if lineage_id:
        r.lineage_id = lineage_id
    s.add(r)
    s.flush()
    _link_coverage(s, r, list(channel_ids), list(source_ids))
    s.flush()
    provenance(s, "rights", r.id, "created", lineage_id=r.lineage_id, version=r.version,
               basis=basis, coverage=coverage, scope_sha256=r.scope_sha256)
    if attest:
        attest_record(s, r, attestation_text)
    return r


def record_public_web(s: Session, source: Source) -> RightsRecord | None:
    """The automatic, honest record for a saved link: public footage that may be fetched
    and analysed. Written by ``system`` in the caller's transaction; covers this one source
    and names its creator, claiming nothing from them. Idempotent: returns
    the source's existing public_web record (any state, so a revoked one is never silently
    replaced). Sources without a URL get none."""
    if source.url_canonical is None:
        return None
    existing = s.scalar(
        select(RightsRecord).join(RightsSourceLink,
                                  RightsSourceLink.rights_record_id == RightsRecord.id)
        .where(RightsSourceLink.source_id == source.id,
               RightsRecord.basis == RightsBasis.PUBLIC_WEB.value))
    if existing is not None:
        return existing
    ch = s.get(CreatorChannel, source.creator_channel_id)
    if ch is None:
        return None
    sc = Scope(v=1, uses=["acquire", "analyze"],
               acquisition_methods=[AcquisitionMethod.URL_FETCH.value],
               notes="Recorded automatically for a saved link. Not creator-confirmed.")
    text = canonical_scope(sc)
    now = utcnow()
    r = RightsRecord(creator_id=ch.creator_id,
                     basis=RightsBasis.PUBLIC_WEB.value,
                     coverage=RightsCoverage.SOURCES.value, scope_json=text,
                     scope_sha256=hashlib.sha256(text.encode()).hexdigest(), valid_from=now,
                     created_at=now, version=1)
    s.add(r)
    s.flush()
    s.add(RightsSourceLink(rights_record_id=r.id, source_id=source.id))
    s.flush()
    r.attestation_text = PUBLIC_WEB_ATTESTATION
    r.attested_by = "system"
    r.attested_at = now
    s.flush()
    provenance(s, "rights", r.id, "recorded_public_web", actor="system", source_id=source.id,
               basis=r.basis, coverage=r.coverage, scope_sha256=r.scope_sha256)
    return r


def attest_record(s: Session, r: RightsRecord, attestation_text: str | None) -> None:
    """The owner's explicit statement activates a record. Evidence never does (§12.6)."""
    if r.attested_at is not None:
        raise ConflictError("Already attested")
    if not attestation_text or len(attestation_text.strip()) < 10:
        raise ValidationFailed("An attestation statement (at least 10 characters) is "
                               "required to activate rights")
    r.attestation_text = attestation_text.strip()
    r.attested_by = "owner"
    r.attested_at = utcnow()
    s.flush()
    provenance(s, "rights", r.id, "attested", version=r.version,
               scope_sha256=r.scope_sha256)


def current_record(s: Session, record_id: str) -> RightsRecord:
    r = s.get(RightsRecord, record_id)
    if r is None:
        raise NotFoundError("Rights record not found")
    return r


def supersede(s: Session, record_id: str, *, scope: dict | Scope, attestation_text: str,
              basis: str | None = None, valid_from: datetime | None = None,
              valid_until: datetime | None = None, channel_ids: list[str] | None = None,
              source_ids: list[str] | None = None) -> RightsRecord:
    """Edit = a new attested version; the old one is marked superseded in the same
    transaction. Jobs authorized under the old version fail at their output fence."""
    old = current_record(s, record_id)
    if old.basis == RightsBasis.PUBLIC_WEB.value:
        raise ValidationFailed("A public_web record cannot be edited into a licence; record "
                               "the creator's permission as a new record instead.",
                               code="public_web_automatic")
    if old.attested_at is None or old.revoked_at is not None or old.superseded_by_id:
        raise ConflictError("Only the current, attested, unrevoked version can be edited")
    if channel_ids is None:
        channel_ids = list(s.scalars(select(RightsRecordChannel.creator_channel_id).where(
            RightsRecordChannel.rights_record_id == old.id)))
    if source_ids is None:
        source_ids = list(s.scalars(select(RightsSourceLink.source_id).where(
            RightsSourceLink.rights_record_id == old.id)))
    new = create_record(s, creator_id=old.creator_id, basis=basis or old.basis,
                        coverage=old.coverage, scope=scope, valid_from=valid_from,
                        valid_until=valid_until, channel_ids=channel_ids,
                        source_ids=source_ids, attestation_text=attestation_text,
                        attest=True, lineage_id=old.lineage_id, version=old.version + 1)
    old.superseded_by_id = new.id
    s.flush()
    provenance(s, "rights", old.id, "superseded", by=new.id, new_version=new.version)
    return new


def revoke(s: Session, record_id: str, reason: str) -> RightsRecord:
    r = current_record(s, record_id)
    if r.attested_at is None:
        raise ConflictError("A draft cannot be revoked; it never authorized anything")
    if r.revoked_at is not None:
        raise ConflictError("Already revoked")
    if not reason or not reason.strip():
        raise ValidationFailed("A reason is required")
    r.revoked_at = utcnow()
    r.revoked_reason = reason.strip()
    s.flush()
    provenance(s, "rights", r.id, "revoked", reason=r.revoked_reason)
    return r


def status_summary(s: Session, source: Source, at: datetime | None = None,
                   soon: timedelta = timedelta(days=14)) -> str:
    """Licence state for badges: active | expires_soon | public_web | unusable | expired |
    revoked | not_attested | not_yet_valid | none | blocked. ``public_web``: only the
    automatic record for a saved link is in force (fetch and analyse only).

    "active" only says a usable licence is in force; which uses it authorizes is
    reported separately (``authorized_uses``), because a licence may allow acquiring but
    not publishing (review finding C)."""
    at = at or utcnow()
    if source.status != SourceStatus.ACTIVE.value:
        return "blocked"
    recs = covering_records(s, source)
    if not recs:
        return "none"
    states = [(r, record_state(r, at)) for r in recs]
    active = [r for r, st in states if st == OK]
    public = [r for r in active if r.basis == RightsBasis.PUBLIC_WEB.value
              and parse_scope(r.scope_json) is not None]
    usable = [r for r in active if r.basis != RightsBasis.PUBLIC_WEB.value
              and parse_scope(r.scope_json) is not None
              and (c := s.get(Creator, r.creator_id)) is not None and c.archived_at is None]
    if not usable and public:
        return "public_web"          # fetching and analysis only; nobody agreed to more
    if usable:
        if all(r.valid_until is not None and r.valid_until - at <= soon for r in usable):
            return "expires_soon"
        return "active"
    if active:
        return "unusable"
    names = {REVOKED: "revoked", EXPIRED: "expired", NOT_ATTESTED: "not_attested",
             NOT_YET_VALID: "not_yet_valid", SUPERSEDED: "none"}
    for reason in REFUSAL_PRIORITY:
        if any(st == reason for _, st in states):
            return names[reason]
    return "none"


def authorized_uses(s: Session, source: Source, *,
                    url_acquisition_enabled: bool = False) -> list[str]:
    """Uses allowed without per-clip parameters. Edit and publish also depend on the clip,
    platform and brand channel, so they are checked when those are known, never shown as
    blanket permissions."""
    out = []
    if evaluate(s, source.id, "acquire",
                url_acquisition_enabled=url_acquisition_enabled).allowed:
        out.append("acquire")
    if evaluate(s, source.id, "analyze").allowed:
        out.append("analyze")
    return out
