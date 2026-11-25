"""Shared, persistent, versioned transcripts (M4; docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md).

One transcript per (recording, transcription configuration). The ``TRANSCRIBE`` job is the
only code that runs Whisper; every consumer (highlight scan, suggestions, captions,
subtitles) reads the stored words through :func:`words`.

* **Configuration identity:** engine, model id, model SHA-256 (``weights.npz`` +
  ``config.json``), language, chunk and overlap lengths, audio format and merge version,
  hashed into a fingerprint. A new configuration gives a new transcript beside the old one.
* **Chunks:** the recording is cut into fixed "core" spans; each chunk is transcribed with
  ``overlap`` on both sides, in a fresh child process, and stored (raw engine output, gzip)
  as soon as it finishes. A DONE chunk is never transcribed again: a crash, a timeout or a
  shutdown resumes at the first unfinished chunk.
* **Deterministic merge (version 2):** at each chunk boundary, the overlap zone (heard by
  both chunks) goes to the chunk whose words there are clearly better (mean probability,
  degenerate zero-length or near-zero-probability words counting as 0); otherwise the
  core edge stands (version 1 behaviour). A word belongs to the chunk owning its midpoint.
  Two words of different chunks that overlap by at least half of the shorter and have the
  same normalised text are one word (higher probability kept; on a tie, the one that
  sorts first by start time, then chunk index).
  The result never depends on the order chunks finished in. Version 2 exists because on
  real speech Whisper repeated an earlier phrase at the end of a chunk, and the fixed core
  edge kept that copy over the next chunk's correct words.
* **Immutability:** raw words, segments, DONE chunks and complete transcripts cannot be
  changed or deleted (database triggers, migration 0010). Timing corrections are a
  separate, versioned layer, applied only when validated and asked for.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermanentJobError,
    ServiceUnavailable,
    TransientJobError,
)
from app.db.base import utcnow
from app.models import (
    ChunkStatus,
    Job,
    JobStatus,
    JobType,
    Recording,
    RecordingStatus,
    Severity,
    Transcript,
    TranscriptChunk,
    TranscriptCorrection,
    TranscriptionConfig,
    TranscriptSegment,
    TranscriptStatus,
    TranscriptWord,
)
from app.services.events import record_event

MERGE_VERSION = 2
CHILD_PERMANENT_EXITS = {64, 66}     # whisper_child: unsupported options, model missing
# Decoding options passed to the child (part of the configuration fingerprint).
# condition_on_previous_text=False: without it, Whisper repeated an earlier phrase over new
# speech at the end of a chunk on the Mac (M4 native run, 2026-11-25).
DECODE_OPTIONS = {"condition_on_previous_text": False}
# Merge v2: who owns each overlap zone is decided by measured word quality.
QUALITY_MARGIN = 0.15        # mean-quality difference needed to move a boundary
DEGENERATE_MS = 20           # a word shorter than this carries no timing information
DEGENERATE_PROB = 0.05
CHILD = Path(__file__).resolve().parents[1] / "workers" / "whisper_child.py"
_NORM = re.compile(r"[^\w']+", re.UNICODE)
_checksums: dict[tuple, str] = {}


# ------------------------------------------------------------------------ configuration
def model_sha256(model_dir: Path) -> str:
    """SHA-256 over the model's ``config.json`` and ``weights.npz`` (streamed; cached per
    path, size and modification time)."""
    files = [model_dir / "config.json", model_dir / "weights.npz"]
    missing = [f.name for f in files if not f.is_file()]
    if missing:
        raise PermanentJobError(f"The Whisper model at {model_dir.name} is incomplete "
                                f"(missing {', '.join(missing)}). Nothing is downloaded.")
    key = (str(model_dir.resolve()),
           *((f.stat().st_mtime_ns, f.stat().st_size) for f in files))
    cached = _checksums.get(key)
    if cached:
        return cached
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0")
        with f.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    digest = h.hexdigest()
    _checksums[key] = digest
    return digest


def require_engine(settings: Settings) -> tuple[Path, Path]:
    py, model = settings.clip_whisper_python, settings.clip_whisper_model
    if py is None or model is None or not py.is_file() or not model.is_dir():
        raise PermanentJobError("Local Whisper is not configured: set RIFTWIRE_CLIP_WHISPER_"
                                "PYTHON and RIFTWIRE_CLIP_WHISPER_MODEL (installed model; "
                                "nothing is downloaded).")
    return py, model


def current_config(settings: Settings) -> dict:
    _, model = require_engine(settings)
    return {
        "engine": "mlx-whisper",
        "model_id": model.name,
        "model_sha256": model_sha256(model),
        "language": settings.transcribe_language,
        "chunk_ms": settings.transcribe_chunk_seconds * 1000,
        "overlap_ms": settings.transcribe_overlap_seconds * 1000,
        "audio": "pcm_s16le/16000/mono",
        "word_timestamps": True,
        "decode": dict(DECODE_OPTIONS),
        "merge_version": MERGE_VERSION,
    }


def fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def ensure_config(session: Session, config: dict) -> TranscriptionConfig:
    fp = fingerprint(config)
    row = session.scalar(select(TranscriptionConfig).where(TranscriptionConfig.fingerprint == fp))
    if row is None:
        row = TranscriptionConfig(fingerprint=fp, config_json=json.dumps(config, sort_keys=True))
        session.add(row)
        session.flush()
    return row


# ------------------------------------------------------------------------------ chunks
@dataclass(frozen=True)
class ChunkPlan:
    idx: int
    start_ms: int          # extracted span, overlap included
    end_ms: int
    core_start_ms: int     # the span this chunk owns in the merge
    core_end_ms: int


def plan_chunks(duration_ms: int, chunk_ms: int, overlap_ms: int) -> list[ChunkPlan]:
    if duration_ms <= 0:
        return []
    plans, core = [], 0
    while core < duration_ms:
        core_end = min(duration_ms, core + chunk_ms)
        plans.append(ChunkPlan(len(plans), max(0, core - overlap_ms),
                               min(duration_ms, core_end + overlap_ms), core, core_end))
        core = core_end
    return plans


def _norm(text: str) -> str:
    return " ".join(_NORM.sub(" ", text.lower()).split())


def _absolute(plan: ChunkPlan, raw: dict, duration_ms: int) -> list[dict]:
    out = []
    for w in raw.get("words", []):
        text = str(w.get("w", "")).strip()
        if not text:
            continue
        start = int(round(plan.start_ms + float(w["start_ms"])))
        end = int(round(plan.start_ms + float(w["end_ms"])))
        start, end = max(0, min(start, duration_ms)), max(0, min(end, duration_ms))
        out.append({"start_ms": start, "end_ms": max(end, start), "text": text,
                    "prob": w.get("prob"), "chunk_idx": plan.idx, "raw_seg": w.get("seg")})
    return out


def _quality(words: list[dict]) -> float | None:
    """Mean word quality: the engine's probability, or 0 for a degenerate word (no
    duration, or near-zero probability). ``None`` when there are no words."""
    if not words:
        return None
    q = [0.0 if (w["end_ms"] - w["start_ms"] < DEGENERATE_MS
                 or (w["prob"] or 0) < DEGENERATE_PROB) else float(w["prob"] or 0)
         for w in words]
    return sum(q) / len(q)


def boundaries(chunks: list[tuple[ChunkPlan, dict]], duration_ms: int) -> list[dict]:
    """Merge v2 ownership decision for each chunk boundary.

    The overlap zone is where both chunks heard the same audio. When both produced words
    there and one chunk's words are clearly better (mean quality higher by at least
    ``QUALITY_MARGIN``), that chunk owns the whole zone; otherwise the nominal boundary (the
    core edge) stands. A side with no words never wins by default: silence on one side
    and words on the other is ambiguous, so the nominal boundary stands."""
    ordered = sorted(chunks, key=lambda c: c[0].idx)
    words = {p.idx: _absolute(p, r, duration_ms) for p, r in ordered}
    out = []
    for (a, _), (b, _) in zip(ordered, ordered[1:], strict=False):
        lo, hi, nominal = b.start_ms, a.end_ms, a.core_end_ms
        in_zone = {i: [w for w in words[i] if lo <= (w["start_ms"] + w["end_ms"]) / 2 < hi]
                   for i in (a.idx, b.idx)}
        qa, qb = _quality(in_zone[a.idx]), _quality(in_zone[b.idx])
        cut, owner = nominal, "nominal"
        if qa is not None and qb is not None and abs(qa - qb) >= QUALITY_MARGIN:
            cut, owner = (hi, "earlier") if qa > qb else (lo, "later")
        out.append({"after_chunk": a.idx, "nominal_ms": nominal, "cut_ms": cut,
                    "owner": owner, "zone_ms": [lo, hi],
                    "quality": [None if qa is None else round(qa, 3),
                                None if qb is None else round(qb, 3)],
                    "words": [len(in_zone[a.idx]), len(in_zone[b.idx])]})
    return out


def merge(chunks: list[tuple[ChunkPlan, dict]], duration_ms: int) -> tuple[list[dict],
                                                                             list[dict]]:
    """Deterministic merge (``MERGE_VERSION`` 2) of per-chunk engine output into one
    recording-relative word list and segment list. Each chunk owns the span between its
    two :func:`boundaries` cuts; a word belongs to the chunk owning its midpoint; the same
    word seen by two chunks is kept once. Independent of the order chunks finished in."""
    ordered = sorted(chunks, key=lambda c: c[0].idx)
    cuts = [b["cut_ms"] for b in boundaries(ordered, duration_ms)]
    candidates: list[dict] = []
    for n, (plan, raw) in enumerate(ordered):
        own_start = cuts[n - 1] if n > 0 else 0
        own_end = cuts[n] if n < len(cuts) else duration_ms
        last = n == len(ordered) - 1
        for w in _absolute(plan, raw, duration_ms):
            mid = (w["start_ms"] + w["end_ms"]) / 2
            if own_start <= mid < own_end or (last and mid == duration_ms):
                candidates.append(w)
    candidates.sort(key=lambda x: (x["start_ms"], x["chunk_idx"], x["end_ms"]))
    kept: list[dict] = []
    for c in candidates:
        dup = None
        for k in reversed(kept[-4:]):
            if k["chunk_idx"] == c["chunk_idx"]:
                continue
            inter = min(k["end_ms"], c["end_ms"]) - max(k["start_ms"], c["start_ms"])
            shorter = max(1, min(k["end_ms"] - k["start_ms"], c["end_ms"] - c["start_ms"]))
            if inter >= shorter / 2 and _norm(k["text"]) == _norm(c["text"]):
                dup = k
                break
        if dup is None:
            kept.append(c)
        elif (c["prob"] or 0) > (dup["prob"] or 0):
            kept[kept.index(dup)] = c
    kept.sort(key=lambda x: (x["start_ms"], x["chunk_idx"], x["end_ms"]))
    segments: list[dict] = []
    key = None
    for i, w in enumerate(kept):
        w["idx"] = i
        k = (w["chunk_idx"], w["raw_seg"])
        if k != key or w["raw_seg"] is None:
            segments.append({"idx": len(segments), "start_ms": w["start_ms"],
                             "end_ms": w["end_ms"], "first_word_idx": i,
                             "last_word_idx": i, "words": [w["text"]]})
            key = k
        else:
            seg = segments[-1]
            seg["end_ms"] = max(seg["end_ms"], w["end_ms"])
            seg["last_word_idx"] = i
            seg["words"].append(w["text"])
        w["segment_idx"] = segments[-1]["idx"]
    for seg in segments:
        seg["text"] = " ".join(seg.pop("words"))
    return kept, segments


# -------------------------------------------------------------------- transcript lookup
def current(session: Session, settings: Settings, recording_id: str) -> Transcript | None:
    """The COMPLETE transcript of this recording for the current configuration."""
    try:
        config = current_config(settings)
    except PermanentJobError:
        return None
    row = session.scalar(select(TranscriptionConfig).where(
        TranscriptionConfig.fingerprint == fingerprint(config)))
    if row is None:
        return None
    return session.scalar(select(Transcript).where(
        Transcript.recording_id == recording_id, Transcript.config_id == row.id,
        Transcript.status == TranscriptStatus.COMPLETE.value))


def words(session: Session, transcript_id: str, start_ms: int | None = None,
          end_ms: int | None = None, *, corrected: bool = False) -> list[dict]:
    """Words whose midpoint lies in ``[start_ms, end_ms)``, recording-relative, each with
    its stable index. ``corrected=True`` applies the newest *validated* correction (raw
    words are never changed); without one it refuses rather than silently returning raw."""
    q = select(TranscriptWord).where(TranscriptWord.transcript_id == transcript_id)
    if start_ms is not None:
        q = q.where(TranscriptWord.end_ms >= start_ms)
    if end_ms is not None:
        q = q.where(TranscriptWord.start_ms <= end_ms)
    rows = session.scalars(q.order_by(TranscriptWord.idx)).all()
    offset = 0
    if corrected:
        corr = _validated(session, transcript_id)
        if corr is None:
            raise ConflictError("No validated timing correction exists for this transcript.",
                                code="no_validated_correction")
        offset = int(json.loads(corr.params_json)["offset_ms"])
    out = []
    for r in rows:
        mid = (r.start_ms + r.end_ms) / 2
        if (start_ms is not None and mid < start_ms) or (end_ms is not None and mid >= end_ms):
            continue
        out.append({"idx": r.idx, "w": r.text, "start_ms": r.start_ms + offset,
                    "end_ms": r.end_ms + offset, "prob": r.prob})
    return out


def _validated(session: Session, transcript_id: str) -> TranscriptCorrection | None:
    return session.scalar(select(TranscriptCorrection).where(
        TranscriptCorrection.transcript_id == transcript_id,
        TranscriptCorrection.validated.is_(True)).order_by(
        TranscriptCorrection.version.desc()))


def latest_validated_correction(session: Session, transcript_id: str) -> int | None:
    corr = _validated(session, transcript_id)
    return corr.version if corr else None


def config_fingerprint(session: Session, transcript: Transcript) -> str:
    return session.get(TranscriptionConfig, transcript.config_id).fingerprint


def words_for_clip(session: Session, transcript_id: str, start_ms: int,
                   length_ms: int) -> list[dict]:
    """Words inside a clip, re-based to clip time (what captions and subtitles use)."""
    return [{**w, "start_ms": max(0, w["start_ms"] - start_ms),
             "end_ms": min(length_ms, w["end_ms"] - start_ms)}
            for w in words(session, transcript_id, start_ms, start_ms + length_ms)]


def add_correction(session: Session, transcript_id: str, offset_ms: int, *,
                   validated: bool = False, validation_ref: str | None = None
                   ) -> TranscriptCorrection:
    """A new correction version (constant offset). Earlier versions stay unchanged."""
    if session.get(Transcript, transcript_id) is None:
        raise NotFoundError("Transcript not found")
    latest = session.scalar(select(TranscriptCorrection.version).where(
        TranscriptCorrection.transcript_id == transcript_id).order_by(
        TranscriptCorrection.version.desc())) or 0
    row = TranscriptCorrection(transcript_id=transcript_id, version=latest + 1,
                               method="constant_offset",
                               params_json=json.dumps({"offset_ms": int(offset_ms)}),
                               validated=validated, validation_ref=validation_ref)
    session.add(row)
    session.flush()
    return row


# ------------------------------------------------------------------- requesting a run
def request(session: Session, settings: Settings, recording_id: str) -> Job | None:
    """Make sure a TRANSCRIBE job will produce the current-configuration transcript.

    Returns ``None`` when it already exists. Otherwise returns the (new, pending, running
    or re-armed) TRANSCRIBE job. A finished job is re-armed only when its transcript is not
    the current configuration's (explicit regeneration after a configuration change)."""
    from app.services import queue

    if current(session, settings, recording_id) is not None:
        return None
    job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                           Job.job_type == JobType.TRANSCRIBE.value))
    if job is None:
        return queue.enqueue(session, recording_id, JobType.TRANSCRIBE,
                             settings.job_max_attempts)
    if job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
        queue.rearm(session, job)
    return job


