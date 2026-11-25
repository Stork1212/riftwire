"""M4 native check: real local MLX Whisper through the shared TRANSCRIBE job (macOS only).

Usage (from backend/, with backend/.venv/bin/python):
    python ../scripts/m4_native.py OUT_DIR WHISPER_PYTHON WHISPER_MODEL

Synthetic footage only: ``testsrc2`` video plus macOS ``say`` speech placed at known times,
one phrase deliberately straddling the first chunk boundary (120 s). Isolated data folder,
the real 60 GiB floor, no network, nothing downloaded. Writes ``m4-native.json`` and exits
non-zero on any failed check. Checks:

1. The transcript completes in the expected number of chunks, covers the whole recording,
   and records the model checksum that ``shasum`` computes independently.
2. Every placed phrase is recognised near its known time, and the boundary phrase's words
   appear exactly once, in order (deterministic merge on real engine output).
3. Raw words cannot be modified in the database.
4. The highlight scan and caption words reuse that transcript: no second Whisper run.
5. Requesting it again is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUT, WHISPER_PY, WHISPER_MODEL = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
OUT.mkdir(parents=True, exist_ok=True)
DATA = Path(tempfile.mkdtemp(prefix="riftwire-m4-native-"))
for k in [k for k in os.environ if k.startswith("RIFTWIRE_")]:
    del os.environ[k]
os.environ.update({
    "RIFTWIRE_DATA_DIR": str(DATA),
    "RIFTWIRE_CLIP_WHISPER_PYTHON": WHISPER_PY,
    "RIFTWIRE_CLIP_WHISPER_MODEL": WHISPER_MODEL,
    "RIFTWIRE_URL_ACQUISITION_ENABLED": "false",
    "RIFTWIRE_JOB_BACKOFF_BASE_SECONDS": "0",
})
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import func, select  # noqa: E402

from app.core import config as config_mod  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.models import Job, JobType, Transcript, TranscriptChunk  # noqa: E402
from app.services import acquisition, clips, queue, transcripts  # noqa: E402
from app.services.locations import load_layout  # noqa: E402
from app.workers.worker import WorkerProcess  # noqa: E402
from tests.conftest import run_migrations  # noqa: E402
from tests.helpers import import_file  # noqa: E402
from tests.test_rights import World  # noqa: E402

DURATION_S = 150
PHRASES = [  # (start seconds, text): the second one crosses the 120 s chunk boundary
    (5.0, "The police helicopter is right above us, take the next exit now."),
    (117.5, "Seven orange taxis crashed into the fountain at the harbor."),
    (138.0, "Mission complete, that was absolutely incredible."),
]
results: list[dict] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)


def norm(text: str) -> list[str]:
    return re.sub(r"[^a-z' ]+", " ", text.lower()).split()


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
    filters.append(f"{mix}amix=inputs={len(PHRASES)}:normalize=0,"
                   f"atrim=0:{DURATION_S}[aout]")
    out = media / "m4-speech.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=30:duration={DURATION_S}", *inputs,
                    "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-t", str(DURATION_S), str(out)],
                   check=True, timeout=600)
    return out


def drain(db, settings, max_jobs: int = 30) -> None:
    worker = WorkerProcess(settings, db)
    for _ in range(max_jobs):
        claimed = queue.claim_next(db, worker.id, settings.job_lease_seconds)
        if claimed is None:
            return
        worker._execute(claimed)


def main() -> int:
    config_mod.reset_settings_cache()
    settings = config_mod.get_settings()
    settings.ensure_dirs()
    run_migrations(settings.database_url)
    db = Database(settings.database_url)
    load_layout(db, settings, "m4-native")
    check("floor", settings.min_free_disk_bytes >= 60 * 1024**3,
          f"min free {settings.min_free_disk_bytes / 1024**3:.0f} GiB per volume")

    rec = import_file(db, settings, make_media()).recording
    drain(db, settings)
    w = World(db)
    w.licence()
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    with db.session() as s:
        acquisition.attach_media(s, settings, source_id, rec.id)
        transcripts.request(s, settings, rec.id)

    t0 = time.monotonic()
    drain(db, settings)
    elapsed = time.monotonic() - t0
    with db.session() as s:
        tr = transcripts.current(s, settings, rec.id)
        job = s.scalar(select(Job).where(Job.job_type == JobType.TRANSCRIBE.value))
        if tr is None:
            check("transcript", False, f"job {job.status}: {job.last_error}")
            return finish()
        words = transcripts.words(s, tr.id)
        chunks = s.scalars(select(TranscriptChunk).where(
            TranscriptChunk.transcript_id == tr.id).order_by(TranscriptChunk.idx)).all()
        cfg = json.loads(s.scalar(select(transcripts.TranscriptionConfig.config_json).where(
            transcripts.TranscriptionConfig.id == tr.config_id)))
        info = {"transcript_id": tr.id, "engine_version": tr.engine_version,
                "word_count": tr.word_count, "coverage_ms": tr.coverage_ms,
                "duration_ms": tr.duration_ms, "chunks": len(chunks),
                "chunk_elapsed_ms": [c.elapsed_ms for c in chunks],
                "wall_seconds": round(elapsed, 1), "config": cfg,
                "job_attempts": job.attempt_count}
    (OUT / "transcript-words.json").write_text(json.dumps(words, indent=1))
    check("transcript", tr.status == "COMPLETE" and tr.coverage_ms == tr.duration_ms
          and len(chunks) == 2,
          f"{tr.word_count} words, {len(chunks)} chunks, {elapsed:.1f} s, "
          f"engine {tr.engine_version}")

    h = hashlib.sha256()
    for name in ("config.json", "weights.npz"):
        h.update(name.encode() + b"\0")
        h.update((Path(WHISPER_MODEL) / name).read_bytes())
    check("model checksum", cfg["model_sha256"] == h.hexdigest(),
          f"fingerprinted {cfg['model_sha256'][:16]}…")

    texts = [norm(w["w"]) for w in words]
    flat = [(t, w) for ts, w in zip(texts, words, strict=True) for t in ts]
    for start, phrase in PHRASES:
        expect = norm(phrase)
        near = [t for t, w in flat if start * 1000 - 1500 <= w["start_ms"]
                <= start * 1000 + 8000]
        hit = sum(1 for e in expect if e in near)
        check(f"phrase@{start:g}s", hit >= 0.8 * len(expect),
              f"{hit}/{len(expect)} words recognised near {start:g} s: {' '.join(near)}")
    b_start = PHRASES[1][0] * 1000
    region = [t for t, w in flat if b_start - 1500 <= w["start_ms"] <= b_start + 8000]
    dupes = [t for t in set(region) if region.count(t) > 1 and len(t) > 3]
    starts = [w["start_ms"] for w in words]
    check("boundary words once, in order", not dupes and starts == sorted(starts),
          f"boundary region: {' '.join(region)}; duplicates {dupes or 'none'}")

    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("UPDATE transcript_words SET start_ms = start_ms + 1")
        conn.commit()
        immutable = False
    except sqlite3.DatabaseError:
        immutable = True
    conn.close()
    check("raw words immutable", immutable, "direct UPDATE refused by trigger")

    with db.session() as s:
        queue.enqueue(s, rec.id, JobType.HIGHLIGHT_SCAN, settings.job_max_attempts)
    drain(db, settings)
    caption_words, cap_tr = clips._words(db, settings, rec, 137_000, 8_000)
    with db.session() as s:
        scan = s.scalar(select(Job).where(Job.job_type == JobType.HIGHLIGHT_SCAN.value))
        scan_result = json.loads(scan.result_json or "{}")
        n_tr = s.scalar(select(func.count()).select_from(Transcript))
        again = transcripts.request(s, settings, rec.id)
        chunks_after = s.scalar(select(func.count()).select_from(TranscriptChunk))
    (OUT / "highlight-scan.json").write_text(json.dumps(scan_result, indent=1))
    check("scan reuses transcript",
          scan.status == "COMPLETED" and scan_result.get("transcript_id") == info["transcript_id"]
          and n_tr == 1 and chunks_after == info["chunks"],
          f"scan {scan.status}, transcript {scan_result.get('transcript_id')}, "
          f"{len(scan_result.get('items', []))} candidates, transcripts={n_tr}")
    check("captions reuse transcript", cap_tr == info["transcript_id"] and caption_words,
          " ".join(w["w"] for w in caption_words))
    check("no second run", again is None, "request() is a no-op once complete")
    info["caption_words_137s"] = caption_words
    (OUT / "m4-native-info.json").write_text(json.dumps(info, indent=1, default=str))
    db.dispose()
    return finish()


def finish() -> int:
    ok = all(r["ok"] for r in results)
    (OUT / "m4-native.json").write_text(json.dumps({"ok": ok, "data_dir": str(DATA),
                                                     "results": results}, indent=1))
    print(f"RESULT {'PASS' if ok else 'FAIL'} ({sum(r['ok'] for r in results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
