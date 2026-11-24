"""FastAPI application factory.

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.rights_routes import m2
from app.api.routes import api, health_router
from app.core.config import REPO_ROOT, Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.db.migrations import schema_status
from app.db.session import Database
from app.models import Severity
from app.services import media, queue
from app.services.events import record_event
from app.services.ingest import reconcile
from app.services.locations import load_layout, maintenance_active

log = get_logger("api")


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message,
                                   "details": details or {}}}, status_code=status)


def startup_checks(app: FastAPI) -> None:
    s: Settings = app.state.settings
    db: Database = app.state.db
    st = schema_status(db.engine)
    app.state.schema_ok = st["ok"]
    if not st["ok"]:
        log.error("database schema not at head; run scripts/migrate.sh",
                  extra={"data": st})
        return
    # Storage layout first: nothing touches a file before every location is resolved.
    load_layout(db, s, "api")
    if maintenance_active(s):
        # A storage migration is running: never clean up or recover now (a reconcile could
        # remove files being migrated). Uploads are refused until it ends.
        with db.session() as sess:
            record_event(sess, Severity.WARNING, "api", "api_started_during_maintenance",
                         "API started while a storage migration runs: recovery and cleanup "
                         "skipped; uploads refused until the migration ends")
        return
    expired = queue.recover_stuck_jobs(
        db, kill_orphan=media.kill_orphan_if_matches,
        backoff_base=s.job_backoff_base_seconds, backoff_cap=s.job_backoff_max_seconds,
        recoverer="api")
    rep = reconcile(db, s, "api")
    ff = media.tool_status(s.ffmpeg_path)
    with db.session() as sess:
        record_event(sess, Severity.INFO, "api", "api_started",
                     f"API {__version__} started: {len(expired)} expired lease(s) recovered, "
                     f"{len(rep.removed_temp)} stale temp file(s) removed, ffmpeg "
                     f"{'available' if ff.available else 'MISSING'}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_internal_dirs()  # locations are resolved/created by load_layout
        configure_logging("api", settings.logs_dir, settings.log_level,
                          settings.log_max_bytes, settings.log_backup_count)
        app.state.db = Database(settings.database_url)
        startup_checks(app)
        log.info("api ready", extra={"data": settings.safe_summary()})
        yield
        app.state.db.dispose()
        log.info("api stopped")

    app = FastAPI(title="RIFTWIRE", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.settings = settings
    app.state.schema_ok = False

    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
                       allow_credentials=False)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, e: AppError):
        return _error(e.status_code, e.code, e.message, e.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        errs = [{"loc": list(x.get("loc", [])), "msg": x.get("msg")} for x in e.errors()]
        return _error(422, "request_validation", "Invalid request", {"errors": errs})

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
        return _error(e.status_code, code, str(e.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, e: Exception):
        log.error("unhandled error", exc_info=e)
        return _error(500, "internal_error", "Internal server error")

    app.include_router(health_router)
    app.include_router(api)
    app.include_router(m2)
    _mount_frontend(app, REPO_ROOT / "frontend" / "dist")
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built dashboard (if present) with SPA fallback."""
    if not (dist / "index.html").exists():
        return
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            return _error(404, "not_found", "Not found")
        candidate = (root / path).resolve()
        if path and root in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})


app = create_app()
