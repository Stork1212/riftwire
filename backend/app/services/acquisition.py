"""Provider-independent acquisition contract (M2, owner decision R1, plan §12.2).

Every way media can reach RIFTWIRE is an ``AcquisitionProvider`` keyed by its method.
Nothing in the source, rights or job model assumes a person uploads: an upload, a
watch-folder scanner or an official-API fetcher all end in the same ``attach_media`` call.

Two independent checks gate every acquisition:
1. content rights: the source's current rights allow the ``acquire`` use;
2. method permission: those rights list this acquisition method (``url_fetch``
   additionally needs written platform permission on record and the global switch).

The upload-fed providers are always available. ``url_fetch`` is available only when the
owner switches URL acquisition on, and only RIFTWIRE's own fetcher
(``services/url_download``) may record it: an uploaded file can never be labelled as
fetched. The others are declared and refuse cleanly with ``acquisition_unavailable``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError, ValidationFailed
from app.models import AcquisitionMethod, Recording, Source
from app.services import rights


class AcquisitionUnavailable(AppError):
    status_code = 501
    code = "acquisition_unavailable"


@dataclass(frozen=True)
class AcquisitionProvider:
    method: str
    available: bool
    description: str


PROVIDERS: dict[str, AcquisitionProvider] = {p.method: p for p in (
    AcquisitionProvider(AcquisitionMethod.LOCAL_RECORDING.value, True,
                        "The owner's own recordings, imported through the dashboard."),
    AcquisitionProvider(AcquisitionMethod.CREATOR_FILE.value, True,
                        "Original files supplied by the creator, imported through the "
                        "dashboard."),
    AcquisitionProvider(AcquisitionMethod.PLATFORM_EXPORT.value, True,
                        "The channel owner's own YouTube Studio / Twitch download, imported "
                        "through the dashboard."),
    AcquisitionProvider(AcquisitionMethod.DIRECT_LINK.value, False,
                        "Creator-provided media link (not implemented in M2)."),
    AcquisitionProvider(AcquisitionMethod.PLATFORM_API.value, False,
                        "Platform-authorized acquisition (not implemented in M2)."),
    AcquisitionProvider(AcquisitionMethod.WATCH_FOLDER.value, False,
                        "Watch-folder automation (not implemented in M2)."),
    AcquisitionProvider(AcquisitionMethod.URL_FETCH.value, False,
                        "RIFTWIRE fetches a saved video link itself (needs written platform "
                        "permission on the licence and URL acquisition switched on)."),
)}


def provider(method: str) -> AcquisitionProvider:
    p = PROVIDERS.get(method)
    if p is None:
        raise ValidationFailed(f"unknown acquisition method '{method}'")
    return p


def is_available(method: str, settings: Settings) -> bool:
    p = provider(method)
    if method == AcquisitionMethod.URL_FETCH.value:
        return settings.url_acquisition_enabled
    return p.available


def check_acquire(s: Session, settings: Settings, source_id: str,
                  method: str, *, fetched: bool = False) -> rights.Decision:
    """Both checks, before a single byte is accepted. Raises on refusal.
    ``fetched``: the caller is RIFTWIRE's own URL fetcher (the only one allowed to claim
    ``url_fetch``)."""
    p = provider(method)
    if method == AcquisitionMethod.URL_FETCH.value and not fetched:
        raise ValidationFailed("Media obtained by URL fetching can only be recorded by "
                               "RIFTWIRE's own acquisition (Acquire footage on the source's "
                               "page), not by an upload.", code="url_fetch_reserved",
                               details={"method": method})
    if not is_available(method, settings):
        raise AcquisitionUnavailable(f"Acquisition method '{method}' is not available: "
                                     f"{p.description}", details={"method": method})
    return rights.require(s, source_id, "acquire", method=method,
                          url_acquisition_enabled=settings.url_acquisition_enabled)


def attach_media(s: Session, settings: Settings, source_id: str, recording_id: str,
                 method: str | None = None, *, actor: str = "owner",
                 fetched: bool = False) -> rights.Decision:
    """Link an imported recording to its source after re-checking both permissions.
    Idempotent for the same source; never moves media to a different source."""
    src = s.get(Source, source_id)
    if src is None:
        raise NotFoundError("Source not found")
    rec = s.get(Recording, recording_id)
    if rec is None:
        raise NotFoundError("Recording not found")
    m = method or src.acquisition_method
    d = check_acquire(s, settings, source_id, m, fetched=fetched)
    if rec.source_id == source_id:
        if rec.acquisition_method is None and method is not None:
            # Linked media whose method was never recorded (pre-0005 data without a
            # usable provenance event): the owner states it explicitly, once.
            rec.acquisition_method = method
            s.flush()
            rights.provenance(s, "recording", rec.id, "acquisition_method_recorded",
                              actor=actor, source_id=source_id, method=method,
                              rights_record_id=d.record_id, rights_version=d.version)
        elif method is not None and method != rec.acquisition_method:
            raise ConflictError("How this footage was obtained is already on record",
                                details={"acquisition_method": rec.acquisition_method})
        return d
    if rec.source_id is not None:
        raise ConflictError("This recording already belongs to another source",
                            details={"source_id": rec.source_id})
    rec.source_id = source_id
    rec.acquisition_method = m          # the method actually used, enforced later
    s.flush()
    rights.provenance(s, "recording", rec.id, "attached_to_source", actor=actor,
                      source_id=source_id, method=m, sha256=rec.sha256,
                      rights_record_id=d.record_id, rights_version=d.version)
    rights.provenance(s, "source", source_id, "media_attached", actor=actor,
                      recording_id=rec.id, method=m, rights_record_id=d.record_id)
    return d
