"""SQLite-backed persistent job queue (ARCHITECTURE §7).

Guarantees:
* Claiming is a compare-and-set UPDATE, so each attempt is owned by exactly one worker.
* Each attempt carries a random ``lease_token``. Every write made on behalf of a running
  attempt goes through :func:`fenced`, which first verifies the job is still RUNNING with
  that token. A "zombie" attempt (lease expired, job re-claimed) cannot change anything.
* Expired leases are recovered to RETRY_WAIT / FAILED, after any recorded FFmpeg child
  has been confirmed dead (PID-reuse safe).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    NotFoundError,
    RightsRefused,
    RightsRefusedJobError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import Database
from app.models import Job, JobStatus, JobType, Recording, RecordingStatus, Severity
from app.services import acquisition, rights, sources
from app.services.events import record_event

log = get_logger("queue")

ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.RETRY_WAIT},
    JobStatus.RETRY_WAIT: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.FAILED: {JobStatus.PENDING},
    JobStatus.COMPLETED: set(),
    JobStatus.CANCELLED: set(),
}

CLAIMABLE = (JobStatus.PENDING.value, JobStatus.RETRY_WAIT.value)
NEXT_STAGE = {JobType.INGEST: JobType.METADATA, JobType.METADATA: JobType.THUMBNAIL}


class InvalidTransition(Exception):
    pass


class LeaseLost(Exception):
    """The attempt no longer owns the job (lease expired and/or re-claimed)."""


def check_transition(current: str, target: JobStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[JobStatus(current)]:
        raise InvalidTransition(f"{current} -> {target.value} is not allowed")


def backoff_seconds(attempt: int, base: float, cap: float) -> float:
    if base <= 0:
        return 0.0
    return float(min(cap, base * (2 ** max(0, attempt - 1))))


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    job_type: JobType
    recording_id: str | None          # None for an ACQUIRE job until its recording exists
    token: str
    attempt: int
    max_attempts: int
    source_id: str | None = None      # ACQUIRE jobs
    # Set by the worker for rights-gated job types (M2): the decision the job runs under.
    # Completion re-checks it inside the same transaction (rights fence).
    decision: rights.Decision | None = None


# ---------------------------------------------------------------------------- enqueue

def enqueue(session: Session, recording_id: str, job_type: JobType, max_attempts: int) -> Job:
    """Idempotent: returns the existing job of this type for the recording if present.

    Rights-gated job types (``rights.JOB_USE``) are never created for media whose rights
    do not allow the use: ``RightsRefused`` is raised instead (M2)."""
    existing = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                                Job.job_type == job_type.value))
    if existing is not None:
        return existing
    use = rights.JOB_USE[job_type]
    if use is not None:
        d = rights.evaluate_recording(session, recording_id, use)
        if not d.allowed:
            raise d.refuse()
    now = utcnow()
    job = Job(recording_id=recording_id, job_type=job_type.value,
              status=JobStatus.PENDING.value, max_attempts=max_attempts,
              created_at=now, updated_at=now, next_attempt_at=now)
    session.add(job)
    session.flush()
    return job


def enqueue_acquire(session: Session, settings, source_id: str, max_attempts: int) -> Job:
    """Queue fetching a saved source URL into a linked recording (``url_download``).

    Idempotent: one ACQUIRE job per source; the existing one is returned. Checked before
    queueing (a saved URL, URL fetching permitted by the current rights with written
    platform permission, URL acquisition switched on), so a job that could only be refused
    is never created; the handler checks again before and after the download."""
    existing = session.scalar(select(Job).where(Job.source_id == source_id,
                                                Job.job_type == JobType.ACQUIRE.value))
    if existing is not None:
        return existing
    src = sources.get_source(session, source_id)
    if not src.url_canonical:
        raise ValidationFailed("Save a YouTube or Twitch video link before acquiring.")
    acquisition.check_acquire(session, settings, source_id,
                              acquisition.AcquisitionMethod.URL_FETCH.value, fetched=True)
    now = utcnow()
    job = Job(source_id=source_id, job_type=JobType.ACQUIRE.value,
              status=JobStatus.PENDING.value, max_attempts=max_attempts,
              created_at=now, updated_at=now, next_attempt_at=now)
    session.add(job)
    session.flush()
    record_event(session, Severity.INFO, "queue", "acquire_queued",
                 f"Acquisition of source {source_id} queued", job_id=job.id)
    return job


# ---------------------------------------------------------------------------- claiming

def claim_next(db: Database, worker_id: str, lease_seconds: int,
               now: datetime | None = None,
               exclude_types: set[JobType] | frozenset[JobType] = frozenset(),
               ) -> ClaimedJob | None:
    """Claim the oldest eligible job. ``exclude_types``: job types that must not start now
    (their storage location is offline); they stay queued and keep their attempts."""
    now = now or utcnow()
    with db.session() as s:
        stmt = select(Job.id).where(
            Job.status.in_(CLAIMABLE),
            or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
        )
        if exclude_types:
            stmt = stmt.where(Job.job_type.not_in([t.value for t in exclude_types]))
        candidates = s.execute(stmt.order_by(Job.created_at, Job.id).limit(10)).scalars().all()
        for job_id in candidates:
            token = secrets.token_hex(16)
            res = s.execute(
                update(Job).where(
                    Job.id == job_id,
                    Job.status.in_(CLAIMABLE),
                    or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
                ).values(
                    status=JobStatus.RUNNING.value, claimed_by=worker_id, lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    attempt_count=Job.attempt_count + 1, started_at=now, updated_at=now,
                    completed_at=None, progress_percent=0, process_pid=None,
                    process_started_at=None,
                )
            )
            if res.rowcount == 1:
                job = s.get(Job, job_id)
                return ClaimedJob(job.id, JobType(job.job_type), job.recording_id, token,
                                  job.attempt_count, job.max_attempts,
                                  source_id=job.source_id)
    return None


# ---------------------------------------------------------------------------- fencing

@contextmanager
def fenced(db: Database, job_id: str, token: str) -> Iterator[Session]:
    """Transaction that only proceeds while ``token`` still owns the RUNNING job.

    The write lock is taken by the fence UPDATE itself (BEGIN IMMEDIATE), so the lease
    cannot be recovered by anyone else until this transaction ends.
    """
    with db.session() as s:
        res = s.execute(update(Job).where(
            Job.id == job_id, Job.status == JobStatus.RUNNING.value, Job.lease_token == token,
        ).values(updated_at=utcnow()))
        if res.rowcount != 1:
            raise LeaseLost(f"job {job_id}: lease lost")
        yield s


def renew_lease(db: Database, job_id: str, token: str, lease_seconds: int) -> bool:
    with db.session() as s:
        now = utcnow()
        res = s.execute(update(Job).where(
            Job.id == job_id, Job.status == JobStatus.RUNNING.value, Job.lease_token == token,
        ).values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now))
        return res.rowcount == 1


def set_progress(db: Database, job_id: str, token: str, percent: int) -> None:
    with fenced(db, job_id, token) as s:
        s.execute(update(Job).where(Job.id == job_id)
                  .values(progress_percent=max(0, min(100, int(percent)))))


def set_process(db: Database, job_id: str, token: str, pid: int | None,
                started_at: str | None) -> None:
    with fenced(db, job_id, token) as s:
        s.execute(update(Job).where(Job.id == job_id)
                  .values(process_pid=pid, process_started_at=started_at))


def mark_completed(s: Session, job_id: str) -> None:
    """Call inside a fenced transaction."""
    job = s.get(Job, job_id)
    check_transition(job.status, JobStatus.COMPLETED)
    now = utcnow()
    job.status = JobStatus.COMPLETED.value
    job.progress_percent = 100
    job.completed_at = now
    job.updated_at = now
    job.lease_expires_at = None
    job.lease_token = None
    job.process_pid = None
    job.process_started_at = None
    job.last_error = None


def complete(db: Database, claimed: ClaimedJob,
             apply: Callable[[Session], None] | None = None) -> None:
    """Fenced completion: apply side effects, mark COMPLETED, enqueue the next stage."""
    with fenced(db, claimed.id, claimed.token) as s:
        if claimed.decision is not None:
            # Final-output authorization (M2 §12.5): same BEGIN IMMEDIATE transaction as
            # the output, so a revocation either commits before (refused) or after.
            try:
                rights.fence(s, claimed.decision)
            except RightsRefused as e:
                raise RightsRefusedJobError(e.message) from e
        if apply:
            apply(s)
        mark_completed(s, claimed.id)
        nxt = NEXT_STAGE.get(claimed.job_type)
        if nxt is not None:
            job = s.get(Job, claimed.id)
            try:
                with s.begin_nested():
                    enqueue(s, claimed.recording_id, nxt, job.max_attempts)
            except RightsRefused as e:
                record_event(s, Severity.WARNING, "queue", "rights_refused",
                             f"{nxt.value} not queued for recording {claimed.recording_id}: "
                             f"{e.message}", recording_id=claimed.recording_id)


def _apply_terminal_failure(s: Session, job: Job, error: str) -> None:
    if job.recording_id is None:          # ACQUIRE before its recording exists
        return
    rec = s.get(Recording, job.recording_id)
    if rec is None:
        return
    if job.job_type == JobType.THUMBNAIL.value:
        rec.thumbnail_status = "FAILED"
    else:
        rec.status = RecordingStatus.FAILED.value
        rec.error_message = f"{job.job_type} failed: {error}"[:2000]


def fail(db: Database, claimed: ClaimedJob, error: str, *, permanent: bool,
         backoff_base: float, backoff_cap: float) -> JobStatus:
    """Fenced failure handling. Returns the resulting status."""
    with fenced(db, claimed.id, claimed.token) as s:
        job = s.get(Job, claimed.id)
        now = utcnow()
        exhausted = job.attempt_count >= job.max_attempts
        if permanent or exhausted:
            check_transition(job.status, JobStatus.FAILED)
            job.status = JobStatus.FAILED.value
            job.completed_at = now
            _apply_terminal_failure(s, job, error)
            sev = Severity.ERROR
            reason = "permanent error" if permanent else "max attempts reached"
            msg = f"{job.job_type} job failed ({reason}): {error}"
        else:
            check_transition(job.status, JobStatus.RETRY_WAIT)
            job.status = JobStatus.RETRY_WAIT.value
            delay = backoff_seconds(job.attempt_count, backoff_base, backoff_cap)
            job.next_attempt_at = now + timedelta(seconds=delay)
            sev = Severity.WARNING
            msg = (f"{job.job_type} attempt {job.attempt_count}/{job.max_attempts} failed; "
                   f"retrying in {delay:.0f}s: {error}")
        job.last_error = error[:4000]
        job.updated_at = now
        job.lease_expires_at = None
        job.lease_token = None
        job.process_pid = None
        job.process_started_at = None
        record_event(s, sev, "worker", "job_failed" if sev == Severity.ERROR else "job_retry",
                     msg, job_id=job.id, recording_id=job.recording_id)
        return JobStatus(job.status)


def defer(db: Database, claimed: ClaimedJob, reason: str, delay_seconds: float) -> None:
    """Storage went offline: give the job back WITHOUT consuming the attempt.

    RUNNING -> RETRY_WAIT with ``attempt_count`` restored, eligible again after
    ``delay_seconds`` (the claim loop also skips it while the location stays offline)."""
    with fenced(db, claimed.id, claimed.token) as s:
        job = s.get(Job, claimed.id)
        check_transition(job.status, JobStatus.RETRY_WAIT)
        now = utcnow()
        job.status = JobStatus.RETRY_WAIT.value
        job.attempt_count = max(0, job.attempt_count - 1)
        job.next_attempt_at = now + timedelta(seconds=delay_seconds)
        job.lease_expires_at = None
        job.lease_token = None
        job.process_pid = None
        job.process_started_at = None
        job.updated_at = now
        job.last_error = f"waiting for storage: {reason}"[:4000]
        record_event(s, Severity.WARNING, "worker", "job_waiting_for_storage",
                     f"{job.job_type} job waiting for storage (attempt not counted): {reason}",
                     job_id=job.id, recording_id=job.recording_id)


def release(db: Database, claimed: ClaimedJob, reason: str) -> None:
    """Give a job back (graceful shutdown): RUNNING -> RETRY_WAIT, immediately eligible."""
    with fenced(db, claimed.id, claimed.token) as s:
        job = s.get(Job, claimed.id)
        check_transition(job.status, JobStatus.RETRY_WAIT)
        now = utcnow()
        job.status = JobStatus.RETRY_WAIT.value
        job.next_attempt_at = now
        job.lease_expires_at = None
        job.lease_token = None
        job.process_pid = None
        job.process_started_at = None
        job.updated_at = now
        job.last_error = reason
        record_event(s, Severity.INFO, "worker", "job_released", reason, job_id=job.id,
                     recording_id=job.recording_id)


# ---------------------------------------------------------------------------- recovery

@dataclass
class Recovered:
    job_id: str
    new_status: JobStatus
    orphan: str  # "none" | "not_running" | "killed" | "pid_reused"


# How long a recoverer holds a job it has fenced while it terminates the old attempt's child.
# If the recoverer itself dies in that window, the job becomes recoverable again afterwards.
RECOVERY_FENCE_SECONDS = 30
RECOVERY_TOKEN_PREFIX = "recovery-"


def _recoverable(now: datetime, foreign_to_worker: str | None):
    cond = and_(Job.lease_expires_at.is_not(None), Job.lease_expires_at < now)
    if foreign_to_worker:
        cond = or_(cond, or_(Job.claimed_by.is_(None), Job.claimed_by != foreign_to_worker))
    return cond


def recover_stuck_jobs(db: Database, *, kill_orphan: Callable[[int, str | None], str],
                       backoff_base: float, backoff_cap: float,
                       foreign_to_worker: str | None = None,
                       now: datetime | None = None,
                       recoverer: str = "recovery",
                       on_candidates: Callable[[list[str]], None] | None = None,
                       ) -> list[Recovered]:
    """Recover RUNNING jobs whose lease expired (ARCHITECTURE §7.4–7.5).

    If ``foreign_to_worker`` is given (the caller holds the single-worker OS lock, so no
    other worker process can be alive), RUNNING jobs claimed by any *other* worker id are
    recovered immediately too, without waiting for lease expiry.

    Per job, in three steps — nothing destructive happens on information read earlier:

    1. **Fence** (one ``BEGIN IMMEDIATE`` transaction): re-evaluate eligibility (status
       RUNNING *and* lease still expired / foreign) and, only if still eligible, replace the
       lease token with a recovery token. A concurrent ``renew_lease`` by a live owner either
       happened before (→ the job is no longer eligible and is skipped untouched) or happens
       after (→ it fails the token check, the owner sees ``LeaseLost`` and stops its own child).
       The child's PID/start time are read in this same transaction.
    2. **Terminate** the old attempt's recorded child (PID-reuse safe). If it cannot be
       confirmed dead, the job stays fenced and is retried by a later recovery pass.
    3. **Transition** to RETRY_WAIT / FAILED by compare-and-set on the recovery token.

    ``on_candidates`` is a test hook called after the initial scan.
    """
    scan_now = now or utcnow()
    with db.session() as s:
        ids = list(s.scalars(select(Job.id).where(Job.status == JobStatus.RUNNING.value,
                                                  _recoverable(scan_now, foreign_to_worker))))
    if on_candidates:
        on_candidates(ids)

    out: list[Recovered] = []
    for job_id in ids:
        t = now or utcnow()
        fence_token = RECOVERY_TOKEN_PREFIX + secrets.token_hex(12)
        # 1. Atomically re-check eligibility and fence out the previous owner.
        with db.session() as s:
            res = s.execute(update(Job).where(
                Job.id == job_id, Job.status == JobStatus.RUNNING.value,
                _recoverable(t, foreign_to_worker),
            ).values(lease_token=fence_token, claimed_by=f"{recoverer}:recovering",
                     lease_expires_at=t + timedelta(seconds=RECOVERY_FENCE_SECONDS),
                     updated_at=utcnow()))
            if res.rowcount != 1:
                log.info("recovery skipped: lease renewed or job finished meanwhile",
                         extra={"job_id": job_id})
                continue
            pid, started = s.execute(select(Job.process_pid, Job.process_started_at)
                                     .where(Job.id == job_id)).one()

        # 2. The old owner can no longer write or renew; terminate its recorded child.
        orphan = "none"
        if pid:
            try:
                orphan = kill_orphan(pid, started)
            except Exception:
                log.exception("orphan termination failed; job stays fenced",
                              extra={"job_id": job_id, "data": {"pid": pid}})
                continue
            if orphan == "kill_failed":
                log.warning("orphaned media process did not exit; job stays fenced",
                            extra={"job_id": job_id, "data": {"pid": pid}})
                continue
            if orphan == "killed":
                log.warning("killed orphaned media process", extra={"job_id": job_id,
                                                                    "data": {"pid": pid}})

        # 3. Make the job claimable again (CAS on our own fence token).
        with db.session() as s:
            job = s.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING.value \
                    or job.lease_token != fence_token:
                continue
            t = utcnow()
            exhausted = job.attempt_count >= job.max_attempts
            if exhausted:
                job.status = JobStatus.FAILED.value
                job.completed_at = t
                job.last_error = "worker stopped responding; max attempts reached"
                _apply_terminal_failure(s, job, job.last_error)
            else:
                job.status = JobStatus.RETRY_WAIT.value
                job.next_attempt_at = t + timedelta(
                    seconds=backoff_seconds(job.attempt_count, backoff_base, backoff_cap))
                job.last_error = "worker stopped responding; lease recovered"
            job.lease_token = None
            job.lease_expires_at = None
            job.process_pid = None
            job.process_started_at = None
            job.updated_at = t
            record_event(s, Severity.WARNING, "queue", "lease_recovered",
                         f"Recovered interrupted {job.job_type} job → {job.status} "
                         f"(orphan process: {orphan})",
                         job_id=job.id, recording_id=job.recording_id)
            out.append(Recovered(job.id, JobStatus(job.status), orphan))
    return out


def active_lease_tokens(db: Database) -> set[str]:
    """Tokens of attempts that currently own a RUNNING job (used by temp cleanup)."""
    with db.session() as s:
        return set(s.scalars(select(Job.lease_token).where(
            Job.status == JobStatus.RUNNING.value, Job.lease_token.is_not(None))))


# ---------------------------------------------------------------------------- manual retry

def retry_job(s: Session, job_id: str) -> Job:
    """FAILED -> PENDING with a fresh attempt budget (owner-initiated)."""
    job = s.get(Job, job_id)
    if job is None:
        raise NotFoundError("job not found")
    if job.status != JobStatus.FAILED.value:
        raise ConflictError(f"only FAILED jobs can be retried (job is {job.status})",
                            code="not_retryable")
    check_transition(job.status, JobStatus.PENDING)
    now = utcnow()
    job.status = JobStatus.PENDING.value
    job.attempt_count = 0
    job.progress_percent = 0
    job.next_attempt_at = now
    job.completed_at = None
    job.started_at = None
    job.updated_at = now
    rec = s.get(Recording, job.recording_id) if job.recording_id else None
    if rec is not None:
        if job.job_type == JobType.THUMBNAIL.value:
            rec.thumbnail_status = "PENDING"
        elif rec.status == RecordingStatus.FAILED.value:
            rec.status = (RecordingStatus.STAGED.value if rec.staging_filename
                          else RecordingStatus.PROCESSING.value)
            rec.error_message = None
    record_event(s, Severity.INFO, "api", "job_retry_requested",
                 f"Manual retry of {job.job_type} job", job_id=job.id,
                 recording_id=job.recording_id)
    return job
