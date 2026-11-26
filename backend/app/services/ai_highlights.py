"""M5: persistent AI highlight evaluation over stored M4 evidence.

``request()`` records "find N highlights in this recording for this game and channel" and
queues the ``AI_HIGHLIGHTS`` job. The job (``workers/handlers.py``) then:

1. waits, without spending an attempt, for the shared transcript and both signal runs;
2. plans candidate **windows** deterministically from stored analysis (:func:`windows`);
3. evaluates them with the local model in small batches (:func:`evaluate_batch`): each
   batch's evidence (``evidence.v1``) is built from the analysis-evidence layer, sent to
   the provider through the versioned prompt, validated strictly, repaired at most
   ``ai_max_repairs`` times, and stored as an immutable ``ai_runs`` row in its own fenced
   transaction (a retried job skips finished batches);
4. ranks every accepted candidate across batches with the versioned scoring profile,
   suppresses duplicates, keeps at most N and **returns fewer when fewer qualify**
   (:func:`finalize`). Every candidate is stored, selected or rejected with its reason.

Times are looked up from evidence ids; the model cannot state a timestamp. Words the
reliability layer marks as unable to support a claim are not shown to the model, so it
cannot cite them. Cost is always 0 (a database check refuses anything else).
"""

from __future__ import annotations

import json
from dataclasses import asdict

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai import analyze, scoring
from app.ai import prompts as prompt_registry
from app.ai.providers.base import Provider
from app.ai.schemas import (
    OUTPUT_CONTRACT_VERSION,
    Event,
    EvidenceBundle,
    Keyframe,
    Window,
    Word,
    output_contract_sha256,
)
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.db.base import utcnow
from app.models import (
    AIRun,
    HighlightCandidate,
    HighlightRequest,
    Job,
    JobStatus,
    JobType,
    Recording,
    RecordingStatus,
    RequestStatus,
    RunStatus,
    SignalRun,
    TranscriptSegment,
)
from app.services import (
    analysis_evidence,
    evidence_reliability,
    highlight_candidates,
    rights,
    signals,
    transcripts,
)
from app.services.clips import SHORT_MAX_MS
from app.services.highlight_candidates import PlannedWindow

MIN_CLIP_MS, MAX_CLIP_MS = 8_000, 60_000
# The target format: a 35/65 Short (clips.SHORT_MAX_MS). Windows, refinement and the final
# check all respect it, so every selected highlight can be rendered as planned.
TARGET_MAX_MS = SHORT_MAX_MS
MAX_WORDS_PER_WINDOW = 160
MAX_EVENTS_PER_WINDOW = 24


# --------------------------------------------------------------------------- requests
def request(session: Session, settings: Settings, recording_id: str, *, game: str | None,
            channel: str | None, target_count: int,
            prompt_id: str | None = None) -> HighlightRequest:
    """Record a highlight request and queue (or re-arm) the AI job. A newer request
    supersedes an unfinished older one of the same recording."""
    from app.services import queue

    recording = session.get(Recording, recording_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise ConflictError("Wait for the recording to finish processing.")
    if not 1 <= target_count <= 20:
        raise ValidationFailed("Ask for between 1 and 20 highlights.")
    if settings.ai_provider == "none":
        raise ConflictError("Local AI is turned off.", code="ai_disabled")
    try:                       # a bad prompt, voice or scoring choice is the caller's
        prompt = prompt_registry.load(prompt_id or settings.ai_default_prompt)
        ranking = scoring.resolve(game=game or prompt.meta.get("game"), channel=channel,
                                  prompt=prompt.id)
        prompt_registry.channel_voice(channel)
    except (prompt_registry.PromptError, scoring.ScoringError) as e:
        raise ValidationFailed(str(e), code="ai_configuration_invalid") from e
    session.execute(update(HighlightRequest).where(
        HighlightRequest.recording_id == recording_id,
        HighlightRequest.status.in_([RequestStatus.PENDING.value, RequestStatus.RUNNING.value]))
        .values(status=RequestStatus.SUPERSEDED.value, completed_at=utcnow()))
    req = HighlightRequest(
        recording_id=recording_id, game=game or prompt.meta.get("game"), channel=channel,
        prompt_id=prompt.id, target_count=target_count,
        settings_json=json.dumps({
            "prompt_version": prompt.version, "prompt_sha256": prompt.sha256,
            "scoring": ranking.identity(), "model": settings.ai_model,
            "provider": settings.ai_provider, "windows_per_call": settings.ai_windows_per_call,
            "max_windows": settings.ai_max_windows, "max_repairs": settings.ai_max_repairs,
        }, sort_keys=True))
    session.add(req)
    session.flush()
    job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                           Job.job_type == JobType.AI_HIGHLIGHTS.value))
    if job is None:
        job = queue.enqueue(session, recording_id, JobType.AI_HIGHLIGHTS,
                            settings.job_max_attempts)
    elif job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
        queue.rearm(session, job)
    req.job_id = job.id
    session.flush()
    return req


