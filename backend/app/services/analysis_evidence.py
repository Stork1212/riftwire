"""M4 analysis-evidence layer (docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md §7).

:func:`window` gathers everything RIFTWIRE has measured about one span of a recording into
a single, bounded, traceable bundle (``analysis_evidence.v1``): words and segments from the
shared transcript, audio and visual measurements downsampled to the window, measured and
inferred events, scenes and keyframe references. OCR is reported as not available.

* **Traceability.** Every item carries a stable id that resolves to a stored row:
  ``w:<transcript>:<idx>``, ``seg:<transcript>:<idx>``, ``a:<run>:<i>``, ``v:<run>:<i>``,
  ``sc:<run>:<i>`` and ``kf:<keyframe>``. These are the ids M5's ``evidence.v1`` uses, so a
  model's evidence references can be checked against them.
* **Time.** Every time is integer milliseconds relative to the recording; the source time
  is the recording time plus ``source_offset_ms``.
* **Fingerprint.** The SHA-256 of the canonical bundle; consumers record it.
* **Size.** ``max_words``, ``max_events`` and ``max_points`` keep a bundle small enough for a
  model prompt. Whole videos are never passed anywhere.
* **Honesty.** Missing inputs are reported as missing (``available: false`` with the
  reason), never filled in. Measured and inferred items stay apart.
"""

from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationFailed
from app.models import Recording, SignalKind, TranscriptSegment
from app.services import evidence_reliability, rights, signals, transcripts

SCHEMA = "analysis_evidence.v1"
MAX_WINDOW_MS = 10 * 60 * 1000      # one bundle covers at most 10 minutes
MAX_LIST_ITEMS = 200                 # segments, scenes and keyframes per bundle
PACE_WINDOW_MS = 5000


