"""System status and readiness (no web framework imports)."""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import asdict

from sqlalchemy import func, select

from app import __version__
from app.core.config import Settings
from app.db.base import utcnow
from app.db.migrations import schema_status
from app.db.session import Database
from app.models import Job, JobStatus, Recording, Severity, SystemEvent, Worker
from app.services import media

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


def storage_status(db: Database, settings: Settings) -> dict:
    du = shutil.disk_usage(settings.data_dir)
    with db.session() as s:
        rec_bytes = s.scalar(select(func.coalesce(func.sum(Recording.file_size_bytes), 0)))
    return {
        "total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free,
        "min_free_bytes": settings.min_free_disk_bytes,
        "importable_bytes": max(0, du.free - settings.min_free_disk_bytes),
        "recordings_bytes": int(rec_bytes or 0),
        "below_floor": du.free < settings.min_free_disk_bytes,
    }


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
