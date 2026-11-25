"""M5: persistent AI highlight jobs over stored M4 evidence, with a scripted local provider.

The provider stand-in answers from the evidence text it is actually sent (window and word
ids), so every answer goes through the real prompt rendering, strict validation, repair
loop, persistence, ranking and job lifecycle. No model, no network."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai import registry
from app.ai.providers.base import Identity, ProviderError, Request, Response
from app.main import create_app
from app.models import AIRun, HighlightCandidate, HighlightRequest, Job, TranscriptWord
from app.services import acquisition, ai_highlights, queue, transcripts
from app.workers.worker import WorkerProcess
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.m4_fixtures import plan_words, tone_word_audio
from tests.test_ingest import drain
from tests.test_rights import World
from tests.test_transcripts import engine  # noqa: F401  (fixture)

_WINDOW = re.compile(r"^WINDOW (c:\d+)", re.MULTILINE)
_WORD = re.compile(r"(w:[0-9a-f-]+:\d+)=")


class Scripted:
    """Answers each request from the evidence in it. ``scores`` gives each window's
    component score (all components equal); ``mode`` selects misbehaviour."""

    def __init__(self, scores=(90, 75, 40, 85), mode="good", fail_first=0):
        self.scores, self.mode, self.fail_first = list(scores), mode, fail_first
        self.calls: list[Request] = []

    def identity(self) -> Identity:
        return Identity("scripted", "scripted-local", "sha256:test", {"q": "none"})

    def health(self) -> dict:
        return {"ok": True}

    def unload(self) -> None:
        pass

    def generate(self, request: Request) -> Response:
        self.calls.append(request)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ProviderError("connection refused", transient=True,
                                code="provider_unreachable")
        if self.mode == "invented":
            return Response(text=json.dumps({"schema": "highlight_eval.v1", "candidates": [
                self._cand("c:1", "w:fake:1", "w:fake:2", 99)], "rejected_window_ids": []}))
        if self.mode == "prose":
            return Response(text="Sure! The best moment is at 0:12, trust me.")
        blocks = re.split(r"(?=^WINDOW c:)", request.user, flags=re.MULTILINE)
        cands, rejected = [], []
        for block in blocks:
            m = _WINDOW.match(block)
            if not m:
                continue
            ids = _WORD.findall(block)
            if len(ids) < 24:
                rejected.append(m.group(1))
                continue
            n = int(m.group(1).split(":")[1])
            cands.append(self._cand(m.group(1), ids[0], ids[22],
                                    self.scores[(n - 1) % len(self.scores)]))
        return Response(text=json.dumps({"schema": "highlight_eval.v1", "candidates": cands,
                                         "rejected_window_ids": rejected}))

    @staticmethod
    def _cand(window, start, end, score):
        return {"window_id": window, "start_id": start, "end_id": end, "category": "reaction",
                "what_happens": "The streamer keeps talking quickly.",
                "why_entertaining": "Sustained commentary.", "standalone": True,
                "clear_start_and_end": True, "title": f"Moment {window}",
                "hook": "Wait for it", "scores": {k: score for k in (
                    "hook", "entertainment", "surprise", "intensity", "standalone",
                    "pacing", "payoff")}, "confidence": 0.6,
                "evidence": [{"ids": [start, end], "claim": "speech in this window",
                              "basis": "transcript"}], "uncertainty": ""}


@pytest.fixture
def recording(db, settings, tmp_path, engine):  # noqa: F811
    words = plan_words(90, start_s=0.5)                       # speech from 0.5 s to ~38 s
    wav = tone_word_audio(tmp_path / "speech.wav", words, 40.0)
    mp4 = tmp_path / "speech.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x240:rate=30:duration=40", "-i", str(wav), "-c:v",
                    "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(mp4)], check=True, timeout=120)
    rec = import_file(db, settings, mp4).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)
    settings.ai_windows_per_call = 1
    return rec


@pytest.fixture
def provider(monkeypatch):
    holder = {"p": Scripted()}
    monkeypatch.setattr(registry, "build", lambda settings: holder["p"])
    monkeypatch.setattr(registry, "available_memory_mb", lambda: 16_000)
    return holder


def _ask(db, settings, rec, n=2, **kw):
    with db.session() as s:
        req = ai_highlights.request(s, settings, rec.id, game="gta", channel="rumbleframe",
                                    target_count=n, **kw)
        return req.id


def _state(db, rec):
    with db.session() as s:
        return ai_highlights.describe(s, rec.id)


# -------------------------------------------------------------------------- full flow
@requires_ffmpeg
def test_request_to_ranked_grounded_highlights(db, settings, recording, provider):
    _ask(db, settings, recording, n=2)
    drain(db, settings, max_jobs=40)
    st = _state(db, recording)
    assert st["request"]["status"] == "COMPLETE", st
    hl = st["highlights"]
    assert 1 <= len(hl) <= 2 and [h["rank"] for h in hl] == list(range(1, len(hl) + 1))
    assert [h["editorial_score"] for h in hl] == sorted(
        (h["editorial_score"] for h in hl), reverse=True)
    assert all(h["editorial_score"] >= 60 for h in hl)
    with db.session() as s:
        for h in hl:                       # times are the stored word times, not the model's
            c = s.get(HighlightCandidate, h["id"])
            tid, start_idx = c.start_id.rsplit(":", 1)[0][2:], int(c.start_id.rsplit(":", 1)[1])
            word_start = s.get(TranscriptWord, (tid, start_idx)).start_ms
            assert h["model_clip"][0] == word_start               # the model's choice
            assert word_start - 300 <= c.start_ms <= word_start   # refined: pre-roll only
            assert c.transcript_id == tid and len(c.evidence_fingerprint) == 64
        runs = s.scalars(select(AIRun)).all()
        assert runs and all(r.cost_usd == 0 and r.status == "accepted" for r in runs)
        rec = json.loads(runs[0].record_json)
        assert rec["prompt"]["id"] == "gaming.gta.v2" and rec["output_contract"]["version"]
        assert runs[0].model == "scripted-local" and runs[0].model_digest == "sha256:test"
    reasons = {r["reason"] for r in st["rejected"]}
    assert reasons <= {"editorial score 40 below 60", "duplicate of a higher-scored clip",
                       "beyond the requested 2 highlights"}
    assert "not a probability" in st["request"]["summary"]["editorial_score_meaning"]


@requires_ffmpeg
def test_fewer_than_requested_is_returned_without_filler(db, settings, recording, provider):
    provider["p"] = Scripted(scores=(45, 50, 30))
    _ask(db, settings, recording, n=5)
    drain(db, settings, max_jobs=40)
    st = _state(db, recording)
    assert st["request"]["status"] == "COMPLETE" and st["highlights"] == []
    assert st["request"]["summary"]["fewer_than_requested"] is True
    assert st["rejected"] and all("below 60" in r["reason"] for r in st["rejected"])


@pytest.mark.parametrize("mode", ["invented", "prose"])
@requires_ffmpeg
def test_invalid_answers_are_rejected_and_kept_for_audit(db, settings, recording, provider,
                                                         mode):
    provider["p"] = Scripted(mode=mode)
    _ask(db, settings, recording)
    drain(db, settings, max_jobs=40)
    st = _state(db, recording)
    assert st["request"]["status"] == "FAILED" and st["highlights"] == []
    with db.session() as s:
        runs = s.scalars(select(AIRun)).all()
        assert runs and all(r.status == "failed_validation" for r in runs)
        assert all(r.attempts == settings.ai_max_repairs + 1 for r in runs)
        assert runs[0].raw_last                     # the raw answer is stored
        assert s.scalar(select(func.count()).select_from(HighlightCandidate)) == 0


@requires_ffmpeg
def test_provider_outage_retries_and_resumes_at_the_unfinished_batch(db, settings,
                                                                    recording, provider):
    provider["p"] = Scripted(fail_first=1)
    _ask(db, settings, recording)
    drain(db, settings, max_jobs=40)                    # backoff 0: retried in the drain
    st = _state(db, recording)
    assert st["request"]["status"] == "COMPLETE"
    with db.session() as s:
        runs = s.scalars(select(AIRun).order_by(AIRun.created_at)).all()
        assert runs[0].status == "provider_error"
        assert "provider_unreachable" in runs[0].error
        accepted = [r for r in runs if r.status == "accepted"]
        assert len({r.batch for r in accepted}) == len(accepted)     # each batch once
    batches = len({r.batch for r in runs})
    assert len(provider["p"].calls) == batches + 1                   # one failed call only


@requires_ffmpeg
def test_memory_pressure_defers_without_spending_an_attempt(db, settings, recording,
                                                            provider, monkeypatch):
    with db.session() as s:
        transcripts.request(s, settings, recording.id)
    drain(db, settings, max_jobs=40)
    monkeypatch.setattr(registry, "available_memory_mb", lambda: 500)
    _ask(db, settings, recording)
    worker = WorkerProcess(settings, db)
    for _ in range(10):
        claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
        if claimed is None:
            break
        worker._execute(claimed)
    with db.session() as s:
        job = s.scalar(select(Job).where(Job.job_type == "AI_HIGHLIGHTS"))
        assert job.status == "RETRY_WAIT" and job.attempt_count == 0
        assert "memory_pressure" in job.last_error
    assert provider["p"].calls == []


@requires_ffmpeg
def test_a_new_request_supersedes_the_unfinished_one(db, settings, recording, provider):
    first = _ask(db, settings, recording, n=3)
    second = _ask(db, settings, recording, n=1)
    drain(db, settings, max_jobs=40)
    with db.session() as s:
        assert s.get(HighlightRequest, first).status == "SUPERSEDED"
        assert s.get(HighlightRequest, second).status == "COMPLETE"
        assert s.scalar(select(func.count()).select_from(Job).where(
            Job.job_type == "AI_HIGHLIGHTS")) == 1
    assert len(_state(db, recording)["highlights"]) <= 1
    third = _ask(db, settings, recording, n=2)          # after completion: job re-armed
    drain(db, settings, max_jobs=40)
    with db.session() as s:
        assert s.get(HighlightRequest, third).status == "COMPLETE"


@requires_ffmpeg
def test_runs_and_candidates_are_immutable_but_review_status_is_not(db, settings, recording,
                                                                   provider):
    _ask(db, settings, recording)
    drain(db, settings, max_jobs=40)
    hl = _state(db, recording)["highlights"]
    conn = sqlite3.connect(settings.db_path)
    for sql in ("UPDATE ai_runs SET raw_last = 'x'", "DELETE FROM ai_runs",
                "UPDATE highlight_candidates SET title = 'x'",
                "UPDATE highlight_candidates SET start_ms = 0",
                "DELETE FROM highlight_candidates",
                "UPDATE highlight_candidates SET id = 'z'",
                "UPDATE highlight_candidates SET created_at = '2000-01-01 00:00:00'",
                "INSERT INTO ai_runs (id) VALUES ('x')"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    conn.close()
    with TestClient(create_app(settings)) as client:
        r = client.patch(f"/api/v1/recordings/{recording.id}/highlights/{hl[0]['id']}",
                         json={"review_status": "approved"})
        assert r.status_code == 200 and r.json()["review_status"] == "approved"


@requires_ffmpeg
def test_api_and_rights(db, settings, recording, provider, media_dir):
    with TestClient(create_app(settings)) as client:
        url = f"/api/v1/recordings/{recording.id}/highlights"
        r = client.post(url, json={"game": "gta", "channel": "rumbleframe", "target_count": 3})
        assert r.status_code == 200, r.text
        drain(db, settings, max_jobs=40)
        body = client.get(url).json()
        assert body["request"]["status"] == "COMPLETE" and body["request"]["target_count"] == 3
        assert client.post(url, json={"target_count": 0}).status_code == 422
        assert client.post(url, json={"target_count": 3, "model": "gpt"}).status_code == 422
        other = import_file(db, settings, media_dir / "basic.mp4").recording
        drain(db, settings)
        assert client.post(f"/api/v1/recordings/{other.id}/highlights",
                           json={"target_count": 3}).status_code == 403
        settings.ai_provider = "none"
        r = client.post(url, json={"target_count": 3})
        assert r.status_code == 409 and r.json()["error"]["code"] == "ai_disabled"


# ------------------------------------------------------------------- cancellation
def test_cancellable_abandons_a_blocking_call_and_unloads():
    unloaded = threading.Event()

    class Slow:
        def generate(self, request):
            time.sleep(5)
            return Response(text="{}")

        def unload(self):
            unloaded.set()

    stop = {"v": False}
    threading.Timer(0.3, lambda: stop.update(v=True)).start()
    wrapped = registry.Cancellable(Slow(), lambda: stop["v"], poll_s=0.05, join_s=0.1)
    t0 = time.monotonic()
    with pytest.raises(ProviderError) as e:
        wrapped.generate(Request(system="s", user="u"))
    assert e.value.code == "cancelled" and time.monotonic() - t0 < 2
    assert unloaded.is_set()
    # The abandoned HTTP call is still running: heavy work waits until it ends.
    assert "still finishing" in str(e.value) and registry.calls_in_flight() == 1
    deadline = time.monotonic() + 10
    while registry.calls_in_flight() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert registry.calls_in_flight() == 0


def test_ai_migration_round_trips_and_refuses_with_runs(db, settings):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    db.dispose()
    command.downgrade(cfg, "0011")
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO highlight_requests (id, recording_id, prompt_id, target_count, "
                 "status, settings_json, created_at) VALUES ('q', 'r', 'p', 1, 'COMPLETE', "
                 "'{}', '2026-11-25 00:00:00')")
    conn.execute("INSERT INTO ai_runs (id, request_id, batch, status, provider, model, "
                 "identity_json, prompt_id, prompt_version, prompt_sha256, contract_version, "
                 "contract_sha256, scoring_json, evidence_fingerprint, evidence_json, "
                 "record_json, raw_last, attempts, wall_s, cost_usd, created_at) VALUES "
                 "('a', 'q', 0, 'accepted', 'p', 'm', '{}', 'p', 1, 'x', 2, 'x', '{}', 'x', "
                 "'{}', '{}', '', 1, 0.1, 0, '2026-11-25 00:00:00')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):          # paid usage cannot be recorded
        conn.execute("INSERT INTO ai_runs (id, request_id, batch, status, provider, model, "
                     "identity_json, prompt_id, prompt_version, prompt_sha256, "
                     "contract_version, contract_sha256, scoring_json, "
                     "evidence_fingerprint, evidence_json, record_json, raw_last, attempts, "
                     "wall_s, cost_usd, created_at) VALUES ('b', 'q', 1, 'accepted', 'p', "
                     "'m', '{}', 'p', 1, 'x', 2, 'x', '{}', 'x', '{}', '{}', '', 1, 0.1, "
                     "0.02, '2026-11-25 00:00:00')")
    conn.close()
    with pytest.raises(Exception, match="AI runs exist"):
        command.downgrade(cfg, "0011")


# ------------------------------------------- independent review of PR #21 (2026-11-25)
def _run_with_timeout(fn, seconds=60):
    """Runs ``fn`` in a thread; a nested-session deadlock shows up as a timeout."""
    box = {}
    t = threading.Thread(target=lambda: box.update(v=fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "the job hung (nested session?)"
    return box.get("v")


@requires_ffmpeg
def test_review_superseded_with_nothing_newer_completes_without_hanging(db, settings,
                                                                        recording, provider):
    req = _ask(db, settings, recording)
    with db.session() as s:
        s.get(HighlightRequest, req).status = "SUPERSEDED"
    _run_with_timeout(lambda: drain(db, settings, max_jobs=40))
    with db.session() as s:
        job = s.scalar(select(Job).where(Job.job_type == "AI_HIGHLIGHTS"))
        assert job.status == "COMPLETED"
        assert s.scalar(select(func.count()).select_from(HighlightCandidate)) == 0
    assert provider["p"].calls == []


@requires_ffmpeg
def test_review_supersede_during_the_last_batch_never_finalizes_the_old_request(
        db, settings, recording, provider):
    settings.ai_windows_per_call = 10                       # one batch: it is the last
    holder = {}
    inner = Scripted()

    class Superseding(Scripted):
        def generate(self, request):
            if "second" not in holder:                      # a newer request arrives now
                holder["second"] = _ask(db, settings, recording, n=1)
            return inner.generate(request)

    provider["p"] = Superseding()
    first = _ask(db, settings, recording, n=3)
    _run_with_timeout(lambda: drain(db, settings, max_jobs=40))
    with db.session() as s:
        old = s.get(HighlightRequest, first)
        assert old.status == "SUPERSEDED" and old.summary_json is None
        assert s.scalar(select(func.count()).select_from(HighlightCandidate).where(
            HighlightCandidate.request_id == first)) == 0
        assert s.get(HighlightRequest, holder["second"]).status == "COMPLETE"
        job = s.scalar(select(Job).where(Job.job_type == "AI_HIGHLIGHTS"))
        assert job.status == "COMPLETED" and job.attempt_count <= 2


@requires_ffmpeg
def test_review_a_failed_job_fails_its_request(db, settings, recording, provider):
    class Refusing(Scripted):
        def generate(self, request):
            self.calls.append(request)
            raise ProviderError("model not pulled", transient=False, code="provider_error")

    provider["p"] = Refusing()
    req = _ask(db, settings, recording)
    drain(db, settings, max_jobs=40)
    with db.session() as s:
        r = s.get(HighlightRequest, req)
        assert r.status == "FAILED" and "model not pulled" in r.error
        job = s.scalar(select(Job).where(Job.job_type == "AI_HIGHLIGHTS"))
        assert job.status == "FAILED" and job.attempt_count == 1       # not retried
    assert len(provider["p"].calls) == 1


@requires_ffmpeg
def test_review_bad_prompt_voice_or_scoring_is_a_422(db, settings, recording, provider,
                                                    monkeypatch):
    from app.ai import scoring

    with TestClient(create_app(settings)) as client:
        url = f"/api/v1/recordings/{recording.id}/highlights"
        r = client.post(url, json={"target_count": 2, "channel": "no-such-channel"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "ai_configuration_invalid"

        def no_profile(**kw):
            raise scoring.ScoringError("no scoring profile matches")

        monkeypatch.setattr(scoring, "resolve", no_profile)
        r = client.post(url, json={"target_count": 2, "channel": "rumbleframe"})
        assert r.status_code == 422 and "no scoring profile" in r.text
    with db.session() as s, pytest.raises(Exception) as e:
        ai_highlights.request(s, settings, recording.id, game="gta", channel="rumbleframe",
                              target_count=2, prompt_id="no.such.prompt")
    assert getattr(e.value, "status_code", None) == 422


@requires_ffmpeg
def test_review_retries_use_the_windows_planned_on_the_first_run(db, settings, recording,
                                                                provider):
    provider["p"] = Scripted(fail_first=1)
    req = _ask(db, settings, recording)
    worker = WorkerProcess(settings, db)
    for _ in range(20):                                     # until the provider error
        claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
        assert claimed is not None
        worker._execute(claimed)
        with db.session() as s:
            if s.scalar(select(AIRun.id).where(AIRun.request_id == req)):
                break
    with db.session() as s:
        planned = json.loads(s.get(HighlightRequest, req).plan_json)
    assert len(planned) >= 2
    settings.ai_max_windows = 1                             # a configuration change
    drain(db, settings, max_jobs=40)
    with db.session() as s:
        assert json.loads(s.get(HighlightRequest, req).plan_json) == planned
        batches = {r.batch for r in s.scalars(select(AIRun).where(AIRun.request_id == req))}
    assert batches == set(range(len(planned)))              # every pinned window evaluated


def test_review_memory_figure_leaves_out_purgeable_pages(monkeypatch):
    out = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
           "Pages free:                               1000.\n"
           "Pages inactive:                           2000.\n"
           "Pages speculative:                         500.\n"
           "Pages purgeable:                         99999.\n")

    class Done:
        stdout = out

    monkeypatch.setattr(registry.sys, "platform", "darwin")
    monkeypatch.setattr(registry.subprocess, "run", lambda *a, **k: Done())
    assert registry.available_memory_mb() == 3500 * 16384 // (1024 * 1024)


@requires_ffmpeg
def test_review_heavy_jobs_wait_for_an_abandoned_ai_call(db, settings, recording, provider,
                                                         monkeypatch):
    monkeypatch.setattr(registry, "calls_in_flight", lambda: 1)
    _ask(db, settings, recording)
    worker = WorkerProcess(settings, db)
    claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
    while claimed is not None and claimed.job_type not in ("AI_HIGHLIGHTS", "TRANSCRIBE"):
        worker._execute(claimed)
        claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
    assert claimed is not None
    worker._execute(claimed)
    with db.session() as s:
        job = s.get(Job, claimed.id)
        assert job.status == "RETRY_WAIT" and job.attempt_count == 0
        assert "still finishing" in job.last_error
    assert provider["p"].calls == []


def test_review_no_cancelled_run_status(db, settings):
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO ai_runs (id, request_id, batch, status, provider, model, "
                     "identity_json, prompt_id, prompt_version, prompt_sha256, "
                     "contract_version, contract_sha256, scoring_json, evidence_fingerprint, "
                     "evidence_json, record_json, raw_last, attempts, wall_s, cost_usd, "
                     "created_at) VALUES ('a', 'q', 0, 'cancelled', 'p', 'm', '{}', 'p', 1, "
                     "'x', 2, 'x', '{}', 'x', '{}', '{}', '', 0, 0, 0, '2026-11-25')")
    conn.close()
