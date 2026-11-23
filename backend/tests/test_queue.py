from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.db.base import utcnow
from app.db.session import Database
from app.models import Job, JobStatus, JobType, Recording
from app.services import queue

# ------------------------------------------------------------------ helpers


def make_recording(db, n=0, **kw):
    with db.session() as s:
        r = Recording(original_filename=f"r{n}.mp4", stored_filename=f"r{n}.mp4",
                      file_path=f"recordings/r{n}.mp4", sha256=f"{n:064d}",
                      file_size_bytes=1, **kw)
        s.add(r)
        s.flush()
        return r.id


def make_job(db, n=0, job_type=JobType.INGEST, max_attempts=3):
    rid = make_recording(db, n)
    with db.session() as s:
        return queue.enqueue(s, rid, job_type, max_attempts).id


def job(db, jid) -> Job:
    with db.session() as s:
        return s.get(Job, jid)


def claim(db, worker="w1", lease=30, now=None):
    return queue.claim_next(db, worker, lease, now=now)


def fail(db, c, permanent=False):
    return queue.fail(db, c, "boom", permanent=permanent, backoff_base=0, backoff_cap=0)


# ------------------------------------------------------------------ state machine

@pytest.mark.parametrize("src,dst,ok", [
    ("PENDING", JobStatus.RUNNING, True), ("PENDING", JobStatus.COMPLETED, False),
    ("RUNNING", JobStatus.COMPLETED, True), ("RUNNING", JobStatus.PENDING, False),
    ("RETRY_WAIT", JobStatus.RUNNING, True), ("FAILED", JobStatus.PENDING, True),
    ("FAILED", JobStatus.RUNNING, False), ("COMPLETED", JobStatus.RUNNING, False),
    ("CANCELLED", JobStatus.PENDING, False),
])
def test_transitions(src, dst, ok):
    if ok:
        queue.check_transition(src, dst)
    else:
        with pytest.raises(queue.InvalidTransition):
            queue.check_transition(src, dst)


def test_backoff_is_exponential_and_capped():
    assert [queue.backoff_seconds(a, 5, 60) for a in (1, 2, 3, 4, 5)] == [5, 10, 20, 40, 60]
    assert queue.backoff_seconds(3, 0, 60) == 0


def test_enqueue_is_idempotent(db):
    rid = make_recording(db)
    with db.session() as s:
        a = queue.enqueue(s, rid, JobType.INGEST, 3)
        b = queue.enqueue(s, rid, JobType.INGEST, 3)
        assert a.id == b.id
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 1


# ------------------------------------------------------------------ claiming

def test_claim_sets_lease_and_token(db):
    jid = make_job(db)
    c = claim(db)
    j = job(db, jid)
    assert c.id == jid and j.status == "RUNNING" and j.lease_token == c.token
    assert j.attempt_count == 1 and j.claimed_by == "w1" and j.lease_expires_at > utcnow()
    assert claim(db) is None  # nothing else claimable


def test_concurrent_claiming_never_double_claims(settings, db):
    n_jobs = 40
    ids = [make_job(db, i) for i in range(n_jobs)]
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(k):
        own = Database(settings.database_url)  # separate engine/connection per worker
        barrier.wait()
        while True:
            c = queue.claim_next(own, f"w{k}", 30)
            if c is None:
                break
            with lock:
                claimed.append(c.id)
        own.dispose()

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(claimed) == sorted(ids)  # every job exactly once
    assert len(set(claimed)) == n_jobs


def test_retry_wait_not_claimed_before_due(db):
    make_job(db)
    c = claim(db)
    queue.fail(db, c, "x", permanent=False, backoff_base=60, backoff_cap=600)
    assert claim(db) is None
    assert claim(db, now=utcnow() + timedelta(seconds=61)) is not None


# ------------------------------------------------------------------ retries

def test_retry_limit_then_failed(db):
    jid = make_job(db, max_attempts=3)
    statuses = []
    for _ in range(3):
        c = claim(db)
        statuses.append(fail(db, c))
    assert statuses == [JobStatus.RETRY_WAIT, JobStatus.RETRY_WAIT, JobStatus.FAILED]
    j = job(db, jid)
    assert j.attempt_count == 3 and j.last_error == "boom"
    assert claim(db) is None
    with db.session() as s:
        rec = s.get(Recording, j.recording_id)
        assert rec.status == "FAILED"


def test_permanent_error_not_retried(db):
    jid = make_job(db, max_attempts=5)
    assert fail(db, claim(db), permanent=True) == JobStatus.FAILED
    assert job(db, jid).attempt_count == 1


