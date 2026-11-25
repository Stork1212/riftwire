"""M4 audio and visual signals (docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md §5, §6).

Everything here is **measured** from the footage with FFmpeg, except the events marked
``"kind": "inferred"``. Those are heuristics with their method and thresholds attached, and
none of them is a claim about what happens on screen (no "gunshot", no "exciting moment").

* **Audio:** one FFmpeg pass. ``astats`` per 100 ms window gives RMS and peak level (dBFS).
  Silence spans and onsets (a rise of at least ``onset_db`` within ``onset_window_ms``) are
  derived from that series. The inferred event is ``loud_burst``.
* **Visual:** one FFmpeg pass over a small grayscale proxy. ``scdet`` reports every
  decoded frame with its **presentation timestamp** (variable frame rate safe), its mean
  absolute frame difference (motion) and its scene score. Measured: a binned motion series,
  scene cuts, scenes and cut bursts. Inferred: ``potential_camera_change`` (a cut with
  little motion on either side). Keyframes: one JPEG per scene at the scene's middle,
  capped per hour of footage.

Series are compressed JSON in ``signal_runs.series_gz``; the run, its events and its
keyframes are immutable once complete. A new configuration gives a new run beside the old.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, PermanentJobError, TransientJobError
from app.db.base import utcnow
from app.models import (
    Job,
    JobStatus,
    JobType,
    Keyframe,
    Recording,
    RecordingStatus,
    SignalKind,
    SignalRun,
    SignalStatus,
)

AUDIO_CONFIG = {
    "version": 1, "method": "ffmpeg astats", "sample_rate": 8000, "window_ms": 100,
    "silence_db": -50.0, "silence_min_ms": 500, "onset_db": 12.0, "onset_window_ms": 500,
    "loud_burst_db": -20.0, "loud_burst_percentile": 95, "floor_db": -120.0,
}
VISUAL_CONFIG = {
    "version": 1, "method": "ffmpeg scdet", "proxy_width": 160, "scdet_threshold": 10.0,
    "bin_ms": 250, "burst_cuts": 3, "burst_window_ms": 2000, "calm_mafd": 2.0,
    "calm_window_ms": 1000, "keyframe_width": 480, "keyframe_height": 854,
    "keyframes_per_hour": 60, "keyframes_min": 4,
}
KEYFRAME_RESERVE_BYTES = 300_000
JOB_FOR = {SignalKind.AUDIO: JobType.AUDIO_SIGNALS, SignalKind.VISUAL: JobType.VISUAL_SIGNALS}
KIND_FOR = {v: k for k, v in JOB_FOR.items()}
_FRAME = re.compile(r"^frame:\s*\d+\s+pts:\s*(?:-?\d+|NOPTS)\s+pts_time:\s*(-?[\d.]+|NOPTS)")
_UNSAFE_PATH = re.compile(r"[\\':,;\[\]=]")
_KV = re.compile(r"^lavfi\.([\w.]+)=(.*)$")


def config(kind: SignalKind) -> dict:
    return dict(AUDIO_CONFIG if kind == SignalKind.AUDIO else VISUAL_CONFIG)


def fingerprint(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


# ------------------------------------------------------------------- FFmpeg metadata
def iter_metadata(lines, offset_s: float = 0.0):
    """Stream ``metadata=print`` / ``ametadata=print`` lines -> ``(pts_seconds, {key:
    value})`` per frame, in decode order, minus ``offset_s`` (0 for FFmpeg's own output: it
    already starts each input at 0 unless ``-copyts`` is given, which RIFTWIRE never passes).
    A frame without a timestamp (``NOPTS``) is skipped with its values."""
    t: float | None = None
    kv: dict[str, str] = {}
    have = False
    for line in lines:
        m = _FRAME.match(line)
        if m:
            if have and t is not None:
                yield t, kv
            have, kv = True, {}
            t = None if m.group(1) == "NOPTS" else float(m.group(1)) - offset_s
            continue
        x = _KV.match(line.strip())
        if x and have:
            kv[x.group(1)] = x.group(2)
    if have and t is not None:
        yield t, kv


def parse_metadata(text: str, offset_s: float = 0.0) -> list[tuple[float, dict[str, str]]]:
    return list(iter_metadata(text.splitlines(), offset_s))


def _num(value: str | None, floor: float) -> float:
    try:
        v = float(value) if value is not None else floor
    except ValueError:
        return floor
    return floor if math.isnan(v) or v < floor else round(v, 2)


# --------------------------------------------------------------------------- audio
def analyse_audio(frames: list[tuple[float, dict[str, str]]], duration_ms: int,
                  cfg: dict = AUDIO_CONFIG) -> tuple[dict, list[dict]]:
    """Series and events from per-window ``astats`` metadata."""
    step, floor = cfg["window_ms"], cfg["floor_db"]
    rms = [_num(kv.get("astats.Overall.RMS_level"), floor) for _, kv in frames]
    peak = [_num(kv.get("astats.Overall.Peak_level"), floor) for _, kv in frames]
    starts = [int(round(t * 1000)) for t, _ in frames]
    silences: list[list[int]] = []
    run_start: int | None = None
    for i in range(len(rms) + 1):
        quiet = i < len(rms) and rms[i] < cfg["silence_db"]
        if quiet and run_start is None:
            run_start = starts[i]
        elif not quiet and run_start is not None:
            end = starts[i] if i < len(starts) else min(duration_ms, starts[-1] + step)
            if end - run_start >= cfg["silence_min_ms"]:
                silences.append([run_start, end])
            run_start = None
    back = max(1, cfg["onset_window_ms"] // step)
    onsets: list[int] = []
    for i in range(1, len(rms)):
        before = min(rms[max(0, i - back):i])
        rising = rms[i] - before >= cfg["onset_db"] and rms[i] >= cfg["silence_db"]
        if rising and (not onsets or starts[i] - onsets[-1] >= cfg["onset_window_ms"]):
            onsets.append(starts[i])
    series = {"step_ms": step, "start_ms": starts[0] if starts else 0, "t_ms": starts,
              "rms_db": rms, "peak_db": peak, "silences_ms": silences, "onsets_ms": onsets}
    events: list[dict] = [{"type": "onset", "kind": "measured", "start_ms": t,
                           "end_ms": t + step, "method": "rms rise",
                           "thresholds": {"onset_db": cfg["onset_db"],
                                          "onset_window_ms": cfg["onset_window_ms"]}}
                          for t in onsets]
    audible = sorted(v for v in rms if v > floor)
    if audible:
        pct = audible[min(len(audible) - 1,
                          math.ceil(cfg["loud_burst_percentile"] / 100 * len(audible)) - 1)]
        level = max(cfg["loud_burst_db"], pct)
        i = 0
        while i < len(rms):
            if rms[i] >= level:
                j = i
                while j + 1 < len(rms) and rms[j + 1] >= level:
                    j += 1
                top = max(rms[i:j + 1])
                events.append({
                    "type": "loud_burst", "kind": "inferred", "start_ms": starts[i],
                    "end_ms": starts[j] + step, "peak_rms_db": top,
                    "method": "rms_db >= max(loud_burst_db, p95 of audible windows)",
                    "thresholds": {"level_db": round(level, 2)},
                    "confidence": round(min(1.0, 0.5 + (top - level) / 20), 2),
                    "note": "loudness only; not a claim about what made the sound"})
                i = j + 1
            else:
                i += 1
    events.sort(key=lambda e: (e["start_ms"], e["type"]))
    return series, events


# -------------------------------------------------------------------------- visual
def analyse_visual(frames, duration_ms: int, cfg: dict = VISUAL_CONFIG
                   ) -> tuple[dict, list[dict], list[dict]]:
    """Series, events and scenes from per-frame ``scdet`` metadata (real frame times).
    ``frames`` may be any iterable (streamed from a file): only compact arrays are kept
    (a 3-hour 60 fps recording is ~650,000 frames)."""
    pts, mafd, score = array("q"), array("d"), array("d")
    cuts: list[int] = []
    for t, kv in frames:
        ms = int(round(t * 1000))
        pts.append(ms)
        mafd.append(_num(kv.get("scd.mafd"), 0.0))
        score.append(_num(kv.get("scd.score"), 0.0))
        if "scd.time" in kv:
            cuts.append(ms)
    bin_ms = cfg["bin_ms"]
    n_bins = max(1, math.ceil(duration_ms / bin_ms))
    sums, counts, peaks = [0.0] * n_bins, [0] * n_bins, [0.0] * n_bins
    for t, m, s in zip(pts, mafd, score, strict=True):
        b = min(n_bins - 1, max(0, t // bin_ms))
        sums[b] += m
        counts[b] += 1
        peaks[b] = max(peaks[b], s)
    motion = [round(sums[i] / counts[i], 2) if counts[i] else None for i in range(n_bins)]
    bounds = [0, *[c for c in cuts if 0 < c < duration_ms], duration_ms]
    scenes = [{"idx": i, "start_ms": a, "end_ms": b, "duration_ms": b - a}
              for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False)) if b > a]
    events: list[dict] = [{"type": "scene_cut", "kind": "measured", "start_ms": c,
                           "end_ms": c, "method": "scdet",
                           "thresholds": {"scdet_threshold": cfg["scdet_threshold"]}}
                          for c in cuts]
    i = 0
    while i < len(cuts):
        j = i
        while j + 1 < len(cuts) and cuts[j + 1] - cuts[i] <= cfg["burst_window_ms"]:
            j += 1
        if j - i + 1 >= cfg["burst_cuts"]:
            events.append({"type": "cut_burst", "kind": "measured", "start_ms": cuts[i],
                           "end_ms": cuts[j], "cuts": j - i + 1, "method": "scene cuts",
                           "thresholds": {"burst_cuts": cfg["burst_cuts"],
                                          "burst_window_ms": cfg["burst_window_ms"]}})
            i = j + 1
        else:
            i += 1
    w = cfg["calm_window_ms"]
    for c in cuts:                       # bisect: O(cuts x frames in the window)
        before = mafd[bisect_left(pts, c - w):bisect_left(pts, c)]
        after = mafd[bisect_right(pts, c):bisect_right(pts, c + w)]
        if before and after and max(before) <= cfg["calm_mafd"] and max(after) <= cfg["calm_mafd"]:
            events.append({"type": "potential_camera_change", "kind": "inferred",
                           "start_ms": c, "end_ms": c, "method": "scene cut with low motion "
                           "before and after", "thresholds": {"calm_mafd": cfg["calm_mafd"],
                                                             "calm_window_ms": w},
                           "confidence": 0.5,
                           "note": "a cut between calm shots; not a claim about the content"})
    events.sort(key=lambda e: (e["start_ms"], e["type"]))
    series = {"bin_ms": bin_ms, "motion_mafd": motion, "max_scene_score": peaks,
              "cuts_ms": cuts, "scenes": scenes, "frames": len(pts),
              "vfr_detected": _vfr(pts)}
    return series, events, scenes


def _vfr(pts) -> bool:
    lo = hi = None
    for a, b in zip(pts, pts[1:], strict=False):
        if b > a:
            g = b - a
            lo = g if lo is None else min(lo, g)
            hi = g if hi is None else max(hi, g)
    return lo is not None and hi - lo > 2


def keyframe_plan(scenes: list[dict], duration_ms: int, cfg: dict = VISUAL_CONFIG) -> list[dict]:
    """One keyframe per scene at its middle; when over the cap, the longest scenes win."""
    cap = max(cfg["keyframes_min"], math.ceil(cfg["keyframes_per_hour"] * duration_ms / 3_600_000))
    chosen = sorted(scenes, key=lambda s: (-s["duration_ms"], s["start_ms"]))[:cap]
    return [{"scene_idx": s["idx"], "pts_ms": s["start_ms"] + s["duration_ms"] // 2}
            for s in sorted(chosen, key=lambda s: s["start_ms"])]


# ------------------------------------------------------------------------- runs
def _sink(meta_file: Path | None) -> str:
    """Where ``metadata=print`` writes: a file when its path is safe inside a filtergraph
    (no escaping surprises), otherwise stdout."""
    if meta_file is None or _UNSAFE_PATH.search(str(meta_file)):
        return "-"
    return str(meta_file)


def audio_command(ffmpeg: str, source: Path, cfg: dict = AUDIO_CONFIG,
                  meta_file: Path | None = None) -> list[str]:
    n = cfg["sample_rate"] * cfg["window_ms"] // 1000
    return [ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-vn", "-map", "0:a:0",
            "-af", f"aresample={cfg['sample_rate']},pan=mono|c0=c0,asetnsamples=n={n}:p=0,"
                   "astats=metadata=1:reset=1:measure_overall=RMS_level+Peak_level:"
                   f"measure_perchannel=none,ametadata=print:file={_sink(meta_file)}",
            "-f", "null", "-"]


def visual_command(ffmpeg: str, source: Path, cfg: dict = VISUAL_CONFIG,
                   meta_file: Path | None = None) -> list[str]:
    return [ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-an", "-map", "0:v:0",
            "-fps_mode", "passthrough",
            "-vf", f"scale={cfg['proxy_width']}:-2,format=gray,"
                   f"scdet=threshold={cfg['scdet_threshold']},"
                   f"metadata=print:file={_sink(meta_file)}",
            "-f", "null", "-"]


def start_offset_s(ffprobe: str, source: Path, run) -> float:
    """The container's start time, recorded for reference only. FFmpeg (without
    ``-copyts``) already removes it before the metadata filters run, so signal times are
    relative to the recording's start like transcript chunks and keyframe seeks; subtracting
    it again would shift every signal early (Codex review of 0be24ef, verified)."""
    r = run([ffprobe, "-v", "error", "-show_entries", "format=start_time", "-of",
             "default=nw=1:nk=1", str(source)], 60)
    out = getattr(r, "stdout", b"")
    out = out.decode(errors="replace") if isinstance(out, bytes) else (out or "")
    try:
        v = float(out.strip().splitlines()[0])
        return v if math.isfinite(v) else 0.0
    except (ValueError, IndexError):
        return 0.0


def measure(kind: SignalKind, ffmpeg: str, source: Path, duration_ms: int,
            run: Callable[[list[str], float], object], timeout: float, *,
            work: Path | None = None, ffprobe: str = "ffprobe"
            ) -> tuple[dict, list[dict], list[dict]]:
    """One FFmpeg pass. With ``work`` (a temporary folder), the per-frame metadata goes to a
    file there and is parsed as a stream, so memory does not grow with the recording."""
    offset = start_offset_s(ffprobe, source, run)
    meta = (work / f"{kind.value}-metadata.txt") if work is not None else None
    build = audio_command if kind == SignalKind.AUDIO else visual_command
    cmd = build(ffmpeg, source, meta_file=meta)
    to_file = meta is not None and _sink(meta) != "-"
    r = run(cmd, timeout)
    if getattr(r, "returncode", 1):
        err = getattr(r, "stderr", "") or ""
        err = err.decode(errors="replace") if isinstance(err, bytes) else err
        raise TransientJobError(f"FFmpeg {kind.value} analysis failed: {err[-300:]}")
    if to_file:
        if not meta.is_file():
            raise TransientJobError(f"FFmpeg wrote no {kind.value} metadata")
        fh = meta.open(encoding="utf-8", errors="replace")
        lines = fh
    else:
        out = getattr(r, "stdout", b"")
        fh = None
        lines = (out.decode("utf-8", "replace") if isinstance(out, bytes) else out).splitlines()
    try:
        frames = iter_metadata(lines)              # already relative to the start
        if kind == SignalKind.AUDIO:
            frames = list(frames)                 # ~10 rows per second: small
            if not frames:
                raise PermanentJobError("FFmpeg reported no audio frames for this recording")
            series, events = analyse_audio(frames, duration_ms)
            series["container_start_s"] = offset    # reference only; not applied
            return series, events, []
        series, events, scenes = analyse_visual(frames, duration_ms)
        if not series["frames"]:
            raise PermanentJobError("FFmpeg reported no video frames for this recording")
        series["container_start_s"] = offset        # reference only; not applied
        return series, events, scenes
    finally:
        if fh is not None:
            fh.close()
        if meta is not None:
            meta.unlink(missing_ok=True)


def prepare(session: Session, recording: Recording, kind: SignalKind, job_id: str) -> SignalRun:
    cfg = config(kind)
    fp = fingerprint(cfg)
    job = session.get(Job, job_id)
    if job is not None:                     # which configuration this attempt works on
        job.result_json = json.dumps({"config_fingerprint": fp})
    run = session.scalar(select(SignalRun).where(
        SignalRun.recording_id == recording.id, SignalRun.kind == kind.value,
        SignalRun.config_fingerprint == fp))
    if run is None:
        run = SignalRun(recording_id=recording.id, kind=kind.value, config_fingerprint=fp,
                        config_json=json.dumps(cfg, sort_keys=True),
                        recording_sha256=recording.sha256, duration_ms=recording.duration_ms,
                        job_id=job_id)
        session.add(run)
        session.flush()
    return run


def finish(session: Session, run_id: str, series: dict, events: list[dict],
           keyframes: list[dict]) -> SignalRun:
    run = session.get(SignalRun, run_id)
    if run.status == SignalStatus.COMPLETE.value:
        return run
    for i, k in enumerate(keyframes):
        session.add(Keyframe(signal_run_id=run.id, idx=i, pts_ms=k["pts_ms"],
                             scene_idx=k["scene_idx"], stored_filename=k["stored_filename"],
                             width=k["width"], height=k["height"], sha256=k["sha256"]))
    run.series_gz = gzip.compress(json.dumps(series, sort_keys=True,
                                             separators=(",", ":")).encode(), mtime=0)
    run.events_json = json.dumps(events, sort_keys=True)
    run.status = SignalStatus.COMPLETE.value
    run.completed_at = utcnow()
    session.flush()
    return run


# ---------------------------------------------------------------------- reading
def current(session: Session, recording_id: str, kind: SignalKind) -> SignalRun | None:
    return session.scalar(select(SignalRun).where(
        SignalRun.recording_id == recording_id, SignalRun.kind == kind.value,
        SignalRun.config_fingerprint == fingerprint(config(kind)),
        SignalRun.status == SignalStatus.COMPLETE.value))


def failed_for_current_config(session: Session, recording_id: str,
                              kind: SignalKind) -> str | None:
    """The error of this kind's signal job if it failed for the current configuration."""
    job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                           Job.job_type == JOB_FOR[kind].value))
    if job is None or job.status != JobStatus.FAILED.value:
        return None
    ran = json.loads(job.result_json or "{}").get("config_fingerprint")
    if ran is not None and ran != fingerprint(config(kind)):
        return None
    return job.last_error or f"{kind.value} signal measurement failed"


