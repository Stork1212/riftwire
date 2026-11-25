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
from dataclasses import replace
from pathlib import Path

from sqlalchemy import update

from app.core.config import Settings, get_settings
from app.core.errors import (
    DependencyPendingJobError,
    JobError,
    RightsRefusedJobError,
    StorageOfflineJobError,
)
from app.core.logging import configure_logging, get_logger
from app.db.base import utcnow
from app.db.migrations import schema_status
from app.db.session import Database
from app.models import Severity, Worker
from app.services import media, queue, rights
from app.services.events import record_event
from app.services.ingest import reconcile
from app.services.locations import (
    MAINTENANCE_LOCK,
    Layout,
    get_layout,
    load_layout,
    maintenance_active,
)
from app.workers.handlers import HANDLERS, REQUIRED_KINDS, JobContext

log = get_logger("worker")

EXIT_OK = 0
EXIT_SCHEMA = 2
EXIT_LOCKED = 3
STORAGE_RETRY_SECONDS = 30.0
DEPENDENCY_RETRY_SECONDS = 5.0


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
        self._offline_kinds: set[str] = set()
        self._offline_lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def startup(self) -> None:
        st = schema_status(self.db.engine)
        if not st["ok"]:
            raise RuntimeError(f"database schema not at head ({st}); run scripts/migrate.sh")
        self.lock.acquire()
        if maintenance_active(self.settings):
            self.lock.release()
            raise WorkerLockedError("a storage migration is in progress "
                                    f"({MAINTENANCE_LOCK} is held)")
        # Storage layout first: no file is touched before every location is resolved.
        load_layout(self.db, self.settings, "worker")
        with self.db.session() as s:
            now = utcnow()
            s.merge(Worker(id=self.id, hostname=socket.gethostname(), pid=os.getpid(),
                           concurrency=self.settings.worker_concurrency, started_at=now,
                           last_heartbeat_at=now, status="RUNNING"))
            # Holding the lock proves every other worker row is dead.
            s.execute(update(Worker).where(Worker.id != self.id, Worker.status == "RUNNING")
                      .values(status="DEAD"))
        # Recover first: clearing dead attempts' tokens lets the reconciler remove their
        # leftover staging/temp files in the same startup pass.
        recovered = self._recover(foreign=True)
        rep = reconcile(self.db, self.settings, "worker")
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
        self._heartbeat_thread = hb
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
        # The heartbeat thread's final beat writes STOPPING: it must finish before the final
        # STOPPED write, or it can overwrite it (smoke test, 2026-11-24).
        hb = getattr(self, "_heartbeat_thread", None)
        if hb is not None:
            hb.join(15)
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
            foreign_to_worker=self.id if foreign else None, recoverer=self.id)

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

    # ---------------------------------------------------------------- storage
    def blocked_job_types(self) -> set:
        """Job types that must not start because a location they need is offline.
        Records an event whenever a location goes offline or comes back."""
        layout: Layout = get_layout(self.settings)
        age = self.settings.storage_check_seconds
        states = {k: layout.state(k, max_age=age)
                  for k in {k for kinds in REQUIRED_KINDS.values() for k in kinds}}
        offline = {k for k, st in states.items() if not st.online}
        with self._offline_lock:
            went_off = offline - self._offline_kinds
            came_back = self._offline_kinds - offline
            self._offline_kinds = offline
        if went_off or came_back:
            try:
                with self.db.session() as s:
                    for k in sorted(went_off):
                        record_event(s, Severity.WARNING, "worker", "storage_offline",
                                     f"Storage location '{k}' went offline "
                                     f"({states[k].message}); jobs that need it are waiting")
                    for k in sorted(came_back):
                        record_event(s, Severity.INFO, "worker", "storage_online",
                                     f"Storage location '{k}' is back online; resuming")
            except Exception:
                log.exception("could not record storage event")
            if came_back:
                # Remount: clean up the returning location before resuming its jobs.
                try:
                    reconcile(self.db, self.settings, "worker")
                except Exception:
                    log.exception("reconcile after remount failed")
        return {t for t, kinds in REQUIRED_KINDS.items() if offline.intersection(kinds)}

    def _claim_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                claimed = queue.claim_next(self.db, self.id, self.settings.job_lease_seconds,
                                           exclude_types=self.blocked_job_types())
            except Exception:
                log.exception("claim failed")
                self.stop_event.wait(self.settings.worker_poll_seconds)
                continue
            if claimed is None:
                self.stop_event.wait(self.settings.worker_poll_seconds)
                continue
            self._execute(claimed)

    def _authorize(self, claimed: queue.ClaimedJob) -> queue.ClaimedJob:
        """Rights gate before a gated job runs (M2): the decision travels with the job so
        its completion is fenced against it. Raises ``RightsRefusedJobError``."""
        use = rights.JOB_USE[claimed.job_type]
        if use is None:
            return claimed
        with self.db.session() as s:
            d = rights.evaluate_recording(s, claimed.recording_id, use)
        if not d.allowed:
            raise RightsRefusedJobError(f"not authorized for {use}: {d.message} "
                                        f"[{d.reason}]")
        return replace(claimed, decision=d)

    def _execute(self, claimed: queue.ClaimedJob) -> None:
        extra = {"job_id": claimed.id, "recording_id": claimed.recording_id,
                 "worker_id": self.id}
        ctx = JobContext(self.db, self.settings, claimed)
        with self._active_lock:
            self.active[claimed.id] = ctx
        log.info(f"running {claimed.job_type.value} (attempt {claimed.attempt})", extra=extra)
        try:
            claimed = self._authorize(claimed)
            ctx.claimed = claimed
            HANDLERS[claimed.job_type](ctx)
            log.info(f"{claimed.job_type.value} completed", extra=extra)
        except queue.LeaseLost:
            log.warning("attempt abandoned: lease lost", extra=extra)
        except media.CommandCancelled:
            if ctx.lost.is_set():
                log.warning("attempt abandoned: lease lost", extra=extra)
            else:
                self._safe(lambda: queue.release(self.db, claimed, "released on shutdown"))
        except RightsRefusedJobError as e:
            msg = f"RightsRefused: {e}"
            log.warning(f"{claimed.job_type.value} refused: {e}", extra=extra)
            self._safe(lambda: self._record_refusal(claimed, msg))
            self._safe(lambda: queue.fail(
                self.db, claimed, msg, permanent=True,
                backoff_base=self.settings.job_backoff_base_seconds,
                backoff_cap=self.settings.job_backoff_max_seconds))
        except DependencyPendingJobError as e:
            why = str(e)
            log.info(f"waiting for a dependency; deferring: {why}", extra=extra)
            self._safe(lambda: queue.defer(self.db, claimed, why, DEPENDENCY_RETRY_SECONDS,
                                           waiting_for="a dependency"))
        except StorageOfflineJobError as e:
            why = str(e)
            log.warning(f"storage offline; deferring: {why}", extra=extra)
            self._safe(lambda: queue.defer(self.db, claimed, why, STORAGE_RETRY_SECONDS))
        except Exception as e:
            offline = get_layout(self.settings).offline(REQUIRED_KINDS[claimed.job_type])
            if offline:  # the drive vanished mid-job: not the job's fault
                why = "; ".join(f"{st.kind}: {st.message}" for st in offline)
                log.warning(f"storage offline after error ({type(e).__name__}: {e}); "
                            "deferring", extra=extra)
                self._safe(lambda: queue.defer(self.db, claimed, why, STORAGE_RETRY_SECONDS))
                return
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

    def _record_refusal(self, claimed: queue.ClaimedJob, msg: str) -> None:
        with self.db.session() as s:
            record_event(s, Severity.WARNING, "worker", "rights_refused", msg,
                         job_id=claimed.id, recording_id=claimed.recording_id)
            rights.provenance(s, "job", claimed.id, "rights_refused", actor="system",
                              job_type=claimed.job_type.value,
                              recording_id=claimed.recording_id, message=msg)

    def _safe(self, fn) -> None:
        try:
            fn()
        except queue.LeaseLost:
            log.warning("could not record outcome: lease lost")
        except Exception:
            log.exception("could not record job outcome")


def main() -> int:
    settings = get_settings()
    settings.ensure_internal_dirs()  # locations are resolved/created by load_layout
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
