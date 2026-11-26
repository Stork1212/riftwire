"""Real-footage evaluation of the highlight workflow (protocol: docs/REAL_FOOTAGE_EVAL.md).

Usage (from backend/, with backend/.venv/bin/python, an Ollama server already running on
127.0.0.1 with qwen3.5:4b; nothing is downloaded):

    python ../scripts/real_eval.py MANIFEST.json DATA_DIR OUT_DIR WHISPER_PYTHON \
        WHISPER_MODEL OUTRO_MP4 [--validate-only] [--render-top N]

* ``MANIFEST.json``: the owner's recordings, authorization statements and marked spans
  (``scripts/real_eval_manifest.example.json``). Outside Git.
* ``DATA_DIR``: an isolated RIFTWIRE data folder for the evaluation (imported copies
  live here); ``OUT_DIR``: results, reports and rendered Shorts. Both outside Git.

The baseline runs the current settings unchanged. Matching marks to highlights follows the
pre-registered rule in the protocol (:func:`match`); nothing here is tuned on the marks.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

GIB = 1024**3
FLOOR_BYTES = 60 * GIB
MATCH_OVERLAP = 0.5
_TIME = re.compile(r"^(?:(\d{1,3}):)?([0-5]?\d):([0-5]\d)(?:\.(\d{1,3}))?$")
BASES = {"ownership", "creator_permission", "licence"}


# ------------------------------------------------------------------------- manifest
def parse_time(value) -> int:
    """Seconds (number) or ``[H:]MM:SS[.mmm]`` -> milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < 0 or math.isnan(value):
            raise ValueError(f"negative or invalid time {value!r}")
        return int(round(value * 1000))
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):          # "75" or "75.5": seconds as text
        return int(round(float(text) * 1000))
    m = _TIME.match(text)
    if not m:
        raise ValueError(f"unreadable time {value!r} (use seconds or HH:MM:SS.mmm)")
    h, mi, s, frac = m.groups()
    ms = int((frac or "0").ljust(3, "0"))
    return ((int(h or 0) * 60 + int(mi)) * 60 + int(s)) * 1000 + ms


@dataclass
class Mark:
    start_ms: int
    end_ms: int
    label: str = ""


@dataclass
class Item:
    path: Path
    authorization: str
    basis: str
    game: str
    channel: str
    request: int
    marks: list[Mark] = field(default_factory=list)
    name: str = ""


def load_manifest(path: Path) -> tuple[list[Item], list[str]]:
    """Parse and validate the manifest. Returns items and every problem found; a recording
    with any problem is not run (no partial authorization)."""
    errors: list[str] = []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [], [f"manifest unreadable: {e}"]
    items: list[Item] = []
    recs = data.get("recordings") if isinstance(data, dict) else None
    if not isinstance(recs, list) or not recs:
        return [], ["manifest needs a non-empty 'recordings' list"]
    for n, r in enumerate(recs, 1):
        where = f"recording {n}"
        bad: list[str] = []
        if not isinstance(r, dict):
            errors.append(f"{where}: not an object")
            continue
        p = Path(str(r.get("path", ""))).expanduser()
        if not p.is_absolute() or not p.is_file():
            bad.append(f"path {str(p)!r} is not an existing absolute file path")
        if r.get("authorized") is not True:
            bad.append("'authorized' must be true (the owner confirms this footage may be "
                       "used for this evaluation)")
        statement = str(r.get("authorization") or "").strip()
        if len(statement) < 10:
            bad.append("'authorization' needs the owner's statement (at least 10 characters)")
        basis = str(r.get("basis") or "ownership")
        if basis not in BASES:
            bad.append(f"'basis' must be one of {sorted(BASES)} (public availability is "
                       "not a basis)")
        request = r.get("request", 5)
        if not isinstance(request, int) or isinstance(request, bool) or not 1 <= request <= 20:
            bad.append("'request' must be an integer from 1 to 20")
        marks: list[Mark] = []
        for k, m in enumerate(r.get("marked") or [], 1):
            try:
                a, b = parse_time(m["start"]), parse_time(m["end"])
            except (KeyError, TypeError, ValueError) as e:
                bad.append(f"mark {k}: {e}")
                continue
            if b <= a:
                bad.append(f"mark {k}: end is not after start")
                continue
            marks.append(Mark(a, b, str(m.get("label") or "")))
        if not marks:
            bad.append("'marked' needs at least one span the owner would clip")
        marks.sort(key=lambda m: m.start_ms)
        for x, y in zip(marks, marks[1:], strict=False):
            if y.start_ms < x.end_ms:
                bad.append(f"marks overlap ({x.label or x.start_ms} and {y.label or y.start_ms});"
                           " merge them or mark them separately without overlap")
        if bad:
            errors += [f"{where}: {b}" for b in bad]
            continue
        items.append(Item(p, statement, basis, str(r.get("game") or "gta"),
                          str(r.get("channel") or "rumbleframe"), request, marks,
                          str(r.get("name") or p.stem)))
    return items, errors


