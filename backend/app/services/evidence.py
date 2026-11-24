"""Rights evidence: supporting documentation for a rights record (M2, R3, plan §12.6).

* Evidence is **documentation, not proof**: storing it never changes a record's status.
* Files live on the internal disk under ``<data_dir>/evidence/<uuid>.<ext>``, read-only,
  never overwritten, never deleted automatically (rows are append-only in the database).
* Limits: ``evidence_max_file_bytes`` (25 MB) per file and ``evidence_max_total_bytes``
  in total. The total is checked before receiving and again inside the transaction that
  records the file (``BEGIN IMMEDIATE`` serializes concurrent uploads).
* The write goes through a persistent disk reservation (60 GiB floor) and is streamed in
  chunks to a temporary file in the same folder, then promoted with ``link(2)``.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import (
    NotFoundError,
    PayloadTooLarge,
    UnsupportedMedia,
    ValidationFailed,
)
from app.db.session import Database
from app.models import EvidenceKind, RightsEvidence, RightsRecord
from app.services import reservations, rights
from app.services.locations import get_layout
from app.services.storage import file_extension, free_bytes, fsync_dir, sanitize_display_filename

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "txt", "md", "eml"}
TMP_PREFIX = ".upload-"
TMP_SUFFIX = ".tmp"


class EvidenceQuotaExceeded(PayloadTooLarge):
    code = "evidence_quota_exceeded"


def total_bytes(s) -> int:
    return int(s.scalar(select(func.coalesce(func.sum(RightsEvidence.size_bytes), 0))) or 0)


def _cleanup_stale_tmp(settings: Settings) -> None:
    cutoff = time.time() - settings.stale_temp_seconds
    try:
        for p in settings.evidence_dir.iterdir():
            if p.name.startswith(TMP_PREFIX) and p.name.endswith(TMP_SUFFIX):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass


def add_evidence(db: Database, settings: Settings, record_id: str, *, kind: str,
                 reference: str | None = None, filename: str | None = None,
                 content_length: int | None = None,
                 chunks: Iterable[bytes] = ()) -> RightsEvidence:
    """Record evidence for ``record_id``; with a file if ``content_length`` > 0."""
    if kind not in {k.value for k in EvidenceKind}:
        raise ValidationFailed(f"unknown evidence kind '{kind}'")
    reference = (reference or "").strip() or None
    with db.session() as s:
        if s.get(RightsRecord, record_id) is None:
            raise NotFoundError("Rights record not found")
    if not content_length:
        if reference is None:
            raise ValidationFailed("a reference or a file is required")
        with db.session() as s:
            ev = RightsEvidence(rights_record_id=record_id, kind=kind, reference=reference)
            s.add(ev)
            s.flush()
            rights.provenance(s, "rights", record_id, "evidence_added", evidence_id=ev.id,
                              kind=kind, with_file=False)
            s.expunge(ev)
        return ev

    ext = file_extension(filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedMedia(f"Evidence files must be one of: "
                               f"{', '.join(sorted(ALLOWED_EXTENSIONS))}")
    if content_length < 0:
        raise ValidationFailed("invalid length")
    if content_length > settings.evidence_max_file_bytes:
        raise PayloadTooLarge(f"Evidence files are limited to "
                              f"{settings.evidence_max_file_bytes // (1024 * 1024)} MB",
                              details={"max_bytes": settings.evidence_max_file_bytes})
    with db.session() as s:
        if total_bytes(s) + content_length > settings.evidence_max_total_bytes:
            raise EvidenceQuotaExceeded("The evidence storage limit would be exceeded",
                                        details={"limit": settings.evidence_max_total_bytes})

    edir = settings.evidence_dir
    edir.mkdir(exist_ok=True)           # internal data directory: always safe
    _cleanup_stale_tmp(settings)
    layout = get_layout(settings)
    vol = reservations.volume_for_path(layout, edir)
    floor = max(settings.min_free_disk_bytes,
                layout.floor_for_volume(vol) if layout.volume_kinds(vol) else 0)
    stored = f"{uuid.uuid4()}.{ext}"
    tmp = edir / f"{TMP_PREFIX}{uuid.uuid4().hex}{TMP_SUFFIX}"
    res = reservations.reserve_on_volume(
        db, vol, edir, floor, "evidence", f"evidence:{stored}", content_length,
        lease_seconds=settings.reservation_lease_seconds, free_fn=free_bytes)
    h = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "xb") as f:
            for chunk in chunks:
                size += len(chunk)
                if size > content_length:
                    raise ValidationFailed("The file is larger than its declared length")
                h.update(chunk)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        if size != content_length:
            raise ValidationFailed("The file is shorter than its declared length")
        final = edir / stored
        with db.session() as s:
            # Re-checked under the write lock: concurrent uploads cannot jointly exceed it.
            if total_bytes(s) + size > settings.evidence_max_total_bytes:
                raise EvidenceQuotaExceeded("The evidence storage limit would be exceeded",
                                            details={"limit":
                                                     settings.evidence_max_total_bytes})
            ev = RightsEvidence(rights_record_id=record_id, kind=kind, reference=reference,
                                original_filename=sanitize_display_filename(filename or ""),
                                stored_name=stored, sha256=h.hexdigest(), size_bytes=size)
            s.add(ev)
            s.flush()
            rights.provenance(s, "rights", record_id, "evidence_added", evidence_id=ev.id,
                              kind=kind, with_file=True, sha256=ev.sha256, size_bytes=size)
            os.link(tmp, final)          # never overwrites (EEXIST); inside the transaction
            os.chmod(final, 0o444)
            s.expunge(ev)
        fsync_dir(edir)
        return ev
    finally:
        tmp.unlink(missing_ok=True)
        reservations.release(db, res)


def evidence_file(settings: Settings, ev: RightsEvidence) -> Path:
    if not ev.stored_name:
        raise NotFoundError("This evidence has no file")
    p = settings.evidence_dir / ev.stored_name
    if p.parent != settings.evidence_dir or not p.is_file():
        raise NotFoundError("Evidence file not found")
    return p