def fail_active(session: Session, recording_id: str, error: str) -> int:
    """Mark the recording's unfinished requests FAILED (the AI job failed for good)."""
    n = 0
    for r in session.scalars(select(HighlightRequest).where(
            HighlightRequest.recording_id == recording_id,
            HighlightRequest.status.in_([RequestStatus.PENDING.value,
                                         RequestStatus.RUNNING.value]))):
        r.status, r.error, r.completed_at = RequestStatus.FAILED.value, error[:2000], utcnow()
        n += 1
    return n


JOB_FAILED_PREFIX = "the AI job failed: "


def reactivate_failed(session: Session, recording_id: str) -> HighlightRequest | None:
    """On a manual retry of the failed AI job: put the request that the job's failure
    ended back to PENDING (finished batches and its pinned windows are kept, so the retry
    resumes). A request that failed for another reason, or any newer request, is left."""
    if active_request(session, recording_id) is not None:
        return None
    latest = session.scalar(select(HighlightRequest).where(
        HighlightRequest.recording_id == recording_id,
        HighlightRequest.status != RequestStatus.SUPERSEDED.value)
        .order_by(HighlightRequest.created_at.desc()))
    if latest is None or latest.status != RequestStatus.FAILED.value or \
            not (latest.error or "").startswith(JOB_FAILED_PREFIX):
        return None
    latest.status, latest.error, latest.completed_at = RequestStatus.PENDING.value, None, None
    return latest


def active_request(session: Session, recording_id: str) -> HighlightRequest | None:
    return session.scalar(select(HighlightRequest).where(
        HighlightRequest.recording_id == recording_id,
        HighlightRequest.status.in_([RequestStatus.PENDING.value, RequestStatus.RUNNING.value]))
        .order_by(HighlightRequest.created_at.desc()))


# ---------------------------------------------------------------------------- windows
def windows(session: Session, recording: Recording, *, transcript_id: str | None,
            trigger_words: list[dict], audio_run_id: str | None, visual_run_id: str,
            max_windows: int) -> list[PlannedWindow]:
    """Candidate windows (M6 stages B and C, deterministic, from stored analysis)."""
    visual = session.get(SignalRun, visual_run_id)
    audio = session.get(SignalRun, audio_run_id) if audio_run_id else None
    segs = [] if transcript_id is None else [
        {"start_ms": x.start_ms, "end_ms": x.end_ms} for x in session.scalars(
            select(TranscriptSegment).where(TranscriptSegment.transcript_id == transcript_id)
            .order_by(TranscriptSegment.idx))]
    seed_list = highlight_candidates.seeds(
        visual_series=signals.series(visual), trigger_words=trigger_words,
        audio_events=signals.events(audio) if audio else [],
        visual_events=signals.events(visual), duration_ms=recording.duration_ms,
        transcript_id=transcript_id, audio_run_id=audio_run_id, visual_run_id=visual_run_id)
    return highlight_candidates.plan(seed_list, segs, recording.duration_ms, max_windows,
                                     max_window_ms=TARGET_MAX_MS)


def pinned_plan(req: HighlightRequest) -> list[PlannedWindow] | None:
    """The windows planned for this request on its first run, if any."""
    if not req.plan_json:
        return None
    return [PlannedWindow(**w) for w in json.loads(req.plan_json)]


def pin_plan(req: HighlightRequest, planned: list[PlannedWindow]) -> None:
    req.plan_json = json.dumps([asdict(w) for w in planned], sort_keys=True)


