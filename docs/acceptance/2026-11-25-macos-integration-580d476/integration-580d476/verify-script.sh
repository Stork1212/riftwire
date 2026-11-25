#!/bin/bash
# RIFTWIRE integration check, pinned to the LOCAL branch phase2/integration at 580d476
# (b13b91f URL acquisition + public_web, merged with phase2/highlight-queue 1b73afe).
# Run once, after url-verify-b13b91f.sh:   bash ~/Claude/integration-e2e-580d476.sh
#
# 1. Full suites (test.sh), acceptance and smoke on isolated data folders and ports 18910/18911.
# 2. The merged migrations on a DISPOSABLE COPY of the demo database (sqlite .backup is
#    read-only on the original; the demo keeps running on its own port).
# 3. One end-to-end test on an isolated instance (port 18921, real 60 GiB floor): saved URL ->
#    queued acquisition (stand-in downloader) -> linked recording -> Find highlights ->
#    review a timestamp (clips.txt) -> export a 35/65 Short, checking the acquisition
#    history, sampled frame, transcript evidence, red word highlights, original audio and the
#    two-second outro. Synthetic media and synthetic speech (macOS `say`) only.
# The branch stays local. Nothing is pushed.
set -u -o pipefail
EXPECTED=580d4765ca71ccf66c21030f34270ff9dba49547
BRANCH=phase2/integration
BUNDLE="$HOME/Claude/riftwire-env/riftwire-integration-580d476.bundle"
REPO="$HOME/Claude/riftwire"
DEMO_DB="/private/tmp/riftwire-url-preview-data/riftwire.db"
FLOOR_GIB=60
MARGIN_GIB=2
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/Claude/riftwire-env/integration-e2e-580d476-$STAMP"
mkdir -p "$OUT"
SUM="$OUT/summary.txt"
say() { echo "$@" | tee -a "$SUM"; }
abort() { say "ABORT: $*"; exit 2; }

LOCK="$HOME/Claude/riftwire-env/.verify.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  say "ABORT: another verification run is in progress ($LOCK exists)."
  say "If no run is active, remove it with: rmdir $LOCK"
  exit 3
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
trap 'exit 130' INT TERM

cd "$REPO" || abort "repo not found at $REPO"
say "RIFTWIRE integration check  started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "evidence folder: $OUT"
DIRTY=$(git status --porcelain --untracked-files=normal)
[ -z "$DIRTY" ] || { echo "$DIRTY" > "$OUT/dirty-tree.txt"; abort "working tree not clean (see dirty-tree.txt)"; }
[ -f "$BUNDLE" ] || abort "bundle not found at $BUNDLE"
git bundle verify "$BUNDLE" > "$OUT/git.log" 2>&1 || abort "bundle does not apply (see git.log)"
git fetch "$BUNDLE" "$BRANCH:refs/remotes/bundle/integration" >> "$OUT/git.log" 2>&1 || abort "fetch from bundle failed"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git checkout "$BRANCH" >> "$OUT/git.log" 2>&1 || abort "checkout failed"
  git merge --ff-only bundle/integration >> "$OUT/git.log" 2>&1 || abort "fast-forward failed"
else
  git checkout -b "$BRANCH" bundle/integration >> "$OUT/git.log" 2>&1 || abort "checkout failed"
fi
HEAD_SHA=$(git rev-parse HEAD)
[ "$HEAD_SHA" = "$EXPECTED" ] || abort "HEAD is $HEAD_SHA, expected $EXPECTED"
say "HEAD: $HEAD_SHA (local branch $BRANCH; not pushed)"