def test_thumbnail_failure_does_not_fail_recording(db):
    rid = make_recording(db, 7, status="READY")
    with db.session() as s:
        queue.enqueue(s, rid, JobType.THUMBNAIL, 1)
    fail(db, claim(db), permanent=True)
    with db.session() as s:
        r = s.get(Recording, rid)
        assert r.status == "READY" and r.thumbnail_status == "FAILED"


def test_manual_retry(db):
    jid = make_job(db, max_attempts=1)
    fail(db, claim(db))
    with db.session() as s:
        queue.retry_job(s, jid)
    j = job(db, jid)
    assert j.status == "PENDING" and j.attempt_count == 0
    with db.session() as s:
        assert s.get(Recording, j.recording_id).status != "FAILED"
    assert claim(db) is not None


def test_manual_retry_rejects_non_failed(db):
    jid = make_job(db)
    with pytest.raises(ConflictError), db.session() as s:
        queue.retry_job(s, jid)


# ------------------------------------------------------------------ fencing / leases

def test_zombie_attempt_is_fenced_out(db):
    jid = make_job(db)
    old = claim(db, "w-old", lease=5)
    future = utcnow() + timedelta(seconds=10)
    rec = queue.recover_stuck_jobs(db, kill_orphan=lambda p, s: "not_running",
                                   backoff_base=0, backoff_cap=0, now=future)
    assert [r.job_id for r in rec] == [jid]
    new = claim(db, "w-new")
    assert new.token != old.token and new.attempt == 2
    with pytest.raises(queue.LeaseLost):
        queue.complete(db, old)
    with pytest.raises(queue.LeaseLost):
        queue.set_progress(db, jid, old.token, 50)
    with pytest.raises(queue.LeaseLost):
        fail(db, old)
    assert not queue.renew_lease(db, jid, old.token, 30)
    queue.complete(db, new)
    assert job(db, jid).status == "COMPLETED"


def test_completion_enqueues_next_stage(db):
    jid = make_job(db)
    c = claim(db)
    queue.complete(db, c)
    with db.session() as s:
        types = sorted(s.scalars(select(Job.job_type)
                                 .where(Job.recording_id == job(db, jid).recording_id)))
    assert types == ["INGEST", "METADATA"]


def test_renewal_extends_lease(db):
    jid = make_job(db)
    c = claim(db, lease=2)
    before = job(db, jid).lease_expires_at
    assert queue.renew_lease(db, jid, c.token, 60)
    assert job(db, jid).lease_expires_at > before


def test_live_lease_not_recovered(db):
    make_job(db)
    claim(db, lease=60)
    assert queue.recover_stuck_jobs(db, kill_orphan=lambda p, s: "none",
                                    backoff_base=0, backoff_cap=0) == []


def test_expired_lease_recovery_kills_orphan_first(db):
    jid = make_job(db)
    c = claim(db, lease=1)
    queue.set_process(db, jid, c.token, 999999, "Mon Nov 23 10:00:00 2026")
    calls = []

    def kill(pid, started):
        calls.append((pid, started, job(db, jid).status))  # status still RUNNING while killing
        return "killed"

    rec = queue.recover_stuck_jobs(db, kill_orphan=kill, backoff_base=0, backoff_cap=0,
                                   now=utcnow() + timedelta(seconds=5))
    assert calls == [(999999, "Mon Nov 23 10:00:00 2026", "RUNNING")]
    assert rec[0].orphan == "killed" and rec[0].new_status == JobStatus.RETRY_WAIT
    j = job(db, jid)
    assert j.process_pid is None and j.lease_token is None


def test_recovery_of_last_attempt_fails_job(db):
    jid = make_job(db, max_attempts=1)
    claim(db, lease=1)
    queue.recover_stuck_jobs(db, kill_orphan=lambda p, s: "none", backoff_base=0,
                             backoff_cap=0, now=utcnow() + timedelta(seconds=5))
    assert job(db, jid).status == "FAILED"


def test_foreign_running_jobs_recovered_immediately_when_lock_held(db):
    jid = make_job(db)
    claim(db, "dead-worker", lease=3600)
    rec = queue.recover_stuck_jobs(db, kill_orphan=lambda p, s: "none", backoff_base=0,
                                   backoff_cap=0, foreign_to_worker="me")
    assert [r.job_id for r in rec] == [jid]


def test_release_returns_job_immediately(db):
    jid = make_job(db)
    c = claim(db)
    queue.release(db, c, "shutdown")
    assert job(db, jid).status == "RETRY_WAIT"
    assert claim(db).id == jid
