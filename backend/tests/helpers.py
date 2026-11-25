from __future__ import annotations

from pathlib import Path

from app.services.ingest import ImportResult, UploadSession, complete_upload
from app.services.locations import MARKER


def user_files(d: Path, files_only: bool = False) -> list[str]:
    """Names in ``d`` excluding the location identity marker (sorted)."""
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.name != MARKER and (not files_only or p.is_file()))


def import_file(db, settings, path: Path, name: str | None = None,
                chunk: int = 256 * 1024) -> ImportResult:
    """Stream a local file through the same code path the API uses."""
    size = path.stat().st_size
    up = UploadSession(settings, name or path.name, size, db)
    try:
        with open(path, "rb") as f:
            while data := f.read(chunk):
                up.write(data)
        digest = up.finish()
    except BaseException:
        up.abort()
        raise
    return complete_upload(db, settings, up, digest)


LEGACY_PUBLIC_WEB_SCOPE = {"v": 1, "uses": ["acquire", "analyze"],
                           "acquisition_methods": ["url_fetch"],
                           "notes": "Recorded automatically for a saved link. "
                                    "Not creator-confirmed."}


def legacy_public_web(s, source_id: str):
    """Reproduce, row for row, the automatic ``public_web`` record that ``b13b91f`` wrote
    for every saved link (system-attested, scope acquire + analyze by url_fetch). The
    application no longer writes these; tests use this to prove existing rows authorize
    nothing."""
    import hashlib

    from app.db.base import utcnow
    from app.models import CreatorChannel, RightsRecord, RightsSourceLink, Source
    from app.services import rights

    src = s.get(Source, source_id)
    ch = s.get(CreatorChannel, src.creator_channel_id)
    text = rights.canonical_scope(rights.Scope.model_validate(LEGACY_PUBLIC_WEB_SCOPE))
    now = utcnow()
    r = RightsRecord(creator_id=ch.creator_id, basis="public_web", coverage="sources",
                     scope_json=text, scope_sha256=hashlib.sha256(text.encode()).hexdigest(),
                     valid_from=now, created_at=now, version=1)
    s.add(r)
    s.flush()
    s.add(RightsSourceLink(rights_record_id=r.id, source_id=source_id))
    s.flush()
    r.attestation_text = ("Publicly accessible footage, recorded automatically when the link "
                          "was saved. Not creator-confirmed: covers fetching and analysis "
                          "only, never editing or publishing.")
    r.attested_by, r.attested_at = "system", now
    s.flush()
    rights.provenance(s, "rights", r.id, "recorded_public_web", actor="system",
                      source_id=source_id, basis=r.basis, coverage=r.coverage,
                      scope_sha256=r.scope_sha256)
    return r