# ------------------------------------------------------------------------ API helpers
def _ready_and_allowed(session: Session, recording_id: str) -> Recording:
    from app.services import rights

    recording = session.get(Recording, recording_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    return recording


def _job_dict(job: Job | None) -> dict | None:
    if job is None:
        return None
    return {"id": job.id, "status": job.status, "progress_percent": job.progress_percent,
            "attempt_count": job.attempt_count, "max_attempts": job.max_attempts,
            "last_error": job.last_error}


def describe(session: Session, settings: Settings, recording_id: str, *,
             include_words: bool = False, start_ms: int | None = None,
             end_ms: int | None = None) -> dict:
    """What the dashboard shows: the TRANSCRIBE job, the current-configuration transcript
    (or ``None``) and, on request, its raw words in a time range. Rights: ``analyze``."""
    _ready_and_allowed(session, recording_id)
    job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                           Job.job_type == JobType.TRANSCRIBE.value))
    try:
        config = current_config(settings)
        engine = {"available": True, "model_id": config["model_id"],
                  "model_sha256": config["model_sha256"], "language": config["language"],
                  "chunk_ms": config["chunk_ms"], "overlap_ms": config["overlap_ms"],
                  "merge_version": config["merge_version"]}
    except PermanentJobError as e:
        engine = {"available": False, "reason": str(e)}
    tr = current(session, settings, recording_id)
    out: dict = {"engine": engine, "job": _job_dict(job), "transcript": None}
    if tr is not None:
        out["transcript"] = {
            "id": tr.id, "status": tr.status, "word_count": tr.word_count,
            "duration_ms": tr.duration_ms, "coverage_ms": tr.coverage_ms,
            "engine_version": tr.engine_version,
            "completed_at": tr.completed_at.isoformat() if tr.completed_at else None,
            "timestamps": "raw",
        }
        if include_words:
            out["words"] = words(session, tr.id, start_ms, end_ms)
    return out