# ------------------------------------------------------------------------- matching
def overlap(a: tuple[int, int], b: tuple[int, int]) -> tuple[float, float]:
    """(overlap coefficient, IoU) of two spans."""
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = max(1, min(a[1] - a[0], b[1] - b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / shorter, (inter / union if union > 0 else 0.0)


def match(highlights: list[dict], marks: list[Mark], *, windows: list[dict] | None = None,
          candidates: list[dict] | None = None) -> dict:
    """The pre-registered rule (protocol §2). ``highlights``: selected, with ``start_ms``,
    ``end_ms``, ``rank``; ``windows``: planned windows; ``candidates``: every validated
    candidate (selected and rejected) with ``status`` and ``reason``."""
    pairs = []
    for i, h in enumerate(highlights):
        for j, m in enumerate(marks):
            coef, iou = overlap((h["start_ms"], h["end_ms"]), (m.start_ms, m.end_ms))
            if coef > 0:
                pairs.append((coef, iou, -min(h["start_ms"], m.start_ms), i, j))
    pairs.sort(reverse=True)
    used_h, used_m, matched, near = set(), set(), [], []
    for coef, iou, _, i, j in pairs:
        if i in used_h or j in used_m:
            continue
        if coef >= MATCH_OVERLAP:
            used_h.add(i)
            used_m.add(j)
            h, m = highlights[i], marks[j]
            matched.append({
                "mark": j, "label": m.label, "highlight_rank": h.get("rank"),
                "overlap": round(coef, 3), "iou": round(iou, 3),
                "start_delta_s": round((h["start_ms"] - m.start_ms) / 1000, 2),
                "end_delta_s": round((h["end_ms"] - m.end_ms) / 1000, 2)})
    seen = set()
    for coef, iou, _, i, j in pairs:          # best partial overlap of each leftover
        if coef >= MATCH_OVERLAP or (i in used_h and j in used_m) or (i, j) in seen:
            continue
        if i not in used_h and any(x["highlight_rank"] == highlights[i].get("rank")
                                   for x in near):
            continue
        seen.add((i, j))
        near.append({"mark": j, "highlight_rank": highlights[i].get("rank"),
                     "overlap": round(coef, 3), "iou": round(iou, 3),
                     "mark_found_by_another": j in used_m})
    missed = []
    for j, m in enumerate(marks):
        if j in used_m:
            continue
        span = (m.start_ms, m.end_ms)
        why, detail = "window", "no candidate window covered at least half of the mark"
        covering = [w for w in (windows or [])
                    if max(0, min(w["end_ms"], span[1]) - max(w["start_ms"], span[0]))
                    >= 0.5 * (span[1] - span[0])]
        if covering:
            why, detail = "model", ("windows " + ", ".join(w["id"] for w in covering)
                                    + " covered it; the model selected no candidate there")
            for c in candidates or []:
                if c.get("status") == "rejected" and overlap(
                        (c["start_ms"], c["end_ms"]), span)[0] >= MATCH_OVERLAP:
                    why, detail = "ranking", (f"a validated candidate matched it but was not "
                                              f"selected: {c.get('reason')}")
                    break
        missed.append({"mark": j, "label": m.label, "start_s": m.start_ms / 1000,
                       "end_s": m.end_ms / 1000, "diagnosis": why, "detail": detail})
    extras = [{"highlight_rank": h.get("rank"), "start_s": h["start_ms"] / 1000,
               "end_s": h["end_ms"] / 1000, "title": h.get("title")}
              for i, h in enumerate(highlights) if i not in used_h]
    abs_start = sorted(abs(x["start_delta_s"]) for x in matched)
    abs_end = sorted(abs(x["end_delta_s"]) for x in matched)
    return {
        "marked": len(marks), "returned": len(highlights), "found": len(matched),
        "missed": missed, "extras": extras, "near_misses": near, "matched": matched,
        "recall": f"{len(matched)}/{len(marks)}", "precision": f"{len(matched)}/{len(highlights)}",
        "median_abs_start_delta_s": _median(abs_start),
        "median_abs_end_delta_s": _median(abs_end),
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 2)


# --------------------------------------------------------------------- media checks
def _run(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def check_short(short: Path, outro: Path, clip_s: float, words: list[dict],
                sheet: Path) -> dict:
    probe = json.loads(_run(["ffprobe", "-v", "error", "-show_entries",
                             "stream=codec_type,codec_name,width,height,r_frame_rate:"
                             "format=duration", "-of", "json", str(short)]).stdout or "{}")
    video = next((s for s in probe.get("streams", []) if s["codec_type"] == "video"), {})
    audio = next((s for s in probe.get("streams", []) if s["codec_type"] == "audio"), None)
    dur = float(probe.get("format", {}).get("duration", 0))
    out: dict = {"width": video.get("width"), "height": video.get("height"),
                 "fps": video.get("r_frame_rate"), "duration_s": round(dur, 3),
                 "expected_s": round(clip_s + 2.5, 3), "audio": bool(audio)}
    r = _run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(short), "-vn", "-af",
              "ebur128=peak=true", "-f", "null", "-"])
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    peak = re.findall(r"Peak:\s+(-?[\d.inf]+) dBFS", r.stderr)
    out["integrated_lufs"] = float(lufs[-1]) if lufs else None
    out["true_peak_dbfs"] = float(peak[-1]) if peak and peak[-1] != "-inf" else None
    start = max(0.0, dur - 2.5)
    r = _run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{start:.3f}", "-i", str(short),
              "-i", str(outro), "-lavfi",
              "[0:v]trim=duration=2.3,setpts=PTS-STARTPTS,scale=540:960[a];"
              "[1:v]trim=duration=2.3,setpts=PTS-STARTPTS,scale=540:960[b];[a][b]psnr",
              "-f", "null", "-"])
    psnr = re.findall(r"average:([\d.]+|inf)", r.stderr)
    out["outro_psnr_db"] = (float("inf") if psnr and psnr[-1] == "inf"
                            else float(psnr[-1]) if psnr else None)
    picks = words[:: max(1, len(words) // 6)][:6] if words else []
    frames = []
    for n, w in enumerate(picks):
        t = (w["start_ms"] + w["end_ms"]) / 2000
        f = sheet.with_name(f"{sheet.stem}-{n}.png")
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(short),
              "-frames:v", "1", "-vf", "scale=270:480", str(f)])
        if f.is_file():
            frames.append((f, w))
    if frames:
        inputs = sum((["-i", str(f)] for f, _ in frames), [])
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, "-filter_complex",
              f"hstack=inputs={len(frames)}" if len(frames) > 1 else "null", str(sheet)])
        sheet.with_suffix(".txt").write_text("\n".join(
            f"frame {n + 1}: t={(w['start_ms'] + w['end_ms']) / 2000:.2f}s expected word "
            f"{w['w']!r}" for n, (_, w) in enumerate(frames)) + "\n")
        for f, _ in frames:
            f.unlink(missing_ok=True)
    out["checks"] = {
        "1080x1920": (out["width"], out["height"]) == (1080, 1920),
        "30fps": out["fps"] == "30/1",
        "duration_is_clip_plus_outro": abs(dur - (clip_s + 2.5)) < 0.3,
        "audio_present": out["audio"],
        "no_clipping": out["true_peak_dbfs"] is None or out["true_peak_dbfs"] < 0.0,
        "outro_matches_asset": out["outro_psnr_db"] is not None and out["outro_psnr_db"] > 25,
    }
    return out