PY="$REPO/backend/.venv/bin/python"
"$PY" -c "import PIL" 2>/dev/null || abort "Pillow is not installed in backend/.venv. It is a NEW dependency \
from phase2/highlight-queue (35/65 caption overlays) and needs your approval (CLAUDE.md §8). If you approve: \
$REPO/backend/.venv/bin/pip install -c $REPO/backend/constraints.txt Pillow   then run this script again."
WHISPER_PY="$REPO/bench/.venv/bin/python"
WHISPER_MODEL="$REPO/data/models/mlx-community--whisper-base-mlx"
[ -x "$WHISPER_PY" ] || abort "local Whisper runtime not found at $WHISPER_PY"
[ -f "$WHISPER_MODEL/weights.npz" ] || abort "whisper-base model not found at $WHISPER_MODEL"
command -v say >/dev/null || abort "macOS 'say' not found (synthetic speech)"
{
  echo "== machine"; sw_vers; uname -m; sysctl -n machdep.cpu.brand_string
  echo "== tools"; "$PY" --version; ffmpeg -version | head -1; node -v; git --version
  "$PY" -c "import PIL; print('Pillow', PIL.__version__)"
  "$WHISPER_PY" -c "import mlx_whisper, importlib.metadata as m; print('mlx-whisper', m.version('mlx-whisper'))"
} > "$OUT/environment.txt" 2>&1

free_gib() { df -k "$REPO" | tail -1 | awk '{printf "%.2f", $4/1048576}'; }
BEFORE=$(free_gib)
say "free space before: $BEFORE GiB (floor $FLOOR_GIB GiB)"
awk -v f="$BEFORE" -v m=$((FLOOR_GIB+MARGIN_GIB)) 'BEGIN{exit !(f>=m)}' || abort "not enough free space above the 60 GiB floor"

run() {
  local name=$1; shift
  say ""; say "== $name: $*"
  local t0=$(date +%s)
  "$@" > "$OUT/$name.log" 2>&1
  local rc=$?
  say "   exit code $rc  ($(( $(date +%s) - t0 )) s)  free now $(free_gib) GiB"
  eval "RC_$name=$rc"
}

run test        scripts/test.sh
run acceptance  "$PY" scripts/acceptance.py --port 18911 --min-free-gib $FLOOR_GIB --report "$OUT/acceptance.json"
export RIFTWIRE_SMOKE_PORT=18910
run smoke       scripts/smoke_test.sh "$OUT/smoke-report.txt"

# ---------------------------------------------------------------- demo-database migration
say ""; say "== demo database migration (disposable copy)"
if [ -f "$DEMO_DB" ]; then
  COPY_DIR=$(mktemp -d -t riftwire-demo-db)
  sqlite3 "$DEMO_DB" ".backup '$COPY_DIR/riftwire.db'" 2>> "$OUT/demo-migration.log" \
    && cp "$COPY_DIR/riftwire.db" "$OUT/demo-db-before.db" \
    && (cd backend && "$PY" - "$COPY_DIR/riftwire.db" >> "$OUT/demo-migration.log" 2>&1 <<'PYEOF'
import os, sqlite3, subprocess, sys
db = sys.argv[1]
def snap():
    c = sqlite3.connect(db)
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                      "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    ver = c.execute("SELECT version_num FROM alembic_version").fetchone()
    trig = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
    fk = c.execute("PRAGMA foreign_key_check").fetchall()
    c.close()
    return ver[0] if ver else None, counts, trig, fk
v0, c0, t0, fk0 = snap()
print("before:", v0, "triggers", t0, "fk violations", len(fk0)); print(c0)
env = {k: v for k, v in os.environ.items() if not k.startswith("RIFTWIRE_")}
env["RIFTWIRE_DATABASE_PATH"] = db
r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env,
                   capture_output=True, text=True, timeout=600)
print(r.stdout[-3000:], r.stderr[-3000:])
v1, c1, t1, fk1 = snap()
print("after:", v1, "triggers", t1, "fk violations", len(fk1)); print(c1)
lost = {t: (c0[t], c1.get(t)) for t in c0 if c1.get(t, 0) < c0[t]}
ok = r.returncode == 0 and v1 == "0009" and t1 == 14 and not fk1 and not lost
print("lost rows:", lost)
print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PYEOF
)
  RC_demo=$?
  say "   demo copy migration exit $RC_demo (details in demo-migration.log; copy kept as demo-db-before.db)"
  rm -rf "$COPY_DIR"