def request_checked(session: Session, settings: Settings, recording_id: str) -> dict:
    """``POST .../transcript``: queue transcription for the current configuration
    (explicit regeneration when the configuration changed), after the rights check."""
    recording = _ready_and_allowed(session, recording_id)
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise ConflictError("Wait for the recording to finish processing.")
    try:
        require_engine(settings)
    except PermanentJobError as e:
        raise ServiceUnavailable(str(e), code="whisper_unavailable") from e
    job = request(session, settings, recording_id)
    session.flush()
    return {"queued": job is not None, "job": _job_dict(job)}


def failed_for_current_config(session: Session, settings: Settings,
                              recording_id: str) -> str | None:
    """The error of a TRANSCRIBE job that failed for the **current** configuration, or
    ``None``. A job that failed for an older configuration is not a reason to stop: the
    next request re-arms it for the new one."""
    job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                           Job.job_type == JobType.TRANSCRIBE.value))
    if job is None or job.status != JobStatus.FAILED.value:
        return None
    ran = json.loads(job.result_json or "{}").get("config_fingerprint")
    if ran is not None and ran != fingerprint(current_config(settings)):
        return None
    return job.last_error or "transcription failed"


# ---------------------------------------------------------------- the TRANSCRIBE job
def prepare(session: Session, settings: Settings, recording: Recording, job_id: str
            ) -> tuple[Transcript, list[TranscriptChunk]]:
    """Create (or find) the transcript row for the current configuration and its chunk
    plan. Idempotent: a re-run finds the same rows."""
    config = current_config(settings)
    cfg = ensure_config(session, config)
    job = session.get(Job, job_id)
    if job is not None:                     # which configuration this attempt works on
        job.result_json = json.dumps({"config_fingerprint": cfg.fingerprint})
    tr = session.scalar(select(Transcript).where(Transcript.recording_id == recording.id,
                                                 Transcript.config_id == cfg.id))
    if tr is None:
        tr = Transcript(recording_id=recording.id, config_id=cfg.id,
                        recording_sha256=recording.sha256, duration_ms=recording.duration_ms,
                        status=TranscriptStatus.PENDING.value, job_id=job_id)
        session.add(tr)
        session.flush()
        for p in plan_chunks(recording.duration_ms, config["chunk_ms"], config["overlap_ms"]):
            session.add(TranscriptChunk(transcript_id=tr.id, idx=p.idx, start_ms=p.start_ms,
                                        end_ms=p.end_ms, core_start_ms=p.core_start_ms,
                                        core_end_ms=p.core_end_ms,
                                        status=ChunkStatus.PENDING.value))
        session.flush()
    elif tr.status == TranscriptStatus.FAILED.value:
        tr.status, tr.error = TranscriptStatus.PENDING.value, None
    chunks = list(session.scalars(select(TranscriptChunk).where(
        TranscriptChunk.transcript_id == tr.id).order_by(TranscriptChunk.idx)))
    return tr, chunks