# ------------------------------------------------------------------------- pipeline
def main(argv: list[str]) -> int:  # noqa: C901 - a linear script
    args = [a for a in argv if not a.startswith("--")]
    validate_only = "--validate-only" in argv
    render_top = 1
    for a in argv:
        if a.startswith("--render-top="):
            render_top = max(0, int(a.split("=", 1)[1]))
    manifest, data_dir, out_dir, whisper_py, whisper_model, outro = (
        Path(args[0]), Path(args[1]), Path(args[2]), args[3], args[4], Path(args[5]))
    items, errors = load_manifest(manifest)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest-check.json").write_text(json.dumps({
        "recordings_ok": [i.name for i in items], "errors": errors}, indent=1))
    for e in errors:
        print(f"MANIFEST  {e}", flush=True)
    if validate_only or not items:
        print(f"manifest: {len(items)} recording(s) ready, {len(errors)} problem(s)")
        return 0 if items and not errors else 1
    if errors:
        print("refusing to run while the manifest has problems (fix them first)")
        return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    need = sum(i.path.stat().st_size for i in items)
    free = shutil.disk_usage(data_dir).free
    if free - 2 * need < FLOOR_BYTES:
        print(f"refusing: {free / GIB:.1f} GiB free on the data volume; importing "
              f"{need / GIB:.1f} GiB (x2 working margin) would go below the 60 GiB floor")
        return 2
    for k in [k for k in os.environ if k.startswith("RIFTWIRE_")]:
        del os.environ[k]
    os.environ.update({
        "RIFTWIRE_DATA_DIR": str(data_dir), "RIFTWIRE_CLIP_WHISPER_PYTHON": whisper_py,
        "RIFTWIRE_CLIP_WHISPER_MODEL": whisper_model, "RIFTWIRE_CLIP_OUTRO_PATH": str(outro),
        "RIFTWIRE_URL_ACQUISITION_ENABLED": "false", "RIFTWIRE_AI_PROVIDER": "ollama",
        "RIFTWIRE_AI_MODEL": "qwen3.5:4b", "RIFTWIRE_JOB_BACKOFF_BASE_SECONDS": "5"})
    sys.path.insert(0, str(Path.cwd()))
    from sqlalchemy import select

    from app.core import config as config_mod
    from app.db.session import Database
    from app.models import AIRun, Job
    from app.services import acquisition, ai_highlights, clips, rights, sources
    from app.services.ingest import UploadSession, complete_upload
    from app.services.locations import load_layout
    from app.workers.worker import WorkerProcess
    from tests.conftest import run_migrations

    config_mod.reset_settings_cache()
    settings = config_mod.get_settings()
    settings.ensure_dirs()
    run_migrations(settings.database_url)
    db = Database(settings.database_url)
    load_layout(db, settings, "real-eval")
    assert settings.min_free_disk_bytes >= FLOOR_BYTES, "the 60 GiB floor must be in force"

    def drain(deadline_s: float = 6 * 3600) -> None:
        worker = WorkerProcess(settings, db)
        end, idle = time.monotonic() + deadline_s, 0
        while time.monotonic() < end:
            claimed = queue_mod.claim_next(db, worker.id, settings.job_lease_seconds)
            if claimed is None:
                with db.session() as s:
                    waiting = s.scalar(select(Job).where(Job.status.in_(
                        ["PENDING", "RETRY_WAIT"])))
                if waiting is None or idle > 300:
                    return
                idle += 1
                time.sleep(1)
                continue
            idle = 0
            worker._execute(claimed)

    from app.services import queue as queue_mod

    with db.session() as s:
        me = sources.create_creator(s, "Owner (evaluation)", "self").id
        local = sources.add_channel(s, me, "local").id
        game = sources.create_game(s, "gta-eval", "GTA (evaluation)").id
    results = []
    for item in items:
        print(f"\n== {item.name}: {item.path}", flush=True)
        t0 = time.monotonic()
        up = UploadSession(settings, item.path.name, item.path.stat().st_size, db)
        try:
            with open(item.path, "rb") as f:
                while chunk := f.read(4 * 1024 * 1024):
                    up.write(chunk)
            digest = up.finish()
        except BaseException:
            up.abort()
            raise
        rec = complete_upload(db, settings, up, digest).recording
        drain()
        t_import = time.monotonic() - t0
        with db.session() as s:
            src = sources.create_source(s, kind="local", creator_channel_id=local,
                                        game_id=game, title_user=item.name,
                                        acquisition_method="local_recording")
            rights.create_record(
                s, creator_id=me, basis=item.basis, coverage="sources", source_ids=[src.id],
                scope={"v": 1, "uses": ["acquire", "analyze", "edit"],
                       "acquisition_methods": ["local_recording", "creator_file"],
                       "notes": "Owner-authorized for the M6 real-footage evaluation only. "
                                "No publishing."},
                attestation_text=item.authorization)
            acquisition.attach_media(s, settings, src.id, rec.id)
            ai_highlights.request(s, settings, rec.id, game=item.game, channel=item.channel,
                                  target_count=item.request)
        t1 = time.monotonic()
        drain()
        wall = time.monotonic() - t0
        with db.session() as s:
            st = ai_highlights.describe(s, rec.id)
            jobs = {j.job_type: j for j in s.scalars(select(Job).where(
                Job.recording_id == rec.id))}
            stages = {jt: {"status": j.status, "attempts": j.attempt_count,
                           "seconds": (round((j.completed_at - j.started_at).total_seconds(), 1)
                                       if j.completed_at and j.started_at else None),
                           "last_error": j.last_error} for jt, j in jobs.items()}
            runs = s.scalars(select(AIRun).where(AIRun.request_id == st["request"]["id"])
                             .order_by(AIRun.batch, AIRun.created_at)).all()
            run_rows, windows = [], {}
            for r in runs:
                rec_json = json.loads(r.record_json)
                meta = json.loads(r.evidence_json)["meta"]
                for w in meta.get("windows", []):
                    windows[w["id"]] = w
                run_rows.append({
                    "batch": r.batch, "status": r.status, "attempts": r.attempts,
                    "repairs": max(0, r.attempts - 1), "wall_s": r.wall_s, "error": r.error,
                    "cost_usd": r.cost_usd, "windows": [w["id"] for w in meta["windows"]],
                    "output_tokens": [(a.get("metrics") or {}).get("output_tokens")
                                      for a in rec_json["attempts"]],
                    "omitted_words": meta.get("omitted_words")})
        hl = st["highlights"]
        verdict = match(hl, item.marks, windows=list(windows.values()),
                        candidates=st["highlights"] + st["rejected"])
        renders = []
        for h in hl[:render_top]:
            short = out_dir / f"{item.name}-rank{h['rank']}-short.mp4"
            try:
                short.write_bytes(clips.cut(db, settings, rec.id, h["start_ms"], h["end_ms"],
                                            layout="split", title=h["title"]))
                words, _ = clips._words(db, settings, rec, h["start_ms"],
                                        h["end_ms"] - h["start_ms"])
                renders.append({"rank": h["rank"], "file": short.name, **check_short(
                    short, outro, (h["end_ms"] - h["start_ms"]) / 1000, words,
                    out_dir / f"{item.name}-rank{h['rank']}-captions.png")})
            except Exception as e:  # noqa: BLE001 - reported, not hidden
                renders.append({"rank": h["rank"], "error": str(e)})
        result = {
            "name": item.name, "recording_id": rec.id, "duration_s": rec.duration_ms / 1000,
            "requested": item.request, "returned": len(hl),
            "summary": st["request"]["summary"], "request_status": st["request"]["status"],
            "request_error": st["request"]["error"],
            "rejected_reasons": _count(r["reason"] for r in st["rejected"]),
            "highlights": [{k: h[k] for k in (
                "rank", "start_ms", "end_ms", "title", "hook", "category", "editorial_score",
                "model_editorial_score", "penalties", "confidence", "boundary_notes",
                "text_checks")} for h in hl],
            "match": verdict, "ai_runs": run_rows, "stages": stages,
            "runtime_s": {"import_and_metadata": round(t_import, 1),
                          "analysis_and_ai": round(time.monotonic() - t1, 1),
                          "end_to_end": round(wall, 1)},
            "cost_usd": sum(r["cost_usd"] for r in run_rows), "renders": renders,
            "marks": [asdict(m) for m in item.marks],
        }
        results.append(result)
        (out_dir / f"{item.name}.json").write_text(json.dumps(result, indent=1, default=str))
        print(f"   returned {len(hl)}/{item.request}; found {verdict['recall']} marks; "
              f"extras {len(verdict['extras'])}; {wall:.0f} s; ${result['cost_usd']:.2f}",
              flush=True)
    report = render_report(results)
    (out_dir / "report.md").write_text(report)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, default=str))
    db.dispose()
    print(f"\nreport: {out_dir / 'report.md'}")
    return 0


