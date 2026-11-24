"""HTTP layer only: parsing, status codes, serialization. Logic lives in services/."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app import __version__
from app.core.config import Settings
from app.core.errors import NotFoundError, ServiceUnavailable, ValidationFailed
from app.core.logging import get_logger
from app.db.session import Database
from app.models import Job, JobStatus, Recording, Severity, SystemEvent
from app.schemas.api import (
    EventOut,
    ImportResponse,
    JobOut,
    Page,
    RecordingDetail,
    RecordingOut,
)
from app.services import clips, media, queue, rights_import, system
from app.services.events import record_event
from app.services.ingest import UploadSession, complete_upload
from app.services.storage import safe_join

log = get_logger("api")

WRITE_BUFFER = 1024 * 1024
PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 270" width="480" height="270">'
    '<rect width="480" height="270" fill="#14161c"/>'
    '<path d="M210 105v60l52-30z" fill="#3a3f4b"/>'
    '<text x="240" y="215" fill="#6b7280" font-family="system-ui,sans-serif" font-size="16" '
    'text-anchor="middle">{label}</text></svg>'
)

health_router = APIRouter()
api = APIRouter(prefix="/api/v1")


# --------------------------------------------------------------------------- deps

def get_db(request: Request) -> Database:
    return request.app.state.db


def get_cfg(request: Request) -> Settings:
    return request.app.state.settings


def require_schema(request: Request) -> None:
    if not request.app.state.schema_ok:
        raise ServiceUnavailable(
            "Database schema is not up to date. Run scripts/migrate.sh and restart.",
            code="schema_not_ready")


DB = Annotated[Database, Depends(get_db)]
CFG = Annotated[Settings, Depends(get_cfg)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


# --------------------------------------------------------------------------- serializers

def rec_out(r: Recording, cls=RecordingOut, **extra):
    return cls(**{
        **{k: getattr(r, k) for k in RecordingOut.model_fields
           if k not in ("thumbnail_url", "fps", "fps_rational")},
        "fps": r.fps, "fps_rational": r.fps_rational,
        "thumbnail_url": f"/api/v1/recordings/{r.id}/thumbnail", **extra,
    })


def job_out(j: Job, filename: str | None = None) -> JobOut:
    out = JobOut.model_validate(j)
    out.recording_filename = filename
    out.retryable = j.status == JobStatus.FAILED.value
    return out


# --------------------------------------------------------------------------- health

@health_router.get("/health")
def health() -> dict:
    """Liveness: the process is up and serving requests."""
    return {"status": "ok", "version": __version__}


@health_router.get("/health/ready")
def health_ready(db: DB, cfg: CFG):
    """Readiness: database schema, FFmpeg/ffprobe and data directories."""
    r = system.readiness(db, cfg)
    return JSONResponse(r, status_code=200 if r["ready"] else 503)


# --------------------------------------------------------------------------- system

@api.get("/system/status", dependencies=[Depends(require_schema)])
def system_status(db: DB, cfg: CFG) -> dict:
    st = system.full_status(db, cfg)
    st["recent_errors"] = [EventOut.model_validate(e).model_dump(mode="json")
                           for e in st["recent_errors"]]
    return st


@api.get("/storage", dependencies=[Depends(require_schema)])
def storage(db: DB, cfg: CFG) -> dict:
    """Storage locations, their drives and free space (read-only). Locations are changed
    only with the verified migration tool (scripts/migrate_storage.py)."""
    return system.storage_status(db, cfg)


# --------------------------------------------------------------------------- recordings

@api.get("/recordings", response_model=Page[RecordingOut],
         dependencies=[Depends(require_schema)])
def list_recordings(db: DB, limit: Limit = 50, offset: Offset = 0,
                    q: Annotated[str | None, Query(max_length=200)] = None,
                    status: Annotated[str | None, Query(max_length=20)] = None):
    stmt = select(Recording)
    if q:
        stmt = stmt.where(Recording.original_filename.ilike(f"%{q}%"))
    if status:
        stmt = stmt.where(Recording.status == status.upper())
    with db.session() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(Recording.imported_at.desc(), Recording.id)
                         .limit(limit).offset(offset)).all()
        return Page(items=[rec_out(r) for r in rows], total=total, limit=limit, offset=offset)


@api.get("/recordings/{recording_id}", response_model=RecordingDetail,
         dependencies=[Depends(require_schema)])
def get_recording(recording_id: str, db: DB):
    with db.session() as s:
        r = s.get(Recording, recording_id)
        if r is None:
            raise NotFoundError("Recording not found")
        jobs = s.scalars(select(Job).where(Job.recording_id == r.id)
                         .order_by(Job.created_at)).all()
        return rec_out(r, RecordingDetail,
                       jobs=[job_out(j, r.original_filename) for j in jobs])


@api.post("/recordings/import", status_code=201, response_model=ImportResponse,
          dependencies=[Depends(require_schema)])
async def import_recording(request: Request, db: DB, cfg: CFG,
                           filename: Annotated[str, Query(min_length=1, max_length=1024)],
                           source_id: str | None = None,
                           creator_channel_id: str | None = None,
                           acquisition_method: str | None = None):
    """Streamed upload: the request body is the raw file (no multipart).

    M2: ``source_id`` links the import to an existing source; ``creator_channel_id``
    creates a local source on that channel (covered by its standing licence). Both
    permissions (rights + acquisition method) are checked before a byte is accepted and
    again when linking."""
    if source_id and creator_channel_id:
        raise ValidationFailed("Give either source_id or creator_channel_id, not both")
    if source_id or creator_channel_id:
        await run_in_threadpool(rights_import.precheck, db, cfg, source_id,
                                creator_channel_id, acquisition_method, filename)
    raw_len = request.headers.get("content-length")
    try:
        content_length = int(raw_len) if raw_len is not None else None
    except ValueError:
        raise ValidationFailed("Invalid Content-Length header") from None

    up = await run_in_threadpool(UploadSession, cfg, filename, content_length, db)
    buf = bytearray()
    try:
        async for chunk in request.stream():
            buf += chunk
            if len(buf) >= WRITE_BUFFER:
                data, buf = bytes(buf), bytearray()
                await run_in_threadpool(up.write, data)
        if buf:
            await run_in_threadpool(up.write, bytes(buf))
        digest = await run_in_threadpool(up.finish)
    except ClientDisconnect:
        await run_in_threadpool(up.abort)
        log.warning("upload interrupted by client", extra={"data": {"filename": filename}})
        with db.session() as s:
            record_event(s, Severity.WARNING, "api", "upload_interrupted",
                         f"Upload of '{up.original_filename}' was interrupted; "
                         "partial file removed")
        return Response(status_code=499)
    except BaseException:
        await run_in_threadpool(up.abort)
        raise
    result = await run_in_threadpool(complete_upload, db, cfg, up, digest)
    linked, error = None, None
    if source_id or creator_channel_id:
        linked, error = await run_in_threadpool(
            rights_import.link, db, cfg, result.recording.id, source_id, creator_channel_id,
            acquisition_method, filename)
    return ImportResponse(recording=rec_out(result.recording, source_id=linked),
                          job_id=result.job_id, source_id=linked, source_error=error)


@api.post("/recordings/{recording_id}/clip-suggestions", dependencies=[Depends(require_schema)])
def suggest_clips(recording_id: str, db: DB, cfg: CFG):
    return {"items": clips.suggest(db, cfg, recording_id)}


@api.post("/recordings/{recording_id}/clip", dependencies=[Depends(require_schema)])
def cut_clip(recording_id: str, start_ms: int, end_ms: int, db: DB, cfg: CFG,
             subtitles: bool = False):
    result = clips.cut(db, cfg, recording_id, start_ms, end_ms, subtitles=subtitles)
    return Response(result, media_type="video/mp4", headers={
        "Content-Disposition": f'attachment; filename="clip-{start_ms}-{end_ms}.mp4"',
        "Cache-Control": "no-store",
    })


@api.get("/recordings/{recording_id}/thumbnail", dependencies=[Depends(require_schema)])
def recording_thumbnail(recording_id: str, db: DB, cfg: CFG):
    with db.session() as s:
        r = s.get(Recording, recording_id)
        if r is None:
            raise NotFoundError("Recording not found")
        state = r.thumbnail_status
    path: Path = safe_join(cfg.thumbnails_dir, f"{recording_id}.jpg")
    if state == "READY" and media.is_valid_jpeg(path):
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})
    label = {"PENDING": "Generating thumbnail…", "FAILED": "No preview available"}.get(
        state, "No preview available")
    return Response(PLACEHOLDER_SVG.format(label=label), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store", "X-Thumbnail-Fallback": "true"})


# --------------------------------------------------------------------------- jobs

@api.get("/jobs", response_model=Page[JobOut], dependencies=[Depends(require_schema)])
def list_jobs(db: DB, limit: Limit = 50, offset: Offset = 0,
              status: Annotated[str | None, Query(max_length=20)] = None,
              job_type: Annotated[str | None, Query(max_length=20)] = None,
              recording_id: Annotated[str | None, Query(max_length=36)] = None):
    stmt = select(Job, Recording.original_filename).outerjoin(Recording)
    if status:
        stmt = stmt.where(Job.status == status.upper())
    if job_type:
        stmt = stmt.where(Job.job_type == job_type.upper())
    if recording_id:
        stmt = stmt.where(Job.recording_id == recording_id)
    with db.session() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.execute(stmt.order_by(Job.created_at.desc(), Job.id)
                         .limit(limit).offset(offset)).all()
        return Page(items=[job_out(j, fn) for j, fn in rows], total=total, limit=limit,
                    offset=offset)


@api.get("/jobs/{job_id}", response_model=JobOut, dependencies=[Depends(require_schema)])
def get_job(job_id: str, db: DB):
    with db.session() as s:
        j = s.get(Job, job_id)
        if j is None:
            raise NotFoundError("Job not found")
        return job_out(j, j.recording.original_filename if j.recording else None)


@api.post("/jobs/{job_id}/retry", response_model=JobOut,
          dependencies=[Depends(require_schema)])
def retry_job(job_id: str, db: DB):
    with db.session() as s:
        j = queue.retry_job(s, job_id)
        s.flush()
        return job_out(j, j.recording.original_filename if j.recording else None)


# --------------------------------------------------------------------------- events

@api.get("/events", response_model=Page[EventOut], dependencies=[Depends(require_schema)])
def list_events(db: DB, limit: Limit = 50, offset: Offset = 0,
                severity: Annotated[str | None, Query(max_length=10)] = None,
                component: Annotated[str | None, Query(max_length=64)] = None,
                recording_id: Annotated[str | None, Query(max_length=36)] = None):
    stmt = select(SystemEvent)
    if severity:
        stmt = stmt.where(SystemEvent.severity == severity.upper())
    if component:
        stmt = stmt.where(SystemEvent.component == component)
    if recording_id:
        stmt = stmt.where(SystemEvent.related_recording_id == recording_id)
    with db.session() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = s.scalars(stmt.order_by(SystemEvent.timestamp.desc(), SystemEvent.id.desc())
                         .limit(limit).offset(offset)).all()
        return Page(items=[EventOut.model_validate(e) for e in rows], total=total,
                    limit=limit, offset=offset)