def _pool(values: list, step_ms: int, origin_ms: int, start_ms: int, end_ms: int,
          max_points: int) -> dict:
    """Values of a regular series inside [start, end), max-pooled to at most max_points."""
    lo = max(0, (start_ms - origin_ms) // step_ms)
    hi = max(lo, math.ceil((end_ms - origin_ms) / step_ms))
    part = values[lo:hi]
    if not part:
        return {"step_ms": step_ms, "start_ms": start_ms, "values": []}
    factor = max(1, math.ceil(len(part) / max_points))
    pooled = []
    for i in range(0, len(part), factor):
        chunk = [v for v in part[i:i + factor] if v is not None]
        pooled.append(max(chunk) if chunk else None)
    return {"step_ms": step_ms * factor, "start_ms": origin_ms + lo * step_ms,
            "values": pooled, "pooling": "max"}


def _speech(words: list[dict], start_ms: int, end_ms: int) -> dict:
    """Speech activity and pace from the transcript (measured from words, not inferred)."""
    span = max(1, end_ms - start_ms)
    covered, last = 0, start_ms
    for w in sorted(words, key=lambda w: w["start_ms"]):
        a, b = max(last, w["start_ms"], start_ms), min(w["end_ms"], end_ms)
        if b > a:
            covered += b - a
            last = b
    pace = []
    t = start_ms
    while t < end_ms:
        n = sum(1 for w in words if t <= (w["start_ms"] + w["end_ms"]) / 2 < t + PACE_WINDOW_MS)
        pace.append({"start_ms": t, "words_per_s": round(n / (PACE_WINDOW_MS / 1000), 2)})
        t += PACE_WINDOW_MS
    return {"speech_fraction": round(covered / span, 3), "word_count": len(words),
            "words_per_s": round(len(words) / (span / 1000), 2), "pace": pace,
            "method": "transcript word coverage"}


def window(session: Session, settings: Settings, recording_id: str, start_ms: int,
           end_ms: int, *, corrected: bool = False, max_words: int = 400,
           max_events: int = 100, max_points: int = 120) -> dict:
    """The evidence bundle for ``[start_ms, end_ms)``. Rights: ``analyze``."""
    recording = session.get(Recording, recording_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    duration = recording.duration_ms or 0
    start_ms, end_ms = max(0, int(start_ms)), min(duration, int(end_ms))
    if end_ms - start_ms > MAX_WINDOW_MS:
        raise ValidationFailed(f"An evidence window covers at most {MAX_WINDOW_MS // 60000} "
                               "minutes; ask for several windows.")
    if end_ms <= start_ms:
        raise ValidationFailed("The window must lie inside the recording and not be empty.")
    offset = recording.source_offset_ms or 0
    bundle: dict = {
        "schema": SCHEMA,
        "recording": {
            "id": recording.id, "sha256": recording.sha256, "duration_ms": duration,
            "fps_num": recording.fps_num, "fps_den": recording.fps_den,
            "variable_frame_rate": recording.is_variable_frame_rate,
            "has_audio": recording.has_audio, "source_id": recording.source_id,
            "source_offset_ms": offset,
        },
        "window": {"start_ms": start_ms, "end_ms": end_ms,
                   "source_start_ms": start_ms + offset, "source_end_ms": end_ms + offset},
        "limits": {"max_words": max_words, "max_events": max_events,
                   "max_points": max_points},
        "ocr": {"available": False, "reason": "OCR needs an approved dependency (deferred)"},
    }

    tr = transcripts.current(session, settings, recording_id)
    if recording.has_audio is False:
        bundle["transcript"] = {"available": False, "reason": "no audio track"}
    elif tr is None:
        bundle["transcript"] = {"available": False, "reason": "not transcribed yet"}
    else:
        ws = transcripts.words(session, tr.id, start_ms, end_ms, corrected=corrected)
        correction = None
        if corrected:
            correction = transcripts.latest_validated_correction(session, tr.id)
        assessed = evidence_reliability.for_recording(session, recording, ws)
        segs = session.scalars(select(TranscriptSegment).where(
            TranscriptSegment.transcript_id == tr.id, TranscriptSegment.end_ms >= start_ms,
            TranscriptSegment.start_ms <= end_ms).order_by(TranscriptSegment.idx)).all()
        bundle["transcript"] = {
            "available": True, "id": tr.id,
            "config_fingerprint": transcripts.config_fingerprint(session, tr),
            "correction_version": correction, "timestamps": "corrected" if corrected else "raw",
            "truncated": len(ws) > max_words,
            "words": [{"id": f"w:{tr.id}:{w['idx']}", "start_ms": w["start_ms"],
                       "end_ms": w["end_ms"], "text": w["w"], "prob": w["prob"],
                       "reliability": {k: a[k] for k in ("class", "can_trigger",
                                                         "can_support", "caption",
                                                         "reasons")}}
                      for w, a in zip(ws[:max_words], assessed[:max_words], strict=True)],
            "reliability": evidence_reliability.summary(assessed),
            "segments": [{"id": f"seg:{tr.id}:{s.idx}", "start_ms": s.start_ms,
                          "end_ms": s.end_ms, "text": s.text}
                         for s in segs[:MAX_LIST_ITEMS]],
            "segments_truncated": len(segs) > MAX_LIST_ITEMS,
            "speech": _speech(ws, start_ms, end_ms),
        }

    events: list[dict] = []
    for kind, prefix in ((SignalKind.AUDIO, "a"), (SignalKind.VISUAL, "v")):
        run = signals.current(session, recording_id, kind)
        if run is None:
            reason = "no audio track" if kind == SignalKind.AUDIO and \
                recording.has_audio is False else "not measured yet"
            bundle[kind.value] = {"available": False, "reason": reason}
            continue
        ser = signals.series(run)
        entry: dict = {"available": True, "run_id": run.id,
                       "config_fingerprint": run.config_fingerprint}
        if kind == SignalKind.AUDIO:
            origin = ser.get("start_ms", 0)
            entry["rms_db"] = _pool(ser.get("rms_db", []), ser["step_ms"], origin,
                                    start_ms, end_ms, max_points)
            entry["peak_db"] = _pool(ser.get("peak_db", []), ser["step_ms"], origin,
                                     start_ms, end_ms, max_points)
            entry["silences_ms"] = [s for s in ser.get("silences_ms", [])
                                    if s[1] > start_ms and s[0] < end_ms]
        else:
            entry["motion_mafd"] = _pool(ser.get("motion_mafd", []), ser["bin_ms"], 0,
                                         start_ms, end_ms, max_points)
            scenes = [{"id": f"sc:{run.id}:{s['idx']}", **{k: s[k] for k in (
                "start_ms", "end_ms", "duration_ms")}} for s in ser.get("scenes", [])
                if s["end_ms"] > start_ms and s["start_ms"] < end_ms]
            kfs = [{"id": f"kf:{k.id}", "pts_ms": k.pts_ms,
                    "scene_id": f"sc:{run.id}:{k.scene_idx}"}
                   for k in signals.run_keyframes(session, run.id)
                   if start_ms <= k.pts_ms < end_ms]
            entry["scenes"], entry["keyframes"] = (scenes[:MAX_LIST_ITEMS],
                                                   kfs[:MAX_LIST_ITEMS])
            entry["truncated"] = len(scenes) > MAX_LIST_ITEMS or len(kfs) > MAX_LIST_ITEMS
            entry["variable_frame_rate_detected"] = ser.get("vfr_detected")
        bundle[kind.value] = entry
        for i, e in enumerate(signals.events(run)):
            if e["end_ms"] >= start_ms and e["start_ms"] < end_ms:
                events.append({"id": f"{prefix}:{run.id}:{i}", **e})
    events.sort(key=lambda e: (e["start_ms"], e["id"]))
    bundle["events"] = events[:max_events]
    bundle["events_truncated"] = len(events) > max_events
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str)
    bundle["fingerprint"] = hashlib.sha256(canonical.encode()).hexdigest()
    return bundle


def resolve(session: Session, item_id: str) -> dict:
    """Resolve an evidence id back to its stored row (M6 checks model references with it)."""
    from app.models import Keyframe, SignalRun, TranscriptWord

    kind, _, rest = item_id.partition(":")
    try:
        if kind == "w":
            tid, idx = rest.rsplit(":", 1)
            w = session.get(TranscriptWord, (tid, int(idx)))
            if w:
                return {"id": item_id, "start_ms": w.start_ms, "end_ms": w.end_ms,
                        "text": w.text}
        elif kind == "seg":
            tid, idx = rest.rsplit(":", 1)
            s = session.get(TranscriptSegment, (tid, int(idx)))
            if s:
                return {"id": item_id, "start_ms": s.start_ms, "end_ms": s.end_ms,
                        "text": s.text}
        elif kind in ("a", "v", "sc"):
            rid, idx = rest.rsplit(":", 1)
            run = session.get(SignalRun, rid)
            if run and run.status == "COMPLETE":
                items = signals.series(run).get("scenes", []) if kind == "sc" \
                    else signals.events(run)
                i = int(idx)
                if 0 <= i < len(items):
                    return {"id": item_id, **items[i]}
        elif kind == "kf":
            k = session.get(Keyframe, rest)
            if k:
                return {"id": item_id, "pts_ms": k.pts_ms, "scene_idx": k.scene_idx}
    except ValueError:
        pass
    raise NotFoundError(f"Unknown evidence id: {item_id}")
