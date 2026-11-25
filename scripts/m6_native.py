"""M5/M6 native check: the full highlight workflow with real local models (macOS only).

Usage (from backend/, with backend/.venv/bin/python, an Ollama server already running on
127.0.0.1 with qwen3.5:4b, nothing downloaded):
    python ../scripts/m6_native.py OUT_DIR WHISPER_PYTHON WHISPER_MODEL OUTRO_MP4

Synthetic footage only: 180 s of ``testsrc2`` with two bursts of heavy motion and
macOS-spoken, fictional gameplay commentary at known times (reactions at 42-56 s and
122-136 s). Isolated data folder, real 60 GiB floor. The workflow:

authorized recording -> shared transcript (real MLX Whisper) -> audio and visual signals
-> candidate windows -> local AI evaluation (real Qwen3.5 4B via Ollama) -> ranked
highlights with titles and hooks -> 35/65 Short with captions and the full outro.

**Gate** (engineering, not quality): the request completes with at least one accepted AI
run, cost 0 everywhere, every selected highlight is grounded (times from stored ids, inside
the recording, 8-60 s, evidence ids resolve), titles pass the claim check, and the
rendered Short is 1080x1920 with the outro's 2.5 s added. **Informative only:** whether
the selected highlights overlap the scripted reaction moments (one synthetic recording
says nothing about highlight quality).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT, WHISPER_PY, WHISPER_MODEL, OUTRO = (Path(sys.argv[1]), sys.argv[2], sys.argv[3],
                                         Path(sys.argv[4]))
OUT.mkdir(parents=True, exist_ok=True)
DATA = Path(tempfile.mkdtemp(prefix="riftwire-m6-native-"))
for k in [k for k in os.environ if k.startswith("RIFTWIRE_")]:
    del os.environ[k]
os.environ.update({
    "RIFTWIRE_DATA_DIR": str(DATA),
    "RIFTWIRE_CLIP_WHISPER_PYTHON": WHISPER_PY,
    "RIFTWIRE_CLIP_WHISPER_MODEL": WHISPER_MODEL,
    "RIFTWIRE_CLIP_OUTRO_PATH": str(OUTRO),
    "RIFTWIRE_URL_ACQUISITION_ENABLED": "false",
    "RIFTWIRE_JOB_BACKOFF_BASE_SECONDS": "5",
    "RIFTWIRE_AI_PROVIDER": "ollama",
    "RIFTWIRE_AI_MODEL": "qwen3.5:4b",
})
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import select  # noqa: E402

from app.core import config as config_mod  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.models import AIRun, Job  # noqa: E402
from app.services import (  # noqa: E402
    acquisition,
    ai_highlights,
    analysis_evidence,
    clips,
    highlight_candidates,
    queue,
)
from app.services.locations import load_layout  # noqa: E402
from app.workers.worker import WorkerProcess  # noqa: E402
from tests.conftest import run_migrations  # noqa: E402
from tests.helpers import import_file  # noqa: E402
from tests.test_rights import World  # noqa: E402

DURATION_S = 180
PHRASES = [
    (10.0, "Alright, let's just drive around the city and see what happens."),
    (42.0, "Oh my god, the cops are right behind us, go go go!"),
    (50.0, "No way, we actually made that jump! That was insane!"),
    (85.0, "Okay, quiet moment, let me check the map."),
    (122.0, "Wait, what is that? The car is flying! This game is broken!"),
    (130.0, "I can't believe that just happened, we escaped the police!"),
    (165.0, "Mission complete. Thanks for watching."),
]
REACTIONS = [(40_000, 58_000), (120_000, 138_000)]
results: list[dict] = []


def check(name: str, ok: bool, detail: str, gate: bool = True) -> None:
    results.append({"check": name, "ok": bool(ok), "gate": gate, "detail": detail})
    tag = ("PASS" if ok else "FAIL") if gate else ("INFO")
    print(f"{tag}  {name}: {detail}", flush=True)


def make_media() -> Path:
    media = DATA / "media"
    media.mkdir()
    inputs, filters = [], []
    for i, (start, text) in enumerate(PHRASES):
        aiff = media / f"p{i}.aiff"
        subprocess.run(["say", "-o", str(aiff), text], check=True, timeout=120)
        inputs += ["-i", str(aiff)]
        ms = int(start * 1000)
        filters.append(f"[{i + 1}:a]aresample=48000,adelay={ms}|{ms},apad[a{i}]")
    mix = "".join(f"[a{i}]" for i in range(len(PHRASES)))
    filters.append(f"{mix}amix=inputs={len(PHRASES)}:normalize=0,atrim=0:{DURATION_S}[aout]")
    filters.append("[0:v]noise=alls=80:allf=t+u:enable='between(t,40,56)+between(t,120,136)',"
                   "format=yuv420p[vout]")
    out = media / "m6-gameplay.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=30:duration={DURATION_S}", *inputs,
                    "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k",
                    "-t", str(DURATION_S), str(out)], check=True, timeout=900)
    return out


def drain(db, settings, max_jobs: int = 80, deadline_s: float = 3600) -> None:
    worker = WorkerProcess(settings, db)
    end = time.monotonic() + deadline_s
    idle = 0
    while time.monotonic() < end and max_jobs > 0:
        claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
        if claimed is None:
            with db.session() as s:
                waiting = s.scalar(select(Job).where(Job.status.in_(["PENDING",
                                                                     "RETRY_WAIT"])))
            if waiting is None or idle > 120:
                return
            idle += 1
            time.sleep(1)
            continue
        idle = 0
        max_jobs -= 1
        worker._execute(claimed)


def main() -> int:
    config_mod.reset_settings_cache()
    settings = config_mod.get_settings()
    settings.ensure_dirs()
    run_migrations(settings.database_url)
    db = Database(settings.database_url)
    load_layout(db, settings, "m6-native")
    check("floor", settings.min_free_disk_bytes >= 60 * 1024**3,
          f"min free {settings.min_free_disk_bytes / 1024**3:.0f} GiB per volume")
    rec = import_file(db, settings, make_media()).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)
        ai_highlights.request(s, settings, rec.id, game="gta", channel="rumbleframe",
                              target_count=5)
    t0 = time.monotonic()
    drain(db, settings)
    wall = time.monotonic() - t0
    with db.session() as s:
        st = ai_highlights.describe(s, rec.id)
        runs = s.scalars(select(AIRun)).all()
        run_rows = [{"batch": r.batch, "status": r.status, "attempts": r.attempts,
                     "wall_s": r.wall_s, "model": r.model, "digest": r.model_digest,
                     "error": r.error, "cost_usd": r.cost_usd,
                     "metrics": [a.get("metrics") for a in json.loads(r.record_json)
                                 ["attempts"]]} for r in runs]
        job = s.scalar(select(Job).where(Job.job_type == "AI_HIGHLIGHTS"))
        job_state = {"status": job.status, "attempts": job.attempt_count,
                     "last_error": job.last_error}
    (OUT / "highlights.json").write_text(json.dumps(st, indent=1, default=str))
    (OUT / "ai-runs.json").write_text(json.dumps(run_rows, indent=1, default=str))
    accepted = [r for r in run_rows if r["status"] == "accepted"]
    check("request completes", st["request"] and st["request"]["status"] == "COMPLETE",
          f"request {st['request'] and st['request']['status']}; job {job_state}; "
          f"{wall:.0f} s end to end")
    first = run_rows[0] if run_rows else {"model": "-", "digest": ""}
    check("genuine local inference", bool(accepted) and all(
        any((m or {}).get("output_tokens") for m in r["metrics"]) for r in accepted),
          f"{len(runs)} runs ({', '.join(r['status'] for r in run_rows)}); model "
          f"{first['model']} {first['digest']}")
    check("cost zero", all(r["cost_usd"] == 0 for r in run_rows), "every run records $0")
    attempts = [r["attempts"] for r in run_rows]
    lat = sorted(r["wall_s"] for r in run_rows)
    check("latency (recorded)", True, f"per batch wall s {lat}; attempts {attempts}", gate=False)
    hl = st["highlights"]
    grounded, problems = True, []
    with db.session() as s:
        for h in hl:
            length = h["end_ms"] - h["start_ms"]
            if not (0 <= h["start_ms"] < h["end_ms"] <= rec.duration_ms and
                    8_000 <= length <= 60_000):
                grounded = False
                problems.append(f"{h['id']}: bounds {h['start_ms']}-{h['end_ms']}")
            for ref in h["evidence"]:
                for item in ref["ids"]:
                    try:
                        analysis_evidence.resolve(s, item)
                    except Exception:  # noqa: BLE001
                        grounded = False
                        problems.append(f"{h['id']}: unresolved {item}")
            ok, bad = highlight_candidates.ground_text(h["title"], [])
            if not ok and not h["text_checks"]:
                grounded = False
                problems.append(f"{h['id']}: title claim {bad}")
    check("highlights grounded", grounded and len(hl) <= 5,
          f"{len(hl)} of 5 returned; " + ("; ".join(problems) or "all bounds and ids valid"))
    for h in hl:
        print(f"      #{h['rank']} {h['start_ms'] / 1000:.1f}-{h['end_ms'] / 1000:.1f}s "
              f"score {h['editorial_score']} conf {h['confidence']:.2f} "
              f"{h['category']}: {h['title']!r} / {h['hook']!r}", flush=True)
    overlap = [h for h in hl if any(min(h["end_ms"], b) - max(h["start_ms"], a) > 0
                                    for a, b in REACTIONS)]
    check("scripted reaction moments", True,
          f"{len(overlap)} of {len(hl)} selected highlights overlap the scripted reactions "
          "(informative only; not a quality measurement)", gate=False)
    if hl:
        top = hl[0]
        data = clips.cut(db, settings, rec.id, top["start_ms"], top["end_ms"],
                         layout="split", title=top["title"])
        short = OUT / "top-highlight-short.mp4"
        short.write_bytes(data)
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:format=duration", "-of", "json", str(short)],
            check=True, capture_output=True, timeout=60).stdout)
        video = next(x for x in probe["streams"] if x["codec_type"] == "video")
        expected = (top["end_ms"] - top["start_ms"]) / 1000 + 2.5
        got = float(probe["format"]["duration"])
        words, tr = clips._words(db, settings, rec, top["start_ms"],
                                 top["end_ms"] - top["start_ms"])
        check("rendered Short", (video["width"], video["height"]) == (1080, 1920)
              and abs(got - expected) < 0.3 and bool(words) and tr == top["transcript_id"],
              f"{video['width']}x{video['height']}, {got:.2f} s (clip + 2.5 s outro = "
              f"{expected:.2f} s), {len(words)} caption words from transcript {tr}")
    else:
        check("rendered Short", False, "no highlight to render")
    db.dispose()
    ok = all(r["ok"] for r in results if r["gate"])
    (OUT / "m6-native.json").write_text(json.dumps({"ok": ok, "data_dir": str(DATA),
                                                    "results": results}, indent=1))
    print(f"RESULT {'PASS' if ok else 'FAIL'} "
          f"({sum(r['ok'] for r in results if r['gate'])}/"
          f"{sum(1 for r in results if r['gate'])} gate checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