def bundle_for(session: Session, settings: Settings, recording: Recording,
               planned: list[PlannedWindow], game: str | None) -> tuple[EvidenceBundle, dict]:
    """The ``evidence.v1`` bundle for some windows, built from the analysis-evidence layer.
    Words that cannot support a claim (suspected hallucinations, degenerate timing) are
    left out so the model cannot cite them; the omissions are counted in ``meta``."""
    words: dict[str, Word] = {}
    events: dict[str, Event] = {}
    keyframes: dict[str, Keyframe] = {}
    meta: dict = {"windows": [asdict(w) for w in planned], "omitted_words": 0,
                  "truncated_windows": [], "bundles": []}
    transcript_id = None
    for w in planned:
        b = analysis_evidence.window(session, settings, recording.id, w.start_ms, w.end_ms,
                                     max_words=MAX_WORDS_PER_WINDOW + 1,
                                     max_events=MAX_EVENTS_PER_WINDOW + 1)
        meta["bundles"].append({"window": w.id, "fingerprint": b["fingerprint"]})
        if b.get("audio", {}).get("available"):      # the run the evidence came from
            meta["audio_run_id"] = b["audio"]["run_id"]
        tr = b.get("transcript", {})
        if tr.get("available"):
            transcript_id = tr["id"]
            ws = tr["words"]
            if tr.get("truncated") or len(ws) > MAX_WORDS_PER_WINDOW:
                meta["truncated_windows"].append(w.id)
            for x in ws[:MAX_WORDS_PER_WINDOW]:
                if not x["reliability"]["can_support"]:
                    meta["omitted_words"] += 1
                    continue
                words[x["id"]] = Word(id=x["id"], t0=x["start_ms"], t1=x["end_ms"],
                                      text=x["text"])
        evs = b.get("events", [])
        if len(evs) > MAX_EVENTS_PER_WINDOW and w.id not in meta["truncated_windows"]:
            meta["truncated_windows"].append(w.id)
        for e in evs[:MAX_EVENTS_PER_WINDOW]:
            events[e["id"]] = Event(id=e["id"], t0=e["start_ms"], t1=max(e["end_ms"],
                                                                         e["start_ms"]),
                                    kind=e["type"], nature=e["kind"])
        for k in b.get("visual", {}).get("keyframes", []) if b.get("visual", {}).get(
                "available") else []:
            keyframes[k["id"]] = Keyframe(id=k["id"], t=k["pts_ms"])
    bundle = EvidenceBundle(
        recording_id=recording.id, transcript_id=transcript_id,
        duration_ms=recording.duration_ms,
        windows=tuple(Window(id=w.id, t0=w.start_ms, t1=w.end_ms) for w in planned),
        words=tuple(sorted(words.values(), key=lambda x: (x.t0, x.id))),
        events=tuple(sorted(events.values(), key=lambda x: (x.t0, x.id))),
        keyframes=tuple(sorted(keyframes.values(), key=lambda x: (x.t, x.id))), game=game)
    return bundle, meta


# --------------------------------------------------------------------------- batches
def finished_batches(session: Session, request_id: str) -> set[int]:
    """Batches that need no new call: accepted, or failed validation (a final answer)."""
    return set(session.scalars(select(AIRun.batch).where(
        AIRun.request_id == request_id,
        AIRun.status.in_([RunStatus.ACCEPTED.value, RunStatus.FAILED_VALIDATION.value]))))


def evaluate_batch(bundle: EvidenceBundle, provider: Provider, req: HighlightRequest,
                   settings: Settings) -> analyze.Outcome:
    prompt = prompt_registry.load(req.prompt_id, json.loads(req.settings_json)
                                  .get("prompt_version"))
    ranking = scoring.resolve(game=req.game, channel=req.channel, prompt=prompt.id)
    identity = None
    try:
        identity = provider.identity()
    except Exception:  # noqa: BLE001 - identity is best effort; the call reports errors
        identity = None
    return analyze.evaluate(bundle, provider, prompt, target_count=len(bundle.windows),
                            channel=req.channel, game=req.game,
                            max_repairs=settings.ai_max_repairs,
                            timeout_s=settings.ai_timeout_seconds, identity=identity,
                            ranking=ranking, keep_alive_during="2m", unload_after=True)