def store_chunk(session: Session, transcript_id: str, idx: int, raw: dict, audio_sha256: str,
                elapsed_ms: int) -> None:
    ch = session.get(TranscriptChunk, (transcript_id, idx))
    if ch is None:
        raise PermanentJobError("transcript chunk vanished")
    if ch.status == ChunkStatus.DONE.value:
        return                                  # a re-run never rewrites a finished chunk
    ch.raw_json_gz = gzip.compress(json.dumps(raw, sort_keys=True).encode(), mtime=0)
    ch.audio_sha256 = audio_sha256
    ch.elapsed_ms = elapsed_ms
    ch.status = ChunkStatus.DONE.value
    ch.completed_at = utcnow()


def finalize(session: Session, transcript_id: str) -> Transcript:
    """Merge every DONE chunk and write the immutable words and segments."""
    tr = session.get(Transcript, transcript_id)
    if tr.status == TranscriptStatus.COMPLETE.value:
        return tr
    chunks = list(session.scalars(select(TranscriptChunk).where(
        TranscriptChunk.transcript_id == transcript_id).order_by(TranscriptChunk.idx)))
    if any(c.status != ChunkStatus.DONE.value for c in chunks):
        raise PermanentJobError("cannot finalize a transcript with unfinished chunks")
    raws = [(ChunkPlan(c.idx, c.start_ms, c.end_ms, c.core_start_ms, c.core_end_ms),
             json.loads(gzip.decompress(c.raw_json_gz))) for c in chunks]
    kept, segments = merge(raws, tr.duration_ms)
    moved = [b for b in boundaries(raws, tr.duration_ms) if b["owner"] != "nominal"]
    if moved:
        record_event(session, Severity.INFO, "transcripts", "transcript_boundary_reassigned",
                     "Merge v2 gave overlap zone(s) to the chunk with better words: "
                     + json.dumps(moved, sort_keys=True), recording_id=tr.recording_id)
    for w in kept:
        session.add(TranscriptWord(transcript_id=tr.id, idx=w["idx"], start_ms=w["start_ms"],
                                   end_ms=w["end_ms"], text=w["text"], prob=w["prob"],
                                   chunk_idx=w["chunk_idx"], segment_idx=w["segment_idx"]))
    for s in segments:
        session.add(TranscriptSegment(transcript_id=tr.id, idx=s["idx"], start_ms=s["start_ms"],
                                      end_ms=s["end_ms"], text=s["text"],
                                      first_word_idx=s["first_word_idx"],
                                      last_word_idx=s["last_word_idx"]))
    versions = sorted({r.get("engine_version") for _, r in raws if r.get("engine_version")})
    tr.engine_version = ", ".join(versions)[:64] or None
    tr.word_count = len(kept)
    tr.coverage_ms = max((c.core_end_ms for c in chunks), default=0)
    tr.status = TranscriptStatus.COMPLETE.value
    tr.completed_at = utcnow()
    session.flush()
    return tr


