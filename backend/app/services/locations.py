"""Storage locations and volume identity (M1, PHASE2_PROPOSAL §8).

* The ``storage_locations`` table is the authority for where each location kind lives.
  Every process calls :func:`load_layout` at startup, before touching any file.
* Each location root holds a marker file, written once and never overwritten. It carries
  the location UUID, its volume UUID and the OS's own volume UUID (macOS APFS
  ``VolumeUUID``). A location is **online** only if the root exists, the marker matches
  the database row, the OS volume UUID matches (when recorded), the filesystem is allowed
  (APFS on macOS) and the root is writable.
* Nothing ever falls back to another location. An offline location refuses writes
  (``StorageOffline``); jobs that need it wait without consuming attempts.
* Only the internal defaults (``<data_dir>/<kind>``) are registered automatically.
  External locations are set only through the verified migration tool.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.core.config import LOCATION_KINDS, Settings
from app.core.errors import StorageOffline
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import Database
from app.models import Severity, StorageLocation
from app.services import volumes
from app.services.events import record_event
from app.services.storage import fsync_dir

log = get_logger("locations")

MARKER = ".riftwire-location.json"
MARKER_FORMAT = 1

# Reason codes (stable; shown in readiness, the dashboard and events).
ONLINE = "online"
VOLUME_NOT_MOUNTED = "volume_not_mounted"
MISSING_ROOT = "missing_root"
MARKER_MISSING = "marker_missing"
MARKER_UNREADABLE = "marker_unreadable"
MARKER_MISMATCH = "marker_mismatch"
WRONG_VOLUME = "wrong_volume"
FILESYSTEM_NOT_ALLOWED = "filesystem_not_allowed"
NOT_WRITABLE = "not_writable"
NOT_REGISTERED = "not_registered"
PROBE_FAILED = "probe_failed"

REASON_TEXT = {
    ONLINE: "online",
    VOLUME_NOT_MOUNTED: "the drive is not mounted",
    MISSING_ROOT: "the location folder does not exist",
    MARKER_MISSING: "the location's identity marker is missing",
    MARKER_UNREADABLE: "the location's identity marker is unreadable",
    MARKER_MISMATCH: "a different location's marker was found (wrong drive or folder)",
    WRONG_VOLUME: "the drive's volume UUID does not match the configured drive",
    FILESYSTEM_NOT_ALLOWED: "the filesystem is not allowed (APFS required)",
    NOT_WRITABLE: "the location folder is not writable",
    NOT_REGISTERED: "the location is not registered",
    PROBE_FAILED: "the drive could not be identified",
}


@dataclass(frozen=True)
class Location:
    kind: str
    path: Path
    location_uuid: str
    volume_uuid: str
    hw_volume_uuid: str | None
    fs_type: str | None
    floor_bytes: int


@dataclass(frozen=True)
class LocationState:
    kind: str
    online: bool
    reason: str
    fs_type: str | None = None
    mount_point: str | None = None
    label: str | None = None

    @property
    def message(self) -> str:
        return REASON_TEXT.get(self.reason, self.reason)


# --------------------------------------------------------------------------- markers

def read_marker(root: Path) -> dict | None:
    try:
        raw = (root / MARKER).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(raw)


def write_marker(root: Path, data: dict) -> None:
    """Create the marker atomically and exclusively: never overwrites an existing one."""
    final = root / MARKER
    tmp = root / f".riftwire-location.{uuid.uuid4().hex}.tmp"
    with open(tmp, "x", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, final)  # fails with FileExistsError if a marker is already there
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(root)


def marker_for(loc: Location) -> dict:
    return {"format": MARKER_FORMAT, "kind": loc.kind, "location_uuid": loc.location_uuid,
            "volume_uuid": loc.volume_uuid, "hw_volume_uuid": loc.hw_volume_uuid,
            "created_at": utcnow().isoformat()}


# --------------------------------------------------------------------------- verification

def verify(loc: Location) -> LocationState:
    """Fresh, read-only check that ``loc`` is the configured location on the right drive."""
    k = loc.kind
    if volumes.unmounted_volume_root(loc.path) is not None:
        return LocationState(k, False, VOLUME_NOT_MOUNTED)
    if not loc.path.is_dir():
        return LocationState(k, False, MISSING_ROOT)
    try:
        m = read_marker(loc.path)
    except (OSError, ValueError):
        return LocationState(k, False, MARKER_UNREADABLE)
    if m is None:
        return LocationState(k, False, MARKER_MISSING)
    if (m.get("location_uuid") != loc.location_uuid or m.get("volume_uuid") != loc.volume_uuid
            or m.get("kind") != loc.kind):
        return LocationState(k, False, MARKER_MISMATCH)
    try:
        vi = volumes.volume_info(loc.path)
    except Exception as e:  # diskutil missing/timeout, unreadable mountinfo, ...
        log.warning(f"volume probe failed for {loc.kind}: {e}")
        return LocationState(k, False, PROBE_FAILED)
    if loc.hw_volume_uuid and vi.hw_volume_uuid != loc.hw_volume_uuid:
        return LocationState(k, False, WRONG_VOLUME, vi.fs_type, vi.mount_point, vi.label)
    if not volumes.filesystem_allowed(vi.fs_type):
        return LocationState(k, False, FILESYSTEM_NOT_ALLOWED, vi.fs_type, vi.mount_point,
                             vi.label)
    if not os.access(loc.path, os.W_OK | os.X_OK):
        return LocationState(k, False, NOT_WRITABLE, vi.fs_type, vi.mount_point, vi.label)
    return LocationState(k, True, ONLINE, vi.fs_type, vi.mount_point, vi.label)


# --------------------------------------------------------------------------- layout

class Layout:
    """The resolved locations of one process, with cached verification."""

    def __init__(self, settings: Settings, locations: dict[str, Location]):
        self.settings = settings
        self.locations = locations
        self._cache: dict[str, tuple[float, LocationState]] = {}
        self._lock = threading.Lock()

    def location(self, kind: str) -> Location:
        try:
            return self.locations[kind]
        except KeyError:
            raise StorageOffline(f"Storage location '{kind}' is not registered",
                                 details={"kind": kind, "reason": NOT_REGISTERED}) from None

    def path(self, kind: str) -> Path:
        return self.location(kind).path

    def state(self, kind: str, max_age: float = 0.0) -> LocationState:
        """Verification result; ``max_age`` > 0 allows a recent cached result."""
        if kind not in self.locations:
            return LocationState(kind, False, NOT_REGISTERED)
        now = time.monotonic()
        if max_age > 0:
            with self._lock:
                hit = self._cache.get(kind)
            if hit and now - hit[0] <= max_age:
                return hit[1]
        st = verify(self.locations[kind])
        with self._lock:
            self._cache[kind] = (now, st)
        return st

    def states(self, max_age: float = 0.0) -> list[LocationState]:
        return [self.state(k, max_age) for k in LOCATION_KINDS]

    def offline(self, kinds, max_age: float = 0.0) -> list[LocationState]:
        return [st for st in (self.state(k, max_age) for k in kinds) if not st.online]

    def require_online(self, *kinds: str) -> None:
        """Fresh verification; raises ``StorageOffline`` naming every offline kind."""
        bad = self.offline(kinds)
        if bad:
            raise StorageOffline(
                "Storage offline: " + "; ".join(f"{b.kind}: {b.message}" for b in bad),
                details={"locations": [{"kind": b.kind, "reason": b.reason} for b in bad]})

    def volume_kinds(self, volume_uuid: str) -> list[str]:
        return [k for k, loc in self.locations.items() if loc.volume_uuid == volume_uuid]

    def floor_for_volume(self, volume_uuid: str) -> int:
        """Effective floor: the strictest of the configured floor and every location's
        floor on that volume."""
        floors = [loc.floor_bytes for loc in self.locations.values()
                  if loc.volume_uuid == volume_uuid]
        return max([self.settings.min_free_disk_bytes, *floors])


def get_layout(settings: Settings) -> Layout:
    lay = settings.layout
    if not isinstance(lay, Layout):
        raise RuntimeError("storage layout not loaded (call locations.load_layout first)")
    return lay


def _to_location(row: StorageLocation) -> Location:
    return Location(kind=row.kind, path=Path(row.path), location_uuid=row.location_uuid,
                    volume_uuid=row.volume_uuid, hw_volume_uuid=row.hw_volume_uuid,
                    fs_type=row.fs_type, floor_bytes=row.floor_bytes)


def _volume_uuid_for(rows: list[StorageLocation], root: Path, hw: str | None) -> str:
    """Reuse the volume UUID of a location already on the same drive, else a new one."""
    dev = os.stat(root).st_dev
    for r in rows:
        if hw and r.hw_volume_uuid == hw:
            return r.volume_uuid
        try:
            if os.stat(r.path).st_dev == dev:
                return r.volume_uuid
        except OSError:
            continue
    return str(uuid.uuid4())


def register_root(rows: list[StorageLocation], kind: str, root: Path,
                  floor_bytes: int) -> StorageLocation:
    """Write (or adopt) the marker in an existing, verified-safe root and return a new
    row (not yet added to a session). The caller checked that creating/using ``root`` is
    safe (internal default, or a mounted, allowed volume)."""
    vi = volumes.volume_info(root)
    if not volumes.filesystem_allowed(vi.fs_type):
        raise StorageOffline(f"{kind}: filesystem '{vi.fs_type}' is not allowed (APFS "
                             "required)", details={"kind": kind,
                                                   "reason": FILESYSTEM_NOT_ALLOWED})
    existing = read_marker(root)
    if existing is not None:
        if existing.get("kind") != kind:
            raise StorageOffline(f"{root} already holds a marker for '{existing.get('kind')}'",
                                 details={"kind": kind, "reason": MARKER_MISMATCH})
        loc_uuid, vol_uuid = existing["location_uuid"], existing["volume_uuid"]
    else:
        loc_uuid = str(uuid.uuid4())
        vol_uuid = _volume_uuid_for(rows, root, vi.hw_volume_uuid)
        write_marker(root, {"format": MARKER_FORMAT, "kind": kind, "location_uuid": loc_uuid,
                            "volume_uuid": vol_uuid, "hw_volume_uuid": vi.hw_volume_uuid,
                            "created_at": utcnow().isoformat()})
    now = utcnow()
    return StorageLocation(kind=kind, path=str(root), location_uuid=loc_uuid,
                           volume_uuid=vol_uuid, hw_volume_uuid=vi.hw_volume_uuid,
                           fs_type=vi.fs_type, floor_bytes=floor_bytes, created_at=now,
                           updated_at=now)


def load_layout(db: Database, settings: Settings, component: str = "app", *,
                register: bool = True) -> Layout:
    """Load the layout from the database and apply it to ``settings``.

    Missing kinds are registered at their **internal default** (``<data_dir>/<kind>``)
    only. Nothing else is created. Configured locations are verified and reported, never
    created or replaced. ``register=False`` is strictly read-only (dry runs): missing kinds
    stay unregistered and nothing is written.
    """
    if not register:
        with db.session() as s:
            rows = list(s.scalars(select(StorageLocation)))
            locs = {r.kind: _to_location(r) for r in rows}
        layout = Layout(settings, locs)
        settings.apply_layout({k: loc.path for k, loc in locs.items()}, layout)
        return layout
    with db.session() as s:
        rows = list(s.scalars(select(StorageLocation)))
        have = {r.kind for r in rows}
        for kind in LOCATION_KINDS:
            if kind in have:
                continue
            root = settings.default_location(kind)
            root.mkdir(parents=True, exist_ok=True)  # internal data dir: always safe
            row = register_root(rows, kind, root, settings.min_free_disk_bytes)
            s.add(row)
            rows.append(row)
            record_event(s, Severity.INFO, component, "storage_location_registered",
                         f"Registered internal default location for {kind}")
        # An internal default folder that was deleted is recreated with its recorded
        # identity (it lives inside the data directory, so this can never write to a
        # missing external drive). External locations are never recreated.
        for r in rows:
            root = Path(r.path)
            if root == settings.default_location(r.kind) and not root.exists():
                root.mkdir(parents=True)
                write_marker(root, marker_for(_to_location(r)))
                record_event(s, Severity.WARNING, component, "storage_location_restored",
                             f"Recreated missing internal folder for {r.kind}")
        s.flush()
        locs = {r.kind: _to_location(r) for r in rows}
    layout = Layout(settings, locs)
    settings.apply_layout({k: loc.path for k, loc in locs.items()}, layout)
    bad = [st for st in layout.states() if not st.online]
    if bad:
        with db.session() as s:
            for st in bad:
                record_event(s, Severity.WARNING, component, "storage_offline",
                             f"Storage location '{st.kind}' is offline: {st.message}")
    return layout


def ensure_subdir(layout: Layout, kind: str, name: str) -> Path:
    """Create a subdirectory inside a location **after** verifying it is online."""
    layout.require_online(kind)
    d = layout.path(kind) / name
    d.mkdir(exist_ok=True)
    return d


# --------------------------------------------------------------------------- maintenance

MAINTENANCE_LOCK = "storage-maintenance.lock"


class MaintenanceLock:
    """Held by the migration tool for its whole run (OS lock, released if it dies).

    While held, the worker refuses to start and uploads are refused, so no process writes
    to a location whose files are being copied or whose path is about to change."""

    def __init__(self, settings: Settings):
        self.path = settings.data_dir / MAINTENANCE_LOCK
        self._fd: int | None = None

    def acquire(self) -> bool:
        import fcntl

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        import fcntl

        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def maintenance_active(settings: Settings) -> bool:
    """True if a storage migration currently holds the maintenance lock."""
    probe = MaintenanceLock(settings)
    if not probe.path.exists():
        return False
    if probe.acquire():
        probe.release()
        return False
    return True


def require_no_maintenance(settings: Settings) -> None:
    if maintenance_active(settings):
        raise StorageOffline("Storage maintenance (a location migration) is in progress",
                             details={"reason": "maintenance"})
