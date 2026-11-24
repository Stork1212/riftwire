"""System status and readiness (no web framework imports)."""

from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from dataclasses import asdict

from sqlalchemy import func, select

from app import __version__
from app.core.config import Settings
from app.db.base import utcnow
from app.db.migrations import schema_status
from app.db.session import Database
from app.models import (
    Job,
    JobStatus,
    Recording,
    Severity,
    StorageMigration,
    StorageMigrationItem,
    SystemEvent,
    Worker,
)
from app.services import media, reservations
from app.services.locations import MARKER, Layout

_TOOL_CACHE: dict[str, tuple[float, media.ToolStatus]] = {}
_TOOL_LOCK = threading.Lock()
TOOL_CACHE_SECONDS = 60.0


def cached_tool_status(executable: str) -> media.ToolStatus:
    now = time.monotonic()
    with _TOOL_LOCK:
        hit = _TOOL_CACHE.get(executable)
        if hit and now - hit[0] < TOOL_CACHE_SECONDS:
            return hit[1]
    st = media.tool_status(executable)
    with _TOOL_LOCK:
        _TOOL_CACHE[executable] = (now, st)
    return st


def clear_tool_cache() -> None:
    with _TOOL_LOCK:
        _TOOL_CACHE.clear()


def worker_status(db: Database, settings: Settings) -> dict:
    with db.session() as s:
        w = s.scalars(select(Worker).order_by(Worker.last_heartbeat_at.desc())).first()
    if w is None:
        return {"state": "NEVER_STARTED", "id": None, "last_heartbeat_at": None,
                "current_job_id": None, "concurrency": None}
    age = (utcnow() - w.last_heartbeat_at).total_seconds()
    if w.status in ("RUNNING", "STOPPING"):
        state = w.status if age <= settings.worker_stale_after_seconds else "UNRESPONSIVE"
    else:
        state = "STOPPED"
    return {"state": state, "id": w.id, "last_heartbeat_at": w.last_heartbeat_at,
            "heartbeat_age_seconds": round(age, 1), "current_job_id": w.current_job_id,
            "concurrency": w.concurrency}


_USAGE_CACHE: dict[str, tuple[float, int]] = {}
USAGE_CACHE_SECONDS = 60.0
USAGE_MAX_ENTRIES = 200_000


def _dir_usage(path) -> int | None:
    """Bytes of regular files under ``path`` (cached; bounded walk). None if too large."""
    key = str(path)
    now = time.monotonic()
    hit = _USAGE_CACHE.get(key)
    if hit and now - hit[0] < USAGE_CACHE_SECONDS:
        return hit[1]
    total, n = 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f == MARKER:
                continue  # the location's identity marker is not content
            n += 1
            if n > USAGE_MAX_ENTRIES:
                return None
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    _USAGE_CACHE[key] = (now, total)
    return total


def _migrations(db: Database, limit: int = 5) -> list[dict]:
    with db.session() as s:
        rows = list(s.scalars(select(StorageMigration)
                              .order_by(StorageMigration.updated_at.desc()).limit(limit)))
        out = []
        for m in rows:
            verified = s.scalar(select(func.count()).select_from(StorageMigrationItem).where(
                StorageMigrationItem.migration_id == m.id,
                StorageMigrationItem.state == "VERIFIED"))
            out.append({"id": m.id, "kind": m.kind, "source_path": m.source_path,
                        "dest_path": m.dest_path, "status": m.status,
                        "total_files": m.total_files, "verified_files": int(verified or 0),
                        "total_bytes": m.total_bytes, "updated_at": m.updated_at.isoformat()})
        return out