def _count(xs) -> dict:
    out: dict = {}
    for x in xs:
        key = re.sub(r"\d+(\.\d+)?", "N", str(x))
        out[key] = out.get(key, 0) + 1
    return out


def render_report(results: list[dict]) -> str:
    marks = sum(r["match"]["marked"] for r in results)
    found = sum(r["match"]["found"] for r in results)
    returned = sum(r["returned"] for r in results)
    lines = ["# Real-footage evaluation — baseline", "",
             f"Recordings: {len(results)}; marked spans: {marks}; highlights returned: "
             f"{returned}.", "",
             f"**Pooled:** found {found}/{marks} marked moments; {found}/{returned} returned "
             "highlights matched a mark. With this sample size these numbers describe these "
             "recordings only.", "",
             "| Recording | Requested | Returned | Found | Missed | Extras | Near | "
             "Median |Δstart| s | Median |Δend| s | AI batches (attempts) | Runtime s | Cost |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        m = r["match"]
        lines.append(
            f"| {r['name']} | {r['requested']} | {r['returned']} | {m['recall']} | "
            f"{len(m['missed'])} | {len(m['extras'])} | {len(m['near_misses'])} | "
            f"{m['median_abs_start_delta_s']} | {m['median_abs_end_delta_s']} | "
            + ", ".join(f"{x['status']}({x['attempts']})" for x in r["ai_runs"])
            + f" | {r['runtime_s']['end_to_end']} | ${r['cost_usd']:.2f} |")
    for r in results:
        lines += ["", f"## {r['name']}", "",
                  f"Request {r['request_status']}: {r['returned']} of {r['requested']}."
                  + (f" Why fewer: {r['summary'].get('reason')}" if r["summary"] and
                     r["summary"].get("fewer_than_requested")
                     and r["request_status"] == "COMPLETE" else ""),
                  f"Rejected candidates by reason: {r['rejected_reasons'] or 'none'}"]
        if r.get("request_error"):
            lines.append(f"Request error: {r['request_error']}")
        lines.append("")
        for h in r["highlights"]:
            ded = "; ".join(f"-{p['points']} {p['name']} ({p['reason']})"
                            for p in h["penalties"]) or "none"
            lines.append(f"- #{h['rank']} {h['start_ms'] / 1000:.1f}-{h['end_ms'] / 1000:.1f} s "
                         f"score {h['editorial_score']} (model {h['model_editorial_score']}; "
                         f"deductions: {ded}); confidence {h['confidence']:.2f}; "
                         f"{h['title']!r}")
        for x in r["match"]["matched"]:
            lines.append(f"- found mark {x['mark'] + 1} {x['label']!r} with #"
                         f"{x['highlight_rank']}: overlap {x['overlap']}, IoU {x['iou']}, "
                         f"Δstart {x['start_delta_s']} s, Δend {x['end_delta_s']} s")
        for x in r["match"]["missed"]:
            lines.append(f"- missed mark {x['mark'] + 1} {x['label']!r} "
                         f"({x['start_s']}-{x['end_s']} s): {x['diagnosis']} — {x['detail']}")
        for x in r["match"]["extras"]:
            lines.append(f"- extra #{x['highlight_rank']} {x['start_s']}-{x['end_s']} s "
                         f"{x['title']!r}")
        for x in r["renders"]:
            lines.append(f"- render #{x['rank']}: " + (x.get("error") or json.dumps(
                x["checks"]) + f"; {x['duration_s']} s; LUFS {x['integrated_lufs']}; "
                f"peak {x['true_peak_dbfs']} dBFS; outro PSNR {x['outro_psnr_db']} dB"))
    lines += ["", "Limits: one person's marks; baseline settings only (prompt gaming.gta.v2, "
              "scoring editorial.default v1, qwen3.5:4b, Whisper base); editorial scores are "
              "estimates, not probabilities of going viral."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