def store_run(session: Session, req: HighlightRequest, batch: int, outcome: analyze.Outcome,
              bundle: EvidenceBundle, meta: dict, settings: Settings,
              status: str | None = None) -> AIRun:
    rec = outcome.record()
    ident = rec.get("provider") or {"provider": settings.ai_provider,
                                    "model": settings.ai_model, "digest": None}
    run = AIRun(
        request_id=req.id, batch=batch, status=status or outcome.status,
        provider=ident.get("provider") or settings.ai_provider,
        model=ident.get("model") or settings.ai_model, model_digest=ident.get("digest"),
        identity_json=json.dumps(ident, sort_keys=True), prompt_id=outcome.prompt_id,
        prompt_version=outcome.prompt_version, prompt_sha256=outcome.prompt_sha256,
        contract_version=OUTPUT_CONTRACT_VERSION, contract_sha256=output_contract_sha256(),
        scoring_json=json.dumps((outcome.ranking or {}).get("profile")
                                or json.loads(req.settings_json)["scoring"], sort_keys=True),
        evidence_fingerprint=bundle.fingerprint(),
        evidence_json=json.dumps({"bundle": bundle.model_dump(by_alias=True), "meta": meta},
                                 sort_keys=True, ensure_ascii=False),
        record_json=json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str),
        raw_last=outcome.raw_last or "", error=outcome.error,
        attempts=len(outcome.attempts), wall_s=outcome.wall_s, cost_usd=0.0)
    session.add(run)
    session.flush()
    return run


# --------------------------------------------------------------------------- finalize
def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if inter > 0 and union > 0 else 0.0


def _editorial(session: Session, req: HighlightRequest, row: dict, min_score: int) -> None:
    """M6 stages E/F on one validated candidate: refine its boundaries, apply measured
    penalties to the editorial score, re-apply the cut, and drop a title or hook that
    makes a factual claim the clip's own words do not contain."""
    c, clip = row["c"], row["model_clip"]
    recording = session.get(Recording, req.recording_id)
    words: list[dict] = []
    if row["transcript_id"]:
        words = transcripts.words(session, row["transcript_id"],
                                  max(0, clip["start_ms"] - MAX_CLIP_MS),
                                  clip["end_ms"] + MAX_CLIP_MS)
    # The audio run the model's evidence came from (not whatever is current now).
    audio_id = row.get("audio_run_id")
    audio = session.get(SignalRun, audio_id) if audio_id else None
    series = signals.series(audio) if audio else None
    assessed = evidence_reliability.assess_words(
        words, series, audio_state="measured" if audio else "pending")
    reliability = {f"w:{row['transcript_id']}:{w['idx']}": a
                   for w, a in zip(words, assessed, strict=True)}
    # Every word except suspected hallucinations: a boundary never cuts into speech, and
    # low-confidence speech is still speech (not dead air).
    usable = [w for w, a in zip(words, assessed, strict=True) if a["caption"] == "show"]
    silences = series.get("silences_ms", []) if series else []
    cited = []
    for ref_ in c.get("evidence", []):
        for item in ref_.get("ids", []):
            try:
                r_ = analysis_evidence.resolve(session, item)
            except NotFoundError:
                continue
            if "start_ms" in r_:
                cited.append((int(r_["start_ms"]), int(r_.get("end_ms", r_["start_ms"]))))
            elif "pts_ms" in r_:
                cited.append((int(r_["pts_ms"]), int(r_["pts_ms"])))
    ref = highlight_candidates.refine(clip["start_ms"], clip["end_ms"], usable,
                                      recording.duration_ms, max_ms=TARGET_MAX_MS,
                                      silences=silences if audio else None, cited=cited)
    row["clip"] = {"start_ms": ref["start_ms"], "end_ms": ref["end_ms"]}
    row["boundary_notes"] = ref["notes"]
    row["penalties"] = highlight_candidates.penalties(
        start_ms=ref["start_ms"], end_ms=ref["end_ms"], candidate=c, words=usable,
        silences=silences, word_reliability=reliability)
    row["score"] = max(0, row["model_score"] - sum(p["points"] for p in row["penalties"]))
    row["below_cut"] = row["score"] < min_score
    row["cut_reason"] = None
    if ref["end_ms"] - ref["start_ms"] > TARGET_MAX_MS:
        row["below_cut"] = True
        row["cut_reason"] = (f"{(ref['end_ms'] - ref['start_ms']) / 1000:.1f} s is longer "
                             f"than the {TARGET_MAX_MS // 1000} s Short limit")
    elif row["below_cut"]:
        why = f"editorial score {row['score']} below {min_score}"
        if row["penalties"]:
            why += " after penalties: " + ", ".join(p["name"] for p in row["penalties"])
        row["cut_reason"] = why
    in_clip = [w for w in usable if ref["start_ms"] <= w["start_ms"] < ref["end_ms"]]
    row["title"], row["hook"], row["text_checks"] = c["title"], c["hook"], []
    for key, neutral in (("title", f"{c['category'].replace('_', ' ').capitalize()} moment"),
                         ("hook", "")):
        ok, bad = highlight_candidates.ground_text(c[key], in_clip)
        if not ok:
            row[key] = neutral
            row["text_checks"].append(f"{key} dropped: unsupported claim(s) {bad}; the clip's "
                                      "words do not contain them")


