"""RIFTWIRE worker process.

    python -m app.workers.worker

* Exactly one worker process per data directory (exclusive OS lock).
* ``RIFTWIRE_WORKER_CONCURRENCY`` threads inside it (default 1).
* Heartbeat thread renews leases (fenced) and the worker's liveness row.
* Recovers interrupted jobs at startup and expired leases while running.
* SIGINT/SIGTERM: stop claiming, finish within the grace period, then cancel + release.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import update

from app.core.config import Settings, get_settings
from app.core.errors import JobError
from app.core.logging import configure_logging, get_logger
from app.db.base import utcnow
from app.db.migrations import schema_status
from app.db.session import Database
from app.models import Severity, Worker
from app.services import media, queue
from app.services.events import record_event
from app.services.ingest import reconcile
from app.workers.handlers import HANDLERS, JobContext

log = get_logger("worker")

EXIT_OK = 0
EXIT_SCHEMA = 2
EXIT_LOCKED = 3


class WorkerLockedError(RuntimeError):
    pass


class WorkerLock:
    """Exclusive, crash-safe lock: the OS releases it when the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WorkerLockedError(f"another worker holds {self.path}") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class WorkerProcess:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.id = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
        self.stop_event = threading.Event()
        self.active: dict[str, JobContext] = {}
        self._active_lock = threading.Lock()
        self.lock = WorkerLock(settings.data_dir / "worker.lock")
        self.threads: list[threading.Thread] = []

    # ---------------------------------------------------------------- lifecycle
    def startup(self) -> None:
        st = schema_status(self.db.engine)
        if not st["ok"]:
            raise RuntimeError(f"database schema not at head ({st}); run scripts/migrate.sh")
        self.lock.acquire()
        with self.db.session() as s:
            now = utcnow()
            s.merge(Worker(id=self.id, hostname=socket.gethostname(), pid=os.getpid(),
                           concurrency=self.settings.worker_concurrency, started_at=now,
                           last_heartbeat_at=now, status="RUNNING"))
            # Holding the lock proves every other worker row is dead.
            s.execute(update(Worker).where(Worker.id != self.id, Worker.status == "RUNNING")
                      .values(status="DEAD"))
        rep = reconcile(self.db, self.settings, "worker")
        recovered = self._recover(foreign=True)
        tools = media.tool_status(self.settings.ffmpeg_path)
        with self.db.session() as s:
            record_event(s, Severity.INFO, "worker", "worker_started",
                         f"Worker {self.id} ready (concurrency "
                         f"{self.settings.worker_concurrency}); recovered {len(recovered)} "
                         f"interrupted job(s); temp files removed {len(rep.removed_temp)}; "
                         f"ffmpeg {'available' if tools.available else 'MISSING'}")
            if not tools.available:
                record_event(s, Severity.ERROR, "worker", "ffmpeg_missing",
                             "FFmpeg not found; media jobs will fail and retry")

    def run(self) -> None:
        for i in range(self.settings.worker_concurrency):
            t = threading.Thread(target=self._claim_loop, name=f"job-{i}", daemon=True)
            t.start()
            self.threads.append(t)
        hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        next_recovery = 0.0
        while not self.stop_event.is_set():
            if time.monotonic() >= next_recovery:
                try:
                    self._recover(foreign=False)
                except Exception:
                    log.exception("lease recovery failed")
                next_recovery = time.monotonic() + max(1.0, self.settings.job_lease_seconds / 2)
            self.stop_event.wait(self.settings.worker_poll_seconds)
        self._shutdown()

    def request_stop(self, *_args) -> None:
        if not self.stop_event.is_set():
            log.info("shutdown requested", extra={"worker_id": self.id})
        self.stop_event.set()

    def _shutdown(self) -> None:
        deadline = time.monotonic() + self.settings.worker_shutdown_grace_seconds
        for t in self.threads:
            t.join(max(0.0, deadline - time.monotonic()))
        with self._active_lock:
            for ctx in self.active.values():
                ctx.cancel.set()
        for t in self.threads:
            t.join(15)
        try:
            with self.db.session() as s:
                s.execute(update(Worker).where(Worker.id == self.id)
                          .values(status="STOPPED", last_heartbeat_at=utcnow(),
                                  current_job_id=None))
                record_event(s, Severity.INFO, "worker", "worker_stopped",
                             f"Worker {self.id} stopped")
        finally:
            self.lock.release()

    # ---------------------------------------------------------------- internals
    def _recover(self, foreign: bool) -> list[queue.Recovered]:
        return queue.recover_stuck_jobs(
            self.db, kill_orphan=media.kill_orphan_if_matches,
            backoff_base=self.settings.job_backoff_base_seconds,
            backoff_cap=self.settings.job_backoff_max_seconds,
            foreign_to_worker=self.id if foreign else None)

    def _heartbeat_loop(self) -> None:
        interval = min(self.settings.worker_heartbeat_seconds,
                       self.settings.job_lease_seconds / 3)
        while not self.stop_event.wait(interval):
            self._beat()
        self._beat()

    def _beat(self) -> None:
        with self._active_lock:
            ctxs = list(self.active.values())
        for ctx in ctxs:
            try:
                ok = queue.renew_lease(self.db, ctx.job_id, ctx.claimed.token,
                                       self.settings.job_lease_seconds)
            except Exception:
                log.exception("lease renewal error", extra={"job_id": ctx.job_id})
                continue
            if not ok:
                log.warning("lease lost; aborting attempt", extra={"job_id": ctx.job_id})
                ctx.lost.set()
        try:
            with self.db.session() as s:
                s.execute(update(Worker).where(Worker.id == self.id).values(
                    last_heartbeat_at=utcnow(),
                    status="STOPPING" if self.stop_event.is_set() else "RUNNING",
                    current_job_id=ctxs[0].job_id if ctxs else None))
        except Exception:
            log.exception("heartbeat write failed")

    def _claim_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                claimed = queue.claim_next(self.db, self.id, self.settings.job_lease_seconds)
            except Exception:
                log.exception("claim failed")
                self.stop_event.wait(self.settings.worker_poll_seconds)
                continue
            if claimed is None:
                self.stop_event.wait(self.settings.worker_poll_seconds)
                continue
            self._execute(claimed)

    def _execute(self, claimed: queue.ClaimedJob) -> None:
        ctx = JobContext(self.db, self.settings, claimed)
        extra = {"job_id": claimed.id, "recording_id": claimed.recording_id,
                 "worker_id": self.id}
        with self._active_lock:
            self.active[claimed.id] = ctx
        log.info(f"running {claimed.job_type.value} (attempt {claimed.attempt})", extra=extra)
        try:
            HANDLERS[claimed.job_type](ctx)
            log.info(f"{claimed.job_type.value} completed", extra=extra)
        except queue.LeaseLost:
            log.warning("attempt abandoned: lease lost", extra=extra)
        except media.CommandCancelled:
            if ctx.lost.is_set():
                log.warning("attempt abandoned: lease lost", extra=extra)
            else:
                self._safe(lambda: queue.release(self.db, claimed, "released on shutdown"))
        except Exception as e:
            permanent = isinstance(e, (JobError, media.MediaError)) and e.permanent
            msg = f"{type(e).__name__}: {e}"
            log.error(f"{claimed.job_type.value} failed: {msg}", extra=extra,
                      exc_info=not permanent)
            self._safe(lambda: queue.fail(
                self.db, claimed, msg, permanent=permanent,
                backoff_base=self.settings.job_backoff_base_seconds,
                backoff_cap=self.settings.job_backoff_max_seconds))
        finally:
            with self._active_lock:
                self.active.pop(claimed.id, None)

    def _safe(self, fn) -> None:
        try:
            fn()
        except queue.LeaseLost:
            log.warning("could not record outcome: lease lost")
        except Exception:
            log.exception("could not record job outcome")


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    configure_logging("worker", settings.logs_dir, settings.log_level,
                      settings.log_max_bytes, settings.log_backup_count)
    db = Database(settings.database_url)
    w = WorkerProcess(settings, db)
    signal.signal(signal.SIGTERM, w.request_stop)
    signal.signal(signal.SIGINT, w.request_stop)
    try:
        w.startup()
    except WorkerLockedError as e:
        log.error(f"refusing to start: {e}")
        return EXIT_LOCKED
    except RuntimeError as e:
        log.error(str(e))
        return EXIT_SCHEMA
    log.info("worker ready", extra={"worker_id": w.id,
                                    "data": settings.safe_summary()})
    w.run()
    db.dispose()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
