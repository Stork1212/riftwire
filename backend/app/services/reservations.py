"""Persistent, per-volume disk reservations shared by the API and the worker (M1).

Admission rule, evaluated in ONE ``BEGIN IMMEDIATE`` transaction (so concurrent callers in
any process serialize on the SQLite write lock):

    free_now(volume) − Σ active reservations(volume, excluding this one) − bytes ≥ floor

``bytes`` is what the holder may still write. Holders renew with their remaining bytes;
rows expire unless renewed, and expired rows are reclaimed, so a crashed holder never
blocks space forever.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from app.core.errors import InsufficientStorage
from app.core.logging import get_logger
from app.db.base import new_uuid, utcnow
from app.db.session import Database
from app.models import DiskReservation, Severity
from app.services.events import record_event
from app.services.locations import Layout
from app.services.storage import free_bytes

log = get_logger("reservations")

GIB = 1024**3
FreeFn = Callable[[Path], int]


@dataclass
class Reservation:
    id: str
    kind: str
    volume_uuid: str
    holder: str
    path: Path | None = None    # set for explicit-volume reservations (migrations)
    floor: int | None = None


def _insufficient(free: int, others: int, need: int, floor: int, kind: str) -> InsufficientStorage:
    return InsufficientStorage(
        f"Not enough free disk space on the {kind} drive: this would leave less than the "
        f"{floor // GIB} GiB safety minimum.",
        details={"free_bytes": free, "reserved_bytes": others, "required_bytes": need,
                 "min_free_bytes": floor, "location": kind})


def reserve(db: Database, layout: Layout, kind: str, holder: str, nbytes: int, *,
            lease_seconds: int, free_fn: FreeFn = free_bytes) -> Reservation:
    """Admit ``nbytes`` on ``kind``'s volume or raise ``InsufficientStorage``.

    The location must be online (``StorageOffline`` otherwise)."""
    layout.require_online(kind)
    loc = layout.location(kind)
    return reserve_on_volume(db, loc.volume_uuid, loc.path,
                             layout.floor_for_volume(loc.volume_uuid), kind, holder, nbytes,
                             lease_seconds=lease_seconds, free_fn=free_fn)


def reserve_on_volume(db: Database, volume_uuid: str, path: Path, floor: int, label: str,
                      holder: str, nbytes: int, *, lease_seconds: int,
                      free_fn: FreeFn = free_bytes) -> Reservation:
    """Admission on an explicit volume (used by migrations to a not-yet-active location)."""
    rid = new_uuid()
    with db.session() as s:
        now = utcnow()
        s.execute(delete(DiskReservation).where(DiskReservation.expires_at <= now))
        others = s.scalar(select(func.coalesce(func.sum(DiskReservation.bytes), 0)).where(
            DiskReservation.volume_uuid == volume_uuid)) or 0
        free = free_fn(path)
        if free - others - max(0, nbytes) < floor:
            raise _insufficient(free, others, nbytes, floor, label)
        s.add(DiskReservation(id=rid, volume_uuid=volume_uuid, location_kind=label[:16],
                              holder=holder[:128], bytes=max(0, nbytes), created_at=now,
                              expires_at=now + timedelta(seconds=lease_seconds)))
    return Reservation(rid, label, volume_uuid, holder, path=path, floor=floor)


def renew(db: Database, layout: Layout | None, res: Reservation, remaining: int, *,
          lease_seconds: int, free_fn: FreeFn = free_bytes) -> None:
    """Update the bytes still to be written and extend the lease, re-checking the floor.

    If the row expired and was reclaimed meanwhile, it is re-admitted under the same id."""
    if res.path is not None:
        path, floor = res.path, res.floor or 0
    else:
        path = layout.location(res.kind).path
        floor = layout.floor_for_volume(res.volume_uuid)
    loc_path = path
    with db.session() as s:
        now = utcnow()
        s.execute(delete(DiskReservation).where(DiskReservation.expires_at <= now,
                                                DiskReservation.id != res.id))
        others = s.scalar(select(func.coalesce(func.sum(DiskReservation.bytes), 0)).where(
            DiskReservation.volume_uuid == res.volume_uuid,
            DiskReservation.id != res.id)) or 0
        free = free_fn(loc_path)
        if free - others - max(0, remaining) < floor:
            raise _insufficient(free, others, remaining, floor, res.kind)
        row = s.get(DiskReservation, res.id)
        if row is None:
            s.add(DiskReservation(id=res.id, volume_uuid=res.volume_uuid,
                                  location_kind=res.kind[:16], holder=res.holder[:128],
                                  bytes=max(0, remaining), created_at=now,
                                  expires_at=now + timedelta(seconds=lease_seconds)))
        else:
            row.bytes = max(0, remaining)
            row.expires_at = now + timedelta(seconds=lease_seconds)


def release(db: Database, res: Reservation | None) -> None:
    if res is None:
        return
    with db.session() as s:
        s.execute(delete(DiskReservation).where(DiskReservation.id == res.id))


def reclaim_expired(db: Database, component: str) -> int:
    with db.session() as s:
        n = s.execute(delete(DiskReservation).where(
            DiskReservation.expires_at <= utcnow())).rowcount or 0
        if n:
            record_event(s, Severity.INFO, component, "reservations_reclaimed",
                         f"Reclaimed {n} expired disk reservation(s)")
    return n


def reserved_bytes(db: Database, volume_uuid: str) -> int:
    with db.session() as s:
        return int(s.scalar(select(func.coalesce(func.sum(DiskReservation.bytes), 0)).where(
            DiskReservation.volume_uuid == volume_uuid,
            DiskReservation.expires_at > utcnow())) or 0)