def finalize(session: Session, req: HighlightRequest) -> dict:
    """Rank every accepted candidate across batches, suppress duplicates (IoU > 0.5 keeps
    the higher score), keep at most ``target_count`` and store every candidate with its
    outcome. Fewer than requested is a normal result, never filled."""
    runs = list(session.scalars(select(AIRun).where(AIRun.request_id == req.id)
                                .order_by(AIRun.batch, AIRun.created_at)))
    final = {}
    for r in runs:                                   # the last run of each batch counts
        final[r.batch] = r
    rows: list[dict] = []
    for r in final.values():
        if r.status != RunStatus.ACCEPTED.value:
            continue
        rec = json.loads(r.record_json)
        clips = {c["window_id"]: c for c in rec["clips"]}
        kept = {k["window_id"]: k for k in rec["ranking"]["kept"]}
        rejected = {k["window_id"]: k for k in rec["ranking"]["rejected"]}
        fp = r.evidence_fingerprint
        tr = json.loads(r.evidence_json)["bundle"].get("transcript_id")
        min_score = int(json.loads(r.scoring_json).get("min_score", scoring.DEFAULT_MIN_SCORE))
        windows_meta = {w["id"]: w for w in json.loads(r.evidence_json)["meta"]["windows"]}
        for c in rec["result"]["candidates"]:
            clip = clips[c["window_id"]]
            score_row = kept.get(c["window_id"]) or rejected[c["window_id"]]
            row = {"run": r, "c": c, "model_clip": clip, "model_score":
                   score_row["editorial_score"], "fingerprint": fp, "transcript_id": tr,
                   "window": windows_meta.get(c["window_id"], {}),
                   "audio_run_id": json.loads(r.evidence_json)["meta"].get("audio_run_id")}
            _editorial(session, req, row, min_score)
            rows.append(row)
    rows.sort(key=lambda x: (-x["score"], x["clip"]["start_ms"], x["c"]["window_id"]))
    chosen: list[dict] = []
    for row in rows:
        span = (row["clip"]["start_ms"], row["clip"]["end_ms"])
        if row["below_cut"]:
            row["status"], row["reason"] = "rejected", row["cut_reason"]
        elif any(_iou(span, (o["clip"]["start_ms"], o["clip"]["end_ms"])) > 0.5
                 for o in chosen):
            row["status"], row["reason"] = "rejected", "duplicate of a higher-scored clip"
        elif len(chosen) >= req.target_count:
            row["status"], row["reason"] = "rejected", (
                f"beyond the requested {req.target_count} highlights")
        else:
            row["status"], row["reason"] = "selected", None
            chosen.append(row)
    for row in rows:
        c = row["c"]
        rank = chosen.index(row) + 1 if row in chosen else None
        session.add(HighlightCandidate(
            request_id=req.id, ai_run_id=row["run"].id, status=row["status"], rank=rank,
            reject_reason=row["reason"], window_id=c["window_id"],
            start_ms=row["clip"]["start_ms"], end_ms=row["clip"]["end_ms"],
            start_id=c["start_id"], end_id=c["end_id"], category=c["category"],
            title=row["title"], hook=row["hook"], what_happens=c["what_happens"],
            why_entertaining=c["why_entertaining"],
            scores_json=json.dumps(c["scores"], sort_keys=True),
            editorial_score=row["score"], confidence=c["confidence"],
            evidence_json=json.dumps({
                "refs": c["evidence"], "model_clip": [row["model_clip"]["start_ms"],
                                                      row["model_clip"]["end_ms"]],
                "boundary_notes": row["boundary_notes"], "penalties": row["penalties"],
                "model_editorial_score": row["model_score"], "text_checks": row["text_checks"],
                "window": row["window"]}, sort_keys=True, ensure_ascii=False),
            uncertainty=c.get("uncertainty", ""), transcript_id=row["transcript_id"],
            evidence_fingerprint=row["fingerprint"]))
    statuses = [r.status for r in final.values()]
    summary = {
        "requested": req.target_count, "returned": len(chosen),
        "candidates_validated": len(rows),
        "batches": len(final), "batch_status": statuses,
        "fewer_than_requested": len(chosen) < req.target_count,
        "reason": None if len(chosen) >= req.target_count else (
            "only these candidates passed validation, the editorial cut and duplicate "
            "suppression"),
        "editorial_score_meaning": "estimated editorial quality 0-100; not a probability "
                                   "of going viral",
    }
    accepted = statuses.count(RunStatus.ACCEPTED.value)
    req.status = (RequestStatus.COMPLETE.value if accepted or not statuses
                  else RequestStatus.FAILED.value)
    if req.status == RequestStatus.FAILED.value:
        req.error = "no batch produced a valid answer: " + "; ".join(
            (r.error or r.status)[:200] for r in final.values())
    req.summary_json = json.dumps(summary, sort_keys=True)
    req.completed_at = utcnow()
    session.flush()
    return summary