else
  RC_demo=0
  say "   no demo database at $DEMO_DB; skipped (not a failure)"
fi

# ---------------------------------------------------------------------- end-to-end test
cat > "$OUT/e2e.py" <<'PYEOF'
"""End-to-end: saved URL -> ACQUIRE -> recording -> highlight scan -> clips.txt -> 35/65 Short."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO, OUT = Path(sys.argv[1]), Path(sys.argv[2])
WHISPER_PY, WHISPER_MODEL = sys.argv[3], sys.argv[4]
BACKEND = REPO / "backend"
PY = sys.executable
PORT = 18921
BASE = f"http://127.0.0.1:{PORT}"
TMP = Path(tempfile.mkdtemp(prefix="riftwire-e2e-"))
DATA, MEDIA, BIN = TMP / "data", TMP / "media", TMP / "fake-bin"
MEDIA.mkdir(parents=True)
BIN.mkdir()
SPEECH_AT = 20.0        # seconds into the source where the synthetic commentary starts
LINES = ("Oh my god, the cops are right behind us. We got a problem. "
         "Hold on, this getaway is insane. No way, we made it!")
CHECKS: list[dict] = []
FACTS: dict = {}


class Fail(Exception):
    pass


def check(ok, msg):
    if not ok:
        raise Fail(msg)


def log(msg):
    print(msg, flush=True)
    with open(OUT / "e2e-driver.txt", "a") as f:
        f.write(msg + "\n")


def record(name, fn):
    t0 = time.time()
    try:
        note, res = fn() or "", "PASS"
    except Exception as e:
        note, res = f"{type(e).__name__}: {e}", "FAIL"
    CHECKS.append({"check": name, "result": res, "note": note,
                   "seconds": round(time.time() - t0, 1)})
    log(f"{res:5} {name} — {note}")
    return res == "PASS"


def ff(*args, timeout=600):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True,
                   timeout=timeout)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", str(path)], capture_output=True, text=True,
                       check=True, timeout=60)
    return json.loads(r.stdout)


def pcm(path, start, dur, rate=8000):
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t",
                        f"{dur:.3f}", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
                        "-f", "s16le", "-"], capture_output=True, check=True, timeout=120)
    b = r.stdout
    return [int.from_bytes(b[i:i + 2], "little", signed=True) for i in range(0, len(b) - 1, 2)]


def corr(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    da = sum((x - ma) ** 2 for x in a) ** .5
    db = sum((y - mb) ** 2 for y in b) ** .5
    return num / (da * db) if da and db else 0.0


def rgb_region(path, t, x, y, w, h):
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i",
                        str(path), "-frames:v", "1", "-vf", f"crop={w}:{h}:{x}:{y}",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True,
                       check=True, timeout=60)
    return r.stdout


# ------------------------------------------------------------------------------ media
def make_media():
    speech = MEDIA / "speech.aiff"
    subprocess.run(["say", "-o", str(speech), LINES], check=True, timeout=120)
    src = MEDIA / "source.mp4"
    # 0-18 s still, 18-45 s moving (testsrc2), 45-60 s still; commentary from 20 s.
    ff("-f", "lavfi", "-i", "color=c=0x303030:s=1280x720:r=30:d=18",
       "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=27",
       "-f", "lavfi", "-i", "color=c=0x303030:s=1280x720:r=30:d=15",
       "-i", str(speech),
       "-filter_complex",
       # Grayscale gameplay: any red in the Short can only come from a caption highlight.
       "[1:v]hue=s=0[m];[0:v][m][2:v]concat=n=3:v=1:a=0[v];"
       f"[3:a]aresample=48000,adelay=delays={int(SPEECH_AT * 1000)}:all=1,"
       "apad=whole_dur=60[a]",
       "-map", "[v]", "-map", "[a]", "-t", "60", "-c:v", "libx264", "-preset", "veryfast",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(src))
    outro = MEDIA / "outro.mp4"   # synthetic magenta outro with a tone, 3 s (2 s are used)
    ff("-f", "lavfi", "-i", "color=c=0xFF00FF:s=1080x1920:r=30:d=3",
       "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(outro))
    fake = BIN / "yt-dlp"
    fake.write_text(f"#!{PY}\nimport shutil, sys\na = sys.argv[1:]\n"
                    "if '--version' in a:\n    print('e2e-stand-in'); sys.exit(0)\n"
                    f"shutil.copyfile({str(src)!r}, a[a.index('-o') + 1])\n")
    fake.chmod(0o755)
    return src, outro


SRC, OUTRO = make_media()
ENV = {k: v for k, v in os.environ.items() if not k.startswith("RIFTWIRE_")}
ENV.update({"RIFTWIRE_DATA_DIR": str(DATA), "RIFTWIRE_API_PORT": str(PORT),
            "RIFTWIRE_URL_ACQUISITION_ENABLED": "true",
            "RIFTWIRE_CLIP_WHISPER_PYTHON": WHISPER_PY,
            "RIFTWIRE_CLIP_WHISPER_MODEL": WHISPER_MODEL,
            "RIFTWIRE_CLIP_OUTRO_PATH": str(OUTRO),
            "RIFTWIRE_WORKER_POLL_SECONDS": "0.5",
            "PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"})
procs = []


def start():
    subprocess.run([PY, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=ENV,
                   check=True, capture_output=True, timeout=300)
    (OUT / "e2e-logs").mkdir(exist_ok=True)
    procs.append(subprocess.Popen([PY, "-m", "uvicorn", "app.main:app", "--host",
                                   "127.0.0.1", "--port", str(PORT), "--no-access-log"],
                                  cwd=BACKEND, env=ENV, stdout=open(OUT / "e2e-logs/api.txt", "ab"),
                                  stderr=subprocess.STDOUT))
    for _ in range(80):
        try:
            if httpx.get(BASE + "/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    procs.append(subprocess.Popen([PY, "-m", "app.workers.worker"], cwd=BACKEND, env=ENV,
                                  stdout=open(OUT / "e2e-logs/worker.txt", "ab"),
                                  stderr=subprocess.STDOUT))
    time.sleep(1.5)


def stop():
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()


def ok(r, code=200):
    check(r.status_code == code, f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def get(path, **kw):
    return httpx.get(BASE + path, timeout=60, **kw)


def post(path, **kw):
    return httpx.post(BASE + path, timeout=900, **kw)


S: dict = {}


def step_acquire():
    cr = ok(post("/api/v1/creators", json={"display_name": "E2E Public Streamer",
                                           "relationship": "unaffiliated"}), 201)
    ch = ok(post(f"/api/v1/creators/{cr['id']}/channels",
                 json={"platform": "twitch", "handle": "E2EPublicStreamer"}), 201)
    S["creator"] = cr["id"]
    src = ok(post("/api/v1/sources", json={"url": "https://www.twitch.tv/videos/777000111",
                                          "creator_channel_id": ch["id"]}), 201)
    S["source"] = src["id"]
    check(src["rights_status"] == "public_web", src["rights_status"])
    deadline = time.time() + 300
    while time.time() < deadline:
        d = ok(get(f"/api/v1/sources/{S['source']}"))
        j = d.get("acquire_job")
        if j and j["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(1)
    check(j and j["status"] == "COMPLETED", f"ACQUIRE: {j}")
    rid = j["recording_id"]
    while time.time() < deadline:
        rec = ok(get(f"/api/v1/recordings/{rid}"))
        stages = {x["job_type"]: x["status"] for x in rec["jobs"]
                  if x["job_type"] in ("INGEST", "METADATA", "THUMBNAIL")}
        if rec["status"] == "READY" and set(stages.values()) == {"COMPLETED"} and len(stages) == 3:
            break
        time.sleep(1)
    check(rec["status"] == "READY", f"recording {rec['status']}")
    check(rec["source_id"] == S["source"] and rec["acquisition_method"] == "url_fetch",
          f"{rec['source_id']} {rec['acquisition_method']}")
    S["rec"] = rid
    return f"public_web recorded; ACQUIRE completed; recording {rid} READY, linked, url_fetch"


def step_history():
    prov = ok(get(f"/api/v1/sources/{S['source']}/provenance"))
    (OUT / "e2e-provenance.json").write_text(json.dumps(prov, indent=2))
    acts = {(p["entity"], p["action"]): p for p in prov}
    for need in (("rights", "recorded_public_web"), ("source", "acquired_by_url"),
                 ("recording", "attached_to_source")):
        check(need in acts, f"missing {need}")
    check(acts[("recording", "attached_to_source")]["details"]["method"] == "url_fetch", "method")
    got = acts[("source", "acquired_by_url")]["details"]
    check(got["recording_id"] == S["rec"] and got["downloader"] == "yt-dlp", str(got))
    return (f"recorded_public_web → acquired_by_url ({got['downloader']} "
            f"{got['downloader_version']}, sha256 {got['sha256'][:12]}…) → attached_to_source "
            "(url_fetch)")


def step_scan():
    ok(post(f"/api/v1/recordings/{S['rec']}/highlight-scan"))
    deadline = time.time() + 900
    while time.time() < deadline:
        d = ok(get(f"/api/v1/recordings/{S['rec']}/highlight-scan"))
        if d["job"] and d["job"]["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(2)
    check(d["job"]["status"] == "COMPLETED", f"scan {d['job']['status']}: {d['job']['last_error']}")
    res = d["result"]
    (OUT / "e2e-highlight-scan.json").write_text(json.dumps(res, indent=2))
    check(res["speech_available"] and res["items"], "no speech or no candidates")
    S["items"] = res["items"]
    top = res["items"][0]
    S["top"] = top
    return (f"{len(res['items'])} candidate(s); top {top['start_ms']}-{top['end_ms']} ms, "
            f"score {top['score']}, cues {top['speech_cues']}")


def step_transcript():
    spoken = {"cops", "problem", "getaway", "made"}
    hit = [i for i in S["items"] if i["excerpt"]]
    check(hit, "no candidate carries a transcript excerpt")
    best = max(hit, key=lambda i: sum(w in i["excerpt"].lower() for w in spoken))
    found = sorted(w for w in spoken if w in best["excerpt"].lower())
    check(len(found) >= 2, f"excerpt does not match the commentary: {best['excerpt'][:200]}")
    check(best["start_ms"] <= SPEECH_AT * 1000 + 9000 and best["end_ms"] >= SPEECH_AT * 1000,
          f"evidence window {best['start_ms']}-{best['end_ms']} misses the speech")
    S["evidence"] = best
    return f"excerpt \"{best['excerpt'][:90]}\" (matched {found}); cues {best['speech_cues']}"


def step_frame():
    idx = S["items"].index(S["evidence"])
    r = get(f"/api/v1/recordings/{S['rec']}/highlight-scan/frame/{idx}")
    check(r.status_code == 200 and r.headers["content-type"].startswith("image/jpeg"),
          f"HTTP {r.status_code} {r.headers.get('content-type')}")
    check(r.content[:2] == b"\xff\xd8", "not a JPEG")
    p = OUT / "e2e-sampled-frame.jpg"
    p.write_bytes(r.content)
    v = [s for s in probe(p)["streams"] if s["codec_type"] == "video"][0]
    check(v["width"] == 480, f"width {v['width']}")
    return f"JPEG {v['width']}x{v['height']} at {S['evidence']['peak_ms']} ms (e2e-sampled-frame.jpg)"


def step_review():
    start = max(0, int(SPEECH_AT) - 2)
    end = start + 15
    S["clip"] = (start * 1000, end * 1000)
    text = (f"URL=https://www.twitch.tv/videos/777000111\n"
            f"00:00:{start:02d}-00:00:{end:02d} Getaway commentary\n")
    (OUT / "e2e-clips.txt").write_text(text)
    d = ok(post(f"/api/v1/recordings/{S['rec']}/clip-list", json={"text": text}))
    item = d["items"][0]
    check(item["from_ms"] == start * 1000 and item["to_ms"] == end * 1000, str(item))
    return f"clips.txt range {start}-{end} s accepted for the linked recording: {item}"


def step_export():
    start, end = S["clip"]
    params = {"start_ms": start, "end_ms": end, "layout": "split", "title": "Getaway"}
    r = post(f"/api/v1/recordings/{S['rec']}/clip", params=params)
    check(r.status_code == 403 and
          r.json()["error"]["details"]["reason"] == "creator_permission_needed",
          f"export of public footage was not refused: HTTP {r.status_code} {r.text[:200]}")
    ok(post("/api/v1/rights", json={
        "creator_id": S["creator"], "basis": "creator_permission", "coverage": "creator",
        "scope": {"v": 1, "uses": ["acquire", "analyze", "edit"],
                  "acquisition_methods": ["url_fetch"], "max_clip_seconds": 60},
        "attestation_text": "E2E TEST ONLY: synthetic creator permission for a synthetic "
                            "test creator in an isolated instance."}), 201)
    r = post(f"/api/v1/recordings/{S['rec']}/clip", params=params)
    check(r.status_code == 200 and r.headers["content-type"].startswith("video/mp4"),
          f"HTTP {r.status_code}: {r.text[:300]}")
    p = OUT / "e2e-short.mp4"
    p.write_bytes(r.content)
    S["short"] = p
    return f"refused before permission (creator_permission_needed); exported {len(r.content)} bytes after"


def step_short_format():
    info = probe(S["short"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    dur = float(info["format"]["duration"])
    length = (S["clip"][1] - S["clip"][0]) / 1000
    check((v["width"], v["height"]) == (1080, 1920), f"{v['width']}x{v['height']}")
    check(a, "no audio stream")
    check(abs(dur - (length + 2.0)) <= 0.2, f"duration {dur:.2f} s, expected {length + 2:.2f}")
    S["dur"] = dur
    return f"1080x1920, audio present, {dur:.2f} s = {length:.0f} s clip + 2 s outro"


def step_outro():
    dur = S["dur"]
    px = rgb_region(S["short"], dur - 1.0, 530, 950, 20, 20)
    r_, g_, b_ = (sum(px[i::3]) / (len(px) / 3) for i in range(3))
    check(r_ > 200 and g_ < 70 and b_ > 200, f"end frame colour {r_:.0f},{g_:.0f},{b_:.0f}")
    before = rgb_region(S["short"], dur - 2.6, 530, 950, 20, 20)
    r2, g2, b2 = (sum(before[i::3]) / (len(before) / 3) for i in range(3))
    check(not (r2 > 200 and g2 < 70 and b2 > 200), "outro colour appears before the last 2 s")
    return (f"last 2 s are the outro (magenta {r_:.0f},{g_:.0f},{b_:.0f}); "
            f"0.6 s earlier is gameplay ({r2:.0f},{g2:.0f},{b2:.0f})")


def _red(t):
    px = rgb_region(S["short"], t, 0, 633, 1080, 145)
    return sum(1 for i in range(0, len(px), 3)
               if px[i] > 190 and px[i + 1] < 80 and px[i + 2] < 90)


def step_red_words():
    start = S["clip"][0] / 1000
    silent = _red(0.5)            # before the commentary starts: no caption, no red
    check(silent < 50, f"red in the caption band before any speech ({silent} px)")
    best, at = 0, None
    t = SPEECH_AT - start
    while t < min(SPEECH_AT - start + 9, S["dur"] - 2.2):
        red = _red(t)
        if red > best:
            best, at = red, t
        t += 0.25
    check(best >= 400, f"no red word highlight in the caption band (max {best} px)")
    shot = OUT / "e2e-caption-frame.png"
    ff("-ss", f"{at:.3f}", "-i", str(S["short"]), "-frames:v", "1", str(shot))
    return (f"red highlight under the spoken word: {best} px at {at:.2f} s; none before "
            f"speech ({silent} px) (e2e-caption-frame.png)")


def step_audio():
    start = S["clip"][0] / 1000
    probe_at = SPEECH_AT - start
    a = pcm(SRC, start + probe_at, 6)
    b = pcm(S["short"], probe_at, 6)
    best = max(corr(a[k:], b) for k in range(0, 400, 8))
    check(best >= 0.7, f"export audio does not match the source (r={best:.2f})")
    return f"export audio matches the source commentary (r={best:.2f}, 6 s from {probe_at:.1f} s)"


try:
    start()
    steps = [("saved URL → queued acquisition → linked recording", step_acquire),
             ("acquisition history", step_history),
             ("Find highlights (queued scan)", step_scan),
             ("transcript evidence", step_transcript),
             ("sampled frame", step_frame),
             ("review a timestamp (clips.txt)", step_review),
             ("export 35/65 Short (creator permission gate)", step_export),
             ("Short format and duration", step_short_format),
             ("two-second outro", step_outro),
             ("red word highlights", step_red_words),
             ("original audio", step_audio)]
    for name, fn in steps:
        if not record(name, fn) and name in ("saved URL → queued acquisition → linked recording",
                                             "Find highlights (queued scan)",
                                             "export 35/65 Short (creator permission gate)"):
            log("stopping: later checks depend on this one")
            break
finally:
    stop()
    (OUT / "e2e-results.json").write_text(json.dumps({"checks": CHECKS}, indent=2))
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if CHECKS and all(c["result"] == "PASS" for c in CHECKS) and len(CHECKS) == 11 else 1)
PYEOF

say ""; say "== end-to-end"
(cd "$REPO/backend" && "$PY" "$OUT/e2e.py" "$REPO" "$OUT" "$WHISPER_PY" "$WHISPER_MODEL") 2>&1 | tee "$OUT/e2e.log" | grep -E "^(PASS|FAIL|stopping)" | tee -a "$SUM"
RC_e2e=${PIPESTATUS[0]}

say ""; say "free space after: $(free_gib) GiB (before $BEFORE GiB)"
say ""; say "== counts"
grep -E "[0-9]+ (passed|failed|skipped|error)" "$OUT/test.log" | tee -a "$SUM"
grep -E "Tests " "$OUT/test.log" | tee -a "$SUM"
grep -Ei "steps passed" "$OUT/acceptance.log" | tail -1 | tee -a "$SUM"
grep -cE "^\s*PASS" "$OUT/smoke.log" | sed 's/^/smoke PASS lines: /' | tee -a "$SUM"
grep -cE "^\s*FAIL" "$OUT/smoke.log" | sed 's/^/smoke FAIL lines: /' | tee -a "$SUM"
git status --porcelain --untracked-files=normal > "$OUT/tree-after.txt"
say "working tree after: $([ -s "$OUT/tree-after.txt" ] && echo 'CHANGED (see tree-after.txt)' || echo clean)"
say ""
say "RESULT  test=$RC_test  acceptance=$RC_acceptance  smoke=$RC_smoke  demo-migration=$RC_demo  e2e=$RC_e2e"
say "Exported Short for you to watch: $OUT/e2e-short.mp4"
if [ "$RC_test" -eq 0 ] && [ "$RC_acceptance" -eq 0 ] && [ "$RC_smoke" -eq 0 ] && [ "$RC_demo" -eq 0 ] && [ "$RC_e2e" -eq 0 ]; then
  say "ALL PASSED"; exit 0
else
  say "FAILURES (see logs)"; exit 1
fi