def storage_status(db: Database, settings: Settings) -> dict:
    """Data-dir summary (Phase 1 fields) plus per-location and per-volume detail (M1)."""
    du = shutil.disk_usage(settings.data_dir)
    with db.session() as s:
        rec_bytes = s.scalar(select(func.coalesce(func.sum(Recording.file_size_bytes), 0)))
    out = {
        "total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free,
        "min_free_bytes": settings.min_free_disk_bytes,
        "importable_bytes": max(0, du.free - settings.min_free_disk_bytes),
        "recordings_bytes": int(rec_bytes or 0),
        "below_floor": du.free < settings.min_free_disk_bytes,
        "locations": [], "volumes": [], "migrations": _migrations(db),
    }
    layout = settings.layout
    if not isinstance(layout, Layout):
        return out
    volumes: dict[str, dict] = {}
    for st in layout.states():
        loc = layout.locations.get(st.kind)
        if loc is None:
            out["locations"].append({"kind": st.kind, "online": False, "reason": st.reason,
                                     "message": st.message})
            continue
        usage = None
        if st.online:
            usage = int(rec_bytes or 0) if st.kind == "recordings" else _dir_usage(loc.path)
        out["locations"].append({
            "kind": st.kind, "path": str(loc.path), "online": st.online, "reason": st.reason,
            "message": st.message, "fs_type": st.fs_type or loc.fs_type,
            "volume_uuid": loc.volume_uuid, "volume_label": st.label,
            "mount_point": st.mount_point, "usage_bytes": usage,
        })
        v = volumes.setdefault(loc.volume_uuid, {
            "volume_uuid": loc.volume_uuid, "kinds": [], "online": True,
            "label": st.label, "mount_point": st.mount_point,
            "floor_bytes": layout.floor_for_volume(loc.volume_uuid),
            "total_bytes": None, "free_bytes": None})
        v["kinds"].append(st.kind)
        v["online"] = v["online"] and st.online
        if st.online and v["free_bytes"] is None:
            vdu = shutil.disk_usage(loc.path)
            v.update(total_bytes=vdu.total, free_bytes=vdu.free,
                     label=v["label"] or st.label, mount_point=st.mount_point)
    for v in volumes.values():
        v["reserved_bytes"] = reservations.reserved_bytes(db, v["volume_uuid"])
        if v["free_bytes"] is not None:
            v["headroom_bytes"] = max(0, v["free_bytes"] - v["reserved_bytes"]
                                      - v["floor_bytes"])
            v["below_floor"] = v["free_bytes"] < v["floor_bytes"]
        else:
            v["headroom_bytes"] = None
            v["below_floor"] = None
        out["volumes"].append(v)
    return out


def counts(db: Database) -> dict:
    with db.session() as s:
        n_rec = s.scalar(select(func.count()).select_from(Recording))
        dur = s.scalar(select(func.coalesce(func.sum(Recording.duration_ms), 0)))
        by_status = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        rec_status = dict(s.execute(select(Recording.status, func.count())
                                    .group_by(Recording.status)).all())
    return {
        "recordings": n_rec,
        "recordings_by_status": rec_status,
        "total_duration_ms": int(dur or 0),
        "jobs": {st.value: by_status.get(st.value, 0) for st in JobStatus},
    }


def recent_errors(db: Database, limit: int = 10) -> list[SystemEvent]:
    with db.session() as s:
        return list(s.scalars(select(SystemEvent).where(
            SystemEvent.severity.in_([Severity.ERROR.value, Severity.CRITICAL.value]))
            .order_by(SystemEvent.timestamp.desc(), SystemEvent.id.desc()).limit(limit)))


def readiness(db: Database, settings: Settings) -> dict:
    checks: dict[str, dict] = {}
    try:
        checks["database"] = schema_status(db.engine)
    except Exception as e:
        checks["database"] = {"ok": False, "error": str(e)}
    for name, exe in (("ffmpeg", settings.ffmpeg_path), ("ffprobe", settings.ffprobe_path)):
        st = cached_tool_status(exe)
        checks[name] = {"ok": st.available, "version": st.version}
    missing = [str(d) for d in settings.required_dirs() if not d.is_dir()]
    checks["data_dirs"] = {"ok": not missing, "missing": missing}
    layout = settings.layout
    if isinstance(layout, Layout):
        states = layout.states()
        offline = [f"storage_offline:{st.kind}" for st in states if not st.online]
        checks["storage"] = {"ok": not offline, "offline": offline,
                             "locations": [{"kind": st.kind, "online": st.online,
                                            "reason": st.reason} for st in states]}
    else:
        checks["storage"] = {"ok": False, "offline": [], "error": "storage layout not loaded"}
    return {"ready": all(c["ok"] for c in checks.values()), "checks": checks}


def full_status(db: Database, settings: Settings) -> dict:
    ff = cached_tool_status(settings.ffmpeg_path)
    fp = cached_tool_status(settings.ffprobe_path)
    return {
        "version": __version__,
        "environment": settings.env,
        "backend": {"status": "ok"},
        "readiness": readiness(db, settings),
        "worker": worker_status(db, settings),
        "ffmpeg": {**asdict(ff), "videotoolbox": "videotoolbox" in ff.hwaccels},
        "ffprobe": asdict(fp),
        "data_dir": str(settings.data_dir),
        "storage": storage_status(db, settings),
        "counts": counts(db),
        "recent_errors": recent_errors(db),
        "settings": settings.safe_summary(),
    }