# ------------------------------------------------------------------------------ read
def describe(session: Session, recording_id: str) -> dict:
    recording = session.get(Recording, recording_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    req = session.scalar(select(HighlightRequest).where(
        HighlightRequest.recording_id == recording_id,
        HighlightRequest.status != RequestStatus.SUPERSEDED.value)
        .order_by(HighlightRequest.created_at.desc()))
    if req is None:
        return {"request": None, "job": None, "highlights": [], "rejected": [], "runs": []}
    job = session.get(Job, req.job_id) if req.job_id else None
    cands = list(session.scalars(select(HighlightCandidate).where(
        HighlightCandidate.request_id == req.id)))
    runs = list(session.scalars(select(AIRun).where(AIRun.request_id == req.id)
                                .order_by(AIRun.batch, AIRun.created_at)))

    def cand(c: HighlightCandidate) -> dict:
        ev = json.loads(c.evidence_json)
        if isinstance(ev, list):                      # M5 rows: the references only
            ev = {"refs": ev}
        return {"id": c.id, "rank": c.rank, "status": c.status, "reason": c.reject_reason,
                "start_ms": c.start_ms, "end_ms": c.end_ms, "start_id": c.start_id,
                "end_id": c.end_id, "category": c.category, "title": c.title,
                "hook": c.hook, "what_happens": c.what_happens,
                "why_entertaining": c.why_entertaining,
                "editorial_score": c.editorial_score, "confidence": c.confidence,
                "scores": json.loads(c.scores_json), "evidence": ev.get("refs", []),
                "model_clip": ev.get("model_clip"), "boundary_notes": ev.get("boundary_notes", []),
                "penalties": ev.get("penalties", []),
                "model_editorial_score": ev.get("model_editorial_score"),
                "text_checks": ev.get("text_checks", []), "window": ev.get("window"),
                "uncertainty": c.uncertainty, "review_status": c.review_status,
                "transcript_id": c.transcript_id, "ai_run_id": c.ai_run_id}
    selected = sorted((c for c in cands if c.status == "selected"), key=lambda c: c.rank)
    return {
        "request": {"id": req.id, "status": req.status, "game": req.game,
                    "channel": req.channel, "prompt_id": req.prompt_id,
                    "target_count": req.target_count, "error": req.error,
                    "summary": json.loads(req.summary_json) if req.summary_json else None,
                    "created_at": req.created_at.isoformat()},
        "job": None if job is None else {"id": job.id, "status": job.status,
                                         "progress_percent": job.progress_percent,
                                         "last_error": job.last_error},
        "highlights": [cand(c) for c in selected],
        "rejected": [cand(c) for c in sorted(
            (c for c in cands if c.status == "rejected"), key=lambda c: -c.editorial_score)],
        "runs": [{"id": r.id, "batch": r.batch, "status": r.status, "model": r.model,
                  "prompt": f"{r.prompt_id} v{r.prompt_version}", "attempts": r.attempts,
                  "wall_s": r.wall_s, "error": r.error, "cost_usd": r.cost_usd}
                 for r in runs],
    }


def set_review(session: Session, recording_id: str, candidate_id: str, status: str) -> dict:
    c = session.get(HighlightCandidate, candidate_id)
    req = session.get(HighlightRequest, c.request_id) if c else None
    if c is None or req is None or req.recording_id != recording_id:
        raise NotFoundError("Highlight not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    if status not in ("pending", "approved", "rejected"):
        raise ValidationFailed("review status must be pending, approved or rejected")
    c.review_status = status
    session.flush()
    return {"id": c.id, "review_status": c.review_status}