def finalize_silent(session: Session, transcript_id: str) -> Transcript:
    """A recording without an audio track: a complete, empty transcript (consumers then
    know there is nothing to transcribe, instead of waiting forever)."""
    tr = session.get(Transcript, transcript_id)
    if tr.status != TranscriptStatus.COMPLETE.value:
        for c in session.scalars(select(TranscriptChunk).where(
                TranscriptChunk.transcript_id == transcript_id,
                TranscriptChunk.status == ChunkStatus.PENDING.value)):
            session.delete(c)
        tr.word_count, tr.coverage_ms = 0, tr.duration_ms
        tr.engine_version = "no-audio"
        tr.status = TranscriptStatus.COMPLETE.value
        tr.completed_at = utcnow()
    return tr


def transcribe_chunk(settings: Settings, source: Path, work: Path, chunk: TranscriptChunk,
                     run: Callable[[list[str], float], object]) -> tuple[dict, str, int]:
    """Extract one chunk's audio (16 kHz mono PCM) and transcribe it in a child process.
    Returns ``(raw engine output, audio SHA-256, elapsed ms)``."""
    import time

    py, model = require_engine(settings)
    audio, out = work / f"chunk-{chunk.idx}.wav", work / f"chunk-{chunk.idx}.json"
    for f in (audio, out):
        f.unlink(missing_ok=True)
    t0 = time.monotonic()
    r = run([settings.ffmpeg_path, "-nostdin", "-v", "error", "-y",
             "-ss", f"{chunk.start_ms / 1000:.3f}", "-i", str(source),
             "-t", f"{(chunk.end_ms - chunk.start_ms) / 1000:.3f}", "-vn", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", str(audio)], settings.ffmpeg_timeout_seconds)
    if getattr(r, "returncode", 1) or not audio.is_file():
        raise TransientJobError(f"could not extract audio for chunk {chunk.idx}")
    h = hashlib.sha256()
    with audio.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    r = run([str(py), str(CHILD), str(model), str(audio), str(out),
             settings.transcribe_language, json.dumps(DECODE_OPTIONS, sort_keys=True)],
            settings.transcribe_child_timeout_seconds)
    code = getattr(r, "returncode", 1)
    if code or not out.is_file():
        tail = (getattr(r, "stderr", b"") or b"")[-400:]
        if isinstance(tail, bytes):
            tail = tail.decode(errors="replace")
        if code in CHILD_PERMANENT_EXITS:     # bad options / model missing: retrying won't help
            raise PermanentJobError(f"Whisper refused chunk {chunk.idx} (exit {code}): {tail}")
        raise TransientJobError(f"Whisper failed on chunk {chunk.idx}: {tail}")
    try:
        raw = json.loads(out.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("words"), list):
            raise ValueError("no word list")
    except ValueError as e:                    # truncated or malformed output
        raise TransientJobError(f"Whisper wrote unreadable output for chunk {chunk.idx}: "
                                f"{e}") from e
    audio.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    return raw, h.hexdigest(), int((time.monotonic() - t0) * 1000)