def series(run: SignalRun) -> dict:
    return json.loads(gzip.decompress(run.series_gz)) if run.series_gz else {}


def events(run: SignalRun) -> list[dict]:
    return json.loads(run.events_json) if run.events_json else []


def request(session: Session, settings, recording_id: str, kind: SignalKind) -> Job | None:
    """Queue (or re-arm after a configuration change) the signal job; ``None`` when the
    current-configuration run already exists."""
    from app.services import queue

    if current(session, recording_id, kind) is not None:
        return None
    jt = JOB_FOR[kind]
    job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                           Job.job_type == jt.value))
    if job is None:
        return queue.enqueue(session, recording_id, jt, settings.job_max_attempts)
    if job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
        queue.rearm(session, job)
    return job


def describe(session: Session, settings, recording_id: str) -> dict:
    """``GET .../signals``: both kinds, their jobs, and summaries (no large series)."""
    from app.services import rights

    recording = session.get(Recording, recording_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    out = {}
    for kind in SignalKind:
        job = session.scalar(select(Job).where(Job.recording_id == recording_id,
                                               Job.job_type == JOB_FOR[kind].value))
        run = current(session, recording_id, kind)
        entry: dict = {"job": None if job is None else {
            "id": job.id, "status": job.status, "progress_percent": job.progress_percent,
            "last_error": job.last_error}, "run": None}
        if run is not None:
            ev = events(run)
            entry["run"] = {
                "id": run.id, "config_fingerprint": run.config_fingerprint,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "measured_events": sum(1 for e in ev if e["kind"] == "measured"),
                "inferred_events": sum(1 for e in ev if e["kind"] == "inferred"),
                "keyframes": len(run_keyframes(session, run.id)),
            }
            if kind == SignalKind.VISUAL:
                entry["run"]["scenes"] = len(series(run).get("scenes", []))
        out[kind.value] = entry
    return out


def request_checked(session: Session, settings, recording_id: str) -> dict:
    from app.services import rights

    recording = session.get(Recording, recording_id)
    if recording is None:
        raise NotFoundError("Recording not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    if recording.status != RecordingStatus.READY.value or not recording.duration_ms:
        raise ConflictError("Wait for the recording to finish processing.")
    queued = {}
    for kind in SignalKind:
        if kind == SignalKind.AUDIO and recording.has_audio is False:
            queued[kind.value] = False
            continue
        queued[kind.value] = request(session, settings, recording_id, kind) is not None
    session.flush()
    return {"queued": queued}


def run_keyframes(session: Session, run_id: str) -> list[Keyframe]:
    return list(session.scalars(select(Keyframe).where(Keyframe.signal_run_id == run_id)
                                .order_by(Keyframe.idx)))



def keyframe_file(session: Session, recording_id: str, keyframe_id: str) -> str:
    """The stored (generated) file name of a keyframe of this recording, after the rights
    check. Never an absolute path."""
    from app.services import rights

    kf = session.get(Keyframe, keyframe_id)
    run = session.get(SignalRun, kf.signal_run_id) if kf else None
    if kf is None or run is None or run.recording_id != recording_id:
        raise NotFoundError("Keyframe not found")
    decision = rights.evaluate_recording(session, recording_id, "analyze")
    if not decision.allowed:
        raise decision.refuse()
    return kf.stored_filename
