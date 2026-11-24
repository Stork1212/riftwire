"""Linking a recording import to its source (M2): permission pre-check before any byte is
accepted, and the link itself after the file was validated.

Two forms (no manual step is hardcoded, R1):
* ``source_id``: an existing source (a pasted, authorized URL, or a local source);
* ``creator_channel_id``: a new local source on that channel, covered by the channel's
  standing licence, so repeat imports need no per-file attestation (§12.3).
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.errors import AppError
from app.db.session import Database
from app.models import AcquisitionMethod
from app.services import acquisition, sources
from app.services.storage import sanitize_display_filename


def _method(method: str | None) -> str:
    return method or AcquisitionMethod.LOCAL_RECORDING.value


def precheck(db: Database, settings: Settings, source_id: str | None,
             creator_channel_id: str | None, method: str | None, filename: str) -> None:
    """Raises if the import would not be allowed. Writes nothing."""
    with db.session() as s:
        try:
            if source_id:
                src = sources.get_source(s, source_id)
                acquisition.check_acquire(s, settings, source_id,
                                          method or src.acquisition_method)
            else:
                # Dry run of the real path; rolled back below whatever happens.
                src = sources.local_source_for_upload(
                    s, creator_channel_id, sanitize_display_filename(filename),
                    _method(method))
                acquisition.check_acquire(s, settings, src.id, src.acquisition_method)
        finally:
            s.rollback()


def link(db: Database, settings: Settings, recording_id: str, source_id: str | None,
         creator_channel_id: str | None, method: str | None,
         filename: str) -> tuple[str | None, str | None]:
    """Link after a successful import. Returns (source_id, None) or (None, error): a
    refusal here (rights revoked mid-upload) leaves the recording imported but unlinked,
    so it can never be analysed, edited or published."""
    try:
        with db.session() as s:
            if source_id is None:
                src = sources.local_source_for_upload(
                    s, creator_channel_id, sanitize_display_filename(filename),
                    _method(method))
                source_id = src.id
            acquisition.attach_media(s, settings, source_id, recording_id, method)
        return source_id, None
    except AppError as e:
        return None, e.message
