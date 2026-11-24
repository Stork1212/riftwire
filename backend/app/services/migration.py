"""Verified, resumable storage migration (M1, PHASE2_PROPOSAL §8.5).

    plan()     read-only: what would be copied, sizes, destination checks and free space.
    execute()  copy → verify → switch. Requires RIFTWIRE stopped; holds the worker lock and
               the storage-maintenance lock for the whole run.

Guarantees:
* **Dry run by default** (the CLI only executes with ``--execute``); ``plan`` writes nothing.
* **Resumable:** every file has a journal row (PENDING → VERIFIED) committed after its
  copy is verified; a re-run continues where it stopped and never re-copies verified files.
* **Checksum-verified:** each file goes through ``transfer.transfer_file`` (staged copy,
  SHA-256 of the staged file re-read from disk, atomic no-overwrite promote). Recordings
  are checked against the SHA-256 recorded at import. Before switching, every destination
  file is hashed again.
* **The location switch is one database transaction**, after everything is verified.
* **Never deletes originals.** Source files are left in place and listed for the owner.
* Never writes to an unmounted drive, a non-APFS drive (macOS) or a non-empty folder.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from app.core.config import LOCATION_KINDS, MIN_FLOOR_BYTES, REPO_ROOT, Settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import Database
from app.models import (
    MigrationStatus,
    Recording,
    Severity,
    StorageLocation,
    StorageMigration,
    StorageMigrationItem,
)
from app.services import reservations, transfer, volumes
from app.services.events import record_event
from app.services.locations import (
    MARKER,
    MARKER_FORMAT,
    Layout,
    MaintenanceLock,
    load_layout,
    read_marker,
    verify,
    write_marker,
)
from app.services.storage import free_bytes, sha256_file

log = get_logger("migration")

STAGING = ".staging"
SKIP_NAMES = {MARKER}
SKIP_DIRS = {STAGING}
SKIP_SUFFIXES = (".partial", ".lock")


class MigrationRefused(Exception):
    """A precondition failed; nothing was written."""


@dataclass
class PlanFile:
    rel_path: str
    size: int
    sha256: str | None  # known for recordings (recorded at import); else hashed on execute


@dataclass
class Plan:
    kind: str
    source: Path
    dest: Path
    files: list[PlanFile]
    total_bytes: int
    dest_free_bytes: int | None
    dest_floor_bytes: int
    problems: list[str] = field(default_factory=list)
    resume_of: str | None = None
    already_verified: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


# --------------------------------------------------------------------------- helpers

SYSTEM_PREFIXES = ("/System", "/usr", "/bin", "/sbin", "/etc", "/Library", "/Applications",
                   "/proc", "/sys", "/boot")


def canonical(path: Path) -> Path:
    """Absolute path with ``..`` removed and every existing symlinked parent resolved
    (macOS ``/tmp`` and ``/var`` are symlinks), so the stored path is the real one."""
    p = Path(os.path.abspath(path))
    anchor = _nearest_existing(p)
    return anchor.resolve() / p.relative_to(anchor)


def _nearest_existing(path: Path) -> Path:
    p = path
    while not p.exists():
        p = p.parent
    return p


def _list_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f in SKIP_NAMES or f.endswith(SKIP_SUFFIXES):
                continue
            p = Path(dirpath, f)
            if p.is_symlink() or not p.is_file():
                continue
            out.append(p)
    return out


def _effective_floor(settings: Settings) -> int:
    return max(settings.min_free_disk_bytes,
               MIN_FLOOR_BYTES if settings.env != "test" else 0)


def open_migration(db: Database, kind: str) -> StorageMigration | None:
    with db.session() as s:
        m = s.scalar(select(StorageMigration).where(
            StorageMigration.kind == kind,
            StorageMigration.status == MigrationStatus.COPYING.value))
        if m is not None:
            s.expunge(m)
        return m


def check_destination(layout: Layout, kind: str, dest: Path,
                      resume: StorageMigration | None = None) -> list[str]:
    """Read-only checks of a destination path. Returns a list of problems."""
    problems: list[str] = []
    src = layout.path(kind)
    if not dest.is_absolute():
        return ["destination must be an absolute path"]
    dest = canonical(dest)
    if any(str(dest) == sp or str(dest).startswith(sp + "/") for sp in SYSTEM_PREFIXES):
        return [f"{dest} is a system location"]
    data_dir = layout.settings.data_dir.resolve()
    repo = REPO_ROOT.resolve()
    if (dest == repo or repo in dest.parents) and not (dest == data_dir
                                                       or data_dir in dest.parents):
        problems.append("destination is inside the RIFTWIRE source folder (outside its data "
                        "folder)")
    if dest == src:
        return ["destination is the current location"]
    for other_kind, loc in layout.locations.items():
        o = loc.path
        if dest == o or o in dest.parents or dest in o.parents:
            problems.append(f"destination overlaps the '{other_kind}' location ({o})")
    if (root := volumes.unmounted_volume_root(dest)) is not None:
        return problems + [f"the drive {root} is not mounted (nothing will be created on "
                           "the internal disk)"]
    anchor = _nearest_existing(dest)
    if anchor in (Path("/"), Path("/Volumes")):
        problems.append(f"refusing to create {dest} directly under {anchor}")
    try:
        vi = volumes.volume_info(anchor)
        if not volumes.filesystem_allowed(vi.fs_type):
            problems.append(f"filesystem '{vi.fs_type}' at {vi.mount_point} is not allowed "
                            "(APFS required)")
    except Exception as e:
        problems.append(f"could not identify the destination drive: {e}")
    if not os.access(anchor, os.W_OK | os.X_OK):
        problems.append(f"{anchor} is not writable")
    if dest.exists():
        if not dest.is_dir():
            problems.append("destination exists and is not a folder")
        else:
            names = [n for n in os.listdir(dest) if n != STAGING]
            m = read_marker(dest) if MARKER in names else None
            resuming = (resume is not None and m is not None
                        and m.get("location_uuid") == resume.dest_location_uuid)
            if names and not resuming:
                problems.append("destination folder is not empty (choose an empty or new "
                                "folder)")
    return problems


# --------------------------------------------------------------------------- plan

def plan(db: Database, settings: Settings, layout: Layout, kind: str, dest: Path) -> Plan:
    """Read-only plan. Writes nothing (no folders, no database rows)."""
    if kind not in LOCATION_KINDS:
        raise MigrationRefused(f"unknown location kind '{kind}'")
    dest = canonical(dest)
    resume = open_migration(db, kind)
    problems: list[str] = []
    if resume is not None and Path(resume.dest_path) != dest:
        problems.append(f"an unfinished migration of '{kind}' to {resume.dest_path} exists; "
                        "resume it or abort it first")
    st = layout.state(kind)
    if kind not in layout.locations:
        problems.append(f"the '{kind}' location is not registered yet: start RIFTWIRE once "
                        "(scripts/start.sh, then scripts/stop.sh) so it registers its "
                        "default locations")
        return Plan(kind, settings.default_location(kind), dest, [], 0, None,
                    _effective_floor(settings), problems)
    if not st.online:
        problems.append(f"the current '{kind}' location is offline: {st.message}")
    problems += check_destination(layout, kind, dest, resume)
    src = layout.path(kind)
    files: list[PlanFile] = []
    if st.online:
        known: dict[str, str] = {}
        if kind == "recordings":
            with db.session() as s:
                for fp, sha in s.execute(select(Recording.file_path, Recording.sha256)):
                    known[fp.partition("/")[2]] = sha
        for p in _list_files(src):
            rel = p.relative_to(src).as_posix()
            files.append(PlanFile(rel, p.stat().st_size, known.get(rel)))
    verified = 0
    if resume is not None and Path(resume.dest_path) == dest:
        with db.session() as s:
            verified = len(list(s.scalars(select(StorageMigrationItem.id).where(
                StorageMigrationItem.migration_id == resume.id,
                StorageMigrationItem.state == "VERIFIED"))))
    total = sum(f.size for f in files)
    floor = _effective_floor(settings)
    free = None
    try:
        anchor = _nearest_existing(dest)
        free = free_bytes(anchor)
        same_volume = os.stat(anchor).st_dev == os.stat(src).st_dev if st.online else False
        need = total  # copies are new data even on the same volume
        if free - need < floor:
            problems.append(
                f"not enough free space at the destination: {free / 1024**3:.1f} GiB free, "
                f"needs {need / 1024**3:.2f} GiB plus the {floor / 1024**3:.0f} GiB floor"
                + (" (same drive as the source)" if same_volume else ""))
    except OSError as e:
        problems.append(f"cannot read free space at the destination: {e}")
    return Plan(kind, src, dest, files, total, free, floor, problems,
                resume.id if resume else None, verified)


# --------------------------------------------------------------------------- execute

def api_running(settings: Settings, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((settings.api_host, settings.api_port), timeout):
            return True
    except OSError:
        return False


@dataclass
class Result:
    migration_id: str
    kind: str
    source: Path
    dest: Path
    copied: int
    already_verified: int
    total_bytes: int
    old_files: list[str]


def execute(db: Database, settings: Settings, kind: str, dest: Path, *,
            rehash: bool = True, check_api: bool = True,
            progress: Callable[[str], None] = lambda _m: None) -> Result:
    """Copy, verify and switch ``kind`` to ``dest``. Raises ``MigrationRefused`` (nothing
    switched) on any failed precondition or verification."""
    from app.workers.worker import WorkerLock, WorkerLockedError  # avoid import cycle

    if check_api and api_running(settings):
        raise MigrationRefused("RIFTWIRE's API is running: stop it first (scripts/stop.sh)")
    wlock = WorkerLock(settings.data_dir / "worker.lock")
    try:
        wlock.acquire()
    except WorkerLockedError:
        raise MigrationRefused("the RIFTWIRE worker is running: stop it first "
                               "(scripts/stop.sh)") from None
    mlock = MaintenanceLock(settings)
    try:
        if not mlock.acquire():
            raise MigrationRefused("another storage migration is running")
        return _execute_locked(db, settings, kind, canonical(dest),
                               rehash=rehash, progress=progress)
    finally:
        mlock.release()
        wlock.release()


def _execute_locked(db: Database, settings: Settings, kind: str, dest: Path, *,
                    rehash: bool, progress: Callable[[str], None]) -> Result:
    layout = load_layout(db, settings, "migration")
    p = plan(db, settings, layout, kind, dest)
    if not p.ok:
        raise MigrationRefused("; ".join(p.problems))
    src_loc = layout.location(kind)

    # 1. Create the destination (only now, after every check) or resume it.
    mig = open_migration(db, kind)
    if mig is None:
        dest.mkdir(parents=True, exist_ok=True)
        try:
            vi = volumes.volume_info(dest)
        except Exception as e:
            raise MigrationRefused(f"could not identify the destination drive: {e}") from e
        if not volumes.filesystem_allowed(vi.fs_type):
            raise MigrationRefused(f"filesystem '{vi.fs_type}' is not allowed (APFS required)")
        vol_uuid = _volume_uuid_for(db, dest, vi.hw_volume_uuid)
        loc_uuid = str(uuid.uuid4())
        write_marker(dest, {"format": MARKER_FORMAT, "kind": kind, "location_uuid": loc_uuid,
                            "volume_uuid": vol_uuid, "hw_volume_uuid": vi.hw_volume_uuid,
                            "created_at": utcnow().isoformat()})
        with db.session() as s:
            mig = StorageMigration(kind=kind, source_path=str(src_loc.path),
                                   dest_path=str(dest), dest_location_uuid=loc_uuid,
                                   status=MigrationStatus.COPYING.value,
                                   total_files=len(p.files), total_bytes=p.total_bytes,
                                   created_at=utcnow(), updated_at=utcnow())
            s.add(mig)
            s.flush()
            record_event(s, Severity.INFO, "migration", "storage_migration_started",
                         f"Migration of '{kind}' to {dest} started ({len(p.files)} files, "
                         f"{p.total_bytes} bytes)")
            s.expunge(mig)
        progress(f"created destination {dest}")
    else:
        progress(f"resuming migration {mig.id}")
    marker = read_marker(dest)
    if marker is None or marker.get("location_uuid") != mig.dest_location_uuid:
        raise MigrationRefused("destination marker does not match the migration journal")

    # 2. Journal: one row per file (idempotent).
    with db.session() as s:
        have = {r.rel_path: r for r in s.scalars(select(StorageMigrationItem).where(
            StorageMigrationItem.migration_id == mig.id))}
        for f in p.files:
            if f.rel_path not in have:
                sha = f.sha256 or sha256_file(p.source / f.rel_path)
                s.add(StorageMigrationItem(migration_id=mig.id, rel_path=f.rel_path,
                                           size_bytes=f.size, sha256=sha, state="PENDING"))
    with db.session() as s:
        items = list(s.scalars(select(StorageMigrationItem).where(
            StorageMigrationItem.migration_id == mig.id).order_by(StorageMigrationItem.id)))
        for it in items:
            s.expunge(it)

    # 3. Copy + verify each pending file under a reservation on the destination volume.
    pending = [it for it in items if it.state != "VERIFIED"]
    remaining = sum(it.size_bytes for it in pending)
    dest_vol = marker["volume_uuid"]
    res = reservations.reserve_on_volume(
        db, dest_vol, dest, _effective_floor(settings), kind, f"migration:{mig.id}",
        remaining, lease_seconds=settings.reservation_lease_seconds, free_fn=free_bytes)
    copied = 0
    staging = dest / STAGING
    try:
        for it in pending:
            src = p.source / it.rel_path
            dst = dest / it.rel_path
            if not src.is_file():
                raise MigrationRefused(f"source file disappeared: {it.rel_path}")
            if src.stat().st_size != it.size_bytes:
                raise MigrationRefused(f"source file changed since planning: {it.rel_path}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                outcome = transfer.transfer_file(src, dst, it.sha256, staging,
                                                 f"migration-{mig.id[:8]}")
            except transfer.TransferError as e:
                raise MigrationRefused(f"{it.rel_path}: {e}") from e
            with db.session() as s:
                row = s.get(StorageMigrationItem, it.id)
                row.state = "VERIFIED"
                s.get(StorageMigration, mig.id).updated_at = utcnow()
            copied += 1
            remaining -= it.size_bytes
            reservations.renew(db, None, res, remaining,
                               lease_seconds=settings.reservation_lease_seconds,
                               free_fn=free_bytes)
            progress(f"verified {it.rel_path} ({outcome})")
    finally:
        reservations.release(db, res)
    try:
        staging.rmdir()
    except OSError:
        pass

    # 4. Final full verification of the destination.
    if rehash:
        for it in items:
            if sha256_file(dest / it.rel_path) != it.sha256:
                raise MigrationRefused(f"final verification failed for {it.rel_path}; "
                                       "nothing switched")
        progress(f"final verification: {len(items)} file(s) re-hashed, all match")

    # 5. Switch in one transaction.
    vi = volumes.volume_info(dest)
    with db.session() as s:
        row = s.get(StorageLocation, kind)
        row.path = str(dest)
        row.location_uuid = mig.dest_location_uuid
        row.volume_uuid = dest_vol
        row.hw_volume_uuid = vi.hw_volume_uuid
        row.fs_type = vi.fs_type
        row.floor_bytes = max(row.floor_bytes, settings.min_free_disk_bytes)
        row.updated_at = utcnow()
        m = s.get(StorageMigration, mig.id)
        m.status = MigrationStatus.SWITCHED.value
        m.updated_at = utcnow()
        record_event(s, Severity.INFO, "migration", "storage_migration_switched",
                     f"Location '{kind}' switched from {p.source} to {dest} after verifying "
                     f"{len(items)} file(s); originals kept at {p.source}")
    # Sanity: the new location verifies online.
    loc_check = load_layout(db, settings, "migration").state(kind)
    if not loc_check.online:
        log.error(f"switched location reports offline: {loc_check.message}")
    return Result(mig.id, kind, p.source, dest, copied, len(items) - len(pending),
                  sum(it.size_bytes for it in items),
                  [str(p.source / it.rel_path) for it in items])


def _volume_uuid_for(db: Database, root: Path, hw: str | None) -> str:
    dev = os.stat(root).st_dev
    with db.session() as s:
        rows = list(s.scalars(select(StorageLocation)))
    for r in rows:
        if hw and r.hw_volume_uuid == hw:
            return r.volume_uuid
        try:
            if os.stat(r.path).st_dev == dev:
                return r.volume_uuid
        except OSError:
            continue
    return str(uuid.uuid4())


def abort(db: Database, kind: str) -> StorageMigration | None:
    """Mark the unfinished migration of ``kind`` aborted. The location is unchanged; files
    already copied to the destination are left there for the owner to inspect/remove."""
    with db.session() as s:
        m = s.scalar(select(StorageMigration).where(
            StorageMigration.kind == kind,
            StorageMigration.status == MigrationStatus.COPYING.value))
        if m is None:
            return None
        m.status = MigrationStatus.ABORTED.value
        m.updated_at = utcnow()
        record_event(s, Severity.WARNING, "migration", "storage_migration_aborted",
                     f"Migration of '{kind}' to {m.dest_path} aborted; location unchanged; "
                     "copied files left in place")
        s.expunge(m)
        return m


def verify_location_online(layout: Layout, kind: str) -> bool:
    return verify(layout.location(kind)).online
