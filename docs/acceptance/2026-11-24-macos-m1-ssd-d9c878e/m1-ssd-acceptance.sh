#!/bin/bash
# RIFTWIRE M1 physical-SSD acceptance (checklist §11, checks 1-8), pinned to main d9c878e.
# Run once, in one Terminal window, with the SanDisk plugged in:
#   bash ~/Downloads/m1-ssd-acceptance-d9c878e.sh
# Optional: a different mount path as the first argument (default "/Volumes/SANDISK SSD").
#
# Synthetic media only. An isolated library on the internal disk (deleted at the end) and a
# new test folder on the SSD (kept, so you can inspect it; delete it yourself afterwards).
# Your real RIFTWIRE library, your checkout and your branch are not touched: main is run
# from a separate git worktree, which is removed at the end. The 60 GiB floor is the real
# one. The script pauses and tells you exactly when to pull or re-plug the cable and when
# to sleep the Mac; type "skip" at a prompt to skip that check (it is recorded as SKIPPED).
set -u -o pipefail
EXPECTED=d9c878ea86add7ce54eabf14579c79798cdd5108
REPO="$HOME/Claude/riftwire"
SSD="${1:-/Volumes/SANDISK SSD}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$HOME/Claude/riftwire-env/m1-ssd-d9c878e-$STAMP"
WT="$HOME/Claude/riftwire-env/m1-ssd-worktree-$STAMP"
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
cleanup() {
  git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1
  rmdir "$LOCK" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT TERM

say "RIFTWIRE M1 physical-SSD acceptance  started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "evidence folder: $OUT"
[ -d "$REPO/.git" ] || abort "repo not found at $REPO"
PY="$REPO/backend/.venv/bin/python"
[ -x "$PY" ] || abort "backend venv not found at $PY (run scripts/bootstrap.sh)"
mount | grep -q " on $SSD " || abort "the SSD is not mounted at $SSD (plug it in, or pass its path)"

git -C "$REPO" fetch origin main > "$OUT/git.txt" 2>&1 || abort "git fetch failed (see git.txt)"
git -C "$REPO" cat-file -e "$EXPECTED^{commit}" 2>>"$OUT/git.txt" || abort "commit $EXPECTED not found"
git -C "$REPO" worktree add --detach "$WT" "$EXPECTED" >> "$OUT/git.txt" 2>&1 || abort "worktree failed (see git.txt)"
HEAD_SHA=$(git -C "$WT" rev-parse HEAD)
[ "$HEAD_SHA" = "$EXPECTED" ] || abort "worktree HEAD is $HEAD_SHA, expected $EXPECTED"
say "code: main $HEAD_SHA (separate worktree; your checkout is untouched)"
{
  echo "== machine"; sw_vers; uname -m; sysctl -n machdep.cpu.brand_string; sysctl -n hw.memsize
  echo "== tools"; "$PY" --version; ffmpeg -version | head -1; git --version
  echo "== SSD"; diskutil info "$SSD"
} > "$OUT/environment.txt" 2>&1

cat > "$OUT/driver.py" <<'PYEOF'
"""Interactive driver for the eight physical-SSD checks (see the shell wrapper)."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

WT, OUT, SSD = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
BACKEND = WT / "backend"
PY = sys.executable
PORT = 18815
BASE = f"http://127.0.0.1:{PORT}"
GIB = 1024**3
FLOOR_GIB = 60
TMP = Path(tempfile.mkdtemp(prefix="riftwire-m1-ssd-"))
DATA, MEDIA = TMP / "data", TMP / "media"
MEDIA.mkdir(parents=True)
DEST = SSD / f"RIFTWIRE-M1-acceptance-{OUT.name.rsplit('-', 1)[-1]}"
ENV = {k: v for k, v in os.environ.items() if not k.startswith("RIFTWIRE_")}
ENV.update({"RIFTWIRE_DATA_DIR": str(DATA), "RIFTWIRE_API_PORT": str(PORT),
            "RIFTWIRE_WORKER_POLL_SECONDS": "0.5", "RIFTWIRE_WORKER_HEARTBEAT_SECONDS": "1",
            "RIFTWIRE_URL_ACQUISITION_ENABLED": "false"})   # real 60 GiB floor (default)
RESULTS: list[dict] = []
FACTS: dict = {}


class Fail(Exception):
    pass


class Skip(Exception):
    pass


def check(ok: bool, msg: str) -> None:
    if not ok:
        raise Fail(msg)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(OUT / "driver.log", "a") as f:
        f.write(line + "\n")


def banner(msg: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\n>>> {msg}\n{bar}", flush=True)
    log(f"PROMPT: {msg}")


def ask(msg: str) -> None:
    banner(msg)
    ans = input("Press Enter when done (or type skip): ").strip().lower()
    if ans == "skip":
        raise Skip("skipped by the owner")


def step(n: str, title: str, fn) -> None:
    t0 = time.time()
    log(f"--- check {n}: {title}")
    try:
        note = fn() or ""
        res = "PASS"
    except Skip as e:
        note, res = str(e), "SKIPPED"
    except Exception as e:          # report every failure, keep going
        note, res = f"{type(e).__name__}: {e}", "FAIL"
    RESULTS.append({"check": n, "title": title, "result": res, "note": note,
                    "seconds": round(time.time() - t0, 1)})
    log(f"{res}  {n}  {title} — {note}")


# ------------------------------------------------------------------------- the stack

class Stack:
    def __init__(self):
        self.api = self.worker = None
        self.logs = OUT / "logs"
        self.logs.mkdir(exist_ok=True)

    def start(self, worker: bool = True):
        if self.api is None:
            self.api = subprocess.Popen(
                [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
                 str(PORT), "--no-access-log"], cwd=BACKEND, env=ENV,
                stdout=open(self.logs / "api.txt", "ab"), stderr=subprocess.STDOUT)
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    if httpx.get(BASE + "/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.3)
            else:
                raise Fail("API did not start (see logs/api.txt)")
        if worker and self.worker is None:
            self.worker = subprocess.Popen(
                [PY, "-m", "app.workers.worker"], cwd=BACKEND, env=ENV,
                stdout=open(self.logs / "worker.txt", "ab"), stderr=subprocess.STDOUT)
            time.sleep(1.5)
            check(self.worker.poll() is None, "worker exited at startup (see logs/worker.txt)")

    def stop(self, which=("worker", "api")):
        for name in which:
            p = getattr(self, name)
            if p is not None and p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(60)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(10)
            setattr(self, name, None)


S = Stack()


def get(path, **kw):
    return httpx.get(BASE + path, timeout=30, **kw)


def upload(path: Path) -> httpx.Response:
    size = path.stat().st_size
    with open(path, "rb") as f:
        return httpx.post(BASE + "/api/v1/recordings/import", params={"filename": path.name},
                          content=f, headers={"Content-Length": str(size)},
                          timeout=httpx.Timeout(60, read=1800, write=1800))


def rec(rid: str) -> dict:
    return get(f"/api/v1/recordings/{rid}").json()


def job(d: dict, t: str) -> dict:
    return next(j for j in d["jobs"] if j["job_type"] == t)


def wait_ready(rid: str, timeout: float = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = rec(rid)
        stages = {j["job_type"]: j["status"] for j in d["jobs"]}
        if d["status"] == "READY" and set(stages.values()) == {"COMPLETED"}:
            return d
        if d["status"] == "FAILED":
            raise Fail(f"recording failed: {d.get('error_message')}")
        time.sleep(1)
    raise Fail(f"recording {rid} not READY within {timeout:.0f} s: {rec(rid)['status']}")


def storage_state(kind="recordings") -> dict:
    st = get("/api/v1/storage").json()
    return next(x for x in st["locations"] if x["kind"] == kind)


def wait_state(pred, what: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = storage_state()
            if pred(last):
                return last
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise Fail(f"{what} not observed within {timeout:.0f} s (last: {last})")


def ready() -> tuple[int, dict]:
    r = get("/health/ready")
    return r.status_code, r.json()


def events_since(t0: str) -> list[dict]:
    items = get("/api/v1/events", params={"limit": 200}).json()["items"]
    return [e for e in items if e["timestamp"] >= t0]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mounted() -> bool:
    return os.path.ismount(SSD)


def wait_mount(state: bool, timeout: float = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mounted() == state:
            return
        time.sleep(0.5)
    raise Fail(f"the SSD was not {'mounted' if state else 'unmounted'} within {timeout:.0f} s")


def diskinfo(path: Path) -> dict:
    out = subprocess.run(["diskutil", "info", "-plist", str(path)], capture_output=True,
                         timeout=60, check=False).stdout
    return plistlib.loads(out) if out else {}


def free_gib(path: Path) -> float:
    return shutil.disk_usage(path).free / GIB


def clip(name: str, seconds: float, raw: bool = False) -> Path:
    """Synthetic media. raw=True: 1080p uncompressed video (about 93 MB per second), so
    multi-GB files are written quickly without encoding."""
    p = MEDIA / name
    v = ["-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le"] if raw else \
        ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]
    size = "1920x1080" if raw else "640x360"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={size}:rate=30:duration={seconds}", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", *v, str(p)],
                   check=True, timeout=1800)
    return p


def stored_files() -> set[str]:
    c = sqlite3.connect(f"file:{DATA / 'riftwire.db'}?mode=ro", uri=True, timeout=10)
    try:
        return {r[0] for r in c.execute("SELECT stored_filename FROM recordings")}
    finally:
        c.close()


def strays() -> list[str]:
    """Anything on the SSD test folder that is not the marker, an empty staging folder, or
    a recording RIFTWIRE knows about (a partial file at a final path would show here)."""
    known = stored_files()
    bad = []
    for p in DEST.iterdir():
        if p.name in (".riftwire-location.json", ".DS_Store") or p.name in known:
            continue
        if p.name == ".staging" and p.is_dir() and not any(p.iterdir()):
            continue
        bad.append(p.name)
    return bad


def marker() -> dict:
    return json.loads((DEST / ".riftwire-location.json").read_text())


def migrate(*args: str, timeout: float = 3600) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(WT / "scripts" / "migrate_storage.py"), "recordings",
                           *args], cwd=BACKEND, env=ENV, capture_output=True, text=True,
                          timeout=timeout, check=False)


# ------------------------------------------------------------------------- the checks

def preflight():
    info = diskinfo(SSD)
    FACTS["ssd"] = {k: info.get(k) for k in ("VolumeName", "FilesystemType", "VolumeUUID",
                                            "MountPoint", "DeviceNode", "BusProtocol")}
    check(info.get("FilesystemType") == "apfs", f"the SSD is {info.get('FilesystemType')}, "
          "not APFS")
    check(free_gib(Path.home()) >= FLOOR_GIB + 30, f"internal disk: {free_gib(Path.home()):.1f}"
          f" GiB free; need {FLOOR_GIB + 30} GiB (floor + about 30 GiB of synthetic media)")
    check(free_gib(SSD) >= FLOOR_GIB + 30, f"SSD: {free_gib(SSD):.1f} GiB free")
    check(not DEST.exists(), f"{DEST} already exists")
    probe = SSD / f".riftwire-write-probe-{os.getpid()}"
    try:
        probe.mkdir()
        probe.rmdir()
    except PermissionError as e:
        raise Fail("macOS does not let Terminal write to the SSD (Privacy & Security). Open "
                   "System Settings > Privacy & Security > Files & Folders > Terminal and "
                   "turn on Removable Volumes (or give Terminal Full Disk Access), quit and "
                   f"reopen Terminal, then run this again. ({e})") from None
    subprocess.run([PY, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=ENV,
                   check=True, capture_output=True, timeout=300)
    log("generating synthetic media (about 19 GiB; a minute or two)…")
    FACTS["media"] = {}
    for name, sec, raw in (("base-a.mkv", 20, True), ("base-b.mkv", 21, True),
                           ("base-c.mkv", 22, True), ("throughput.mkv", 55, True),
                           ("midcopy.mkv", 80, True)):
        p = clip(name, sec, raw)
        FACTS["media"][name] = p.stat().st_size
    for i in range(4):
        clip(f"small-{i}.mp4", 2 + i / 10)
    return (f"SSD {info.get('VolumeName')} apfs {info.get('VolumeUUID')}; "
            f"internal {free_gib(Path.home()):.0f} GiB free, SSD {free_gib(SSD):.0f} GiB free")


def c6_migration():
    S.start()
    ids = []
    for n in ("base-a.mkv", "base-b.mkv", "base-c.mkv"):
        r = upload(MEDIA / n)
        check(r.status_code == 201, f"import {n}: HTTP {r.status_code} {r.text[:200]}")
        ids.append(r.json()["recording"]["id"])
    for rid in ids:
        wait_ready(rid)
    FACTS["base_ids"] = ids
    S.stop()
    dry = migrate(str(DEST))
    (OUT / "migration-dry-run.txt").write_text(dry.stdout + dry.stderr)
    check(dry.returncode == 0 and "all checks pass" in dry.stdout, "dry run: " + dry.stdout[-400:])
    check(not DEST.exists(), "the dry run created the destination")
    total = sum(FACTS["media"][n] for n in ("base-a.mkv", "base-b.mkv", "base-c.mkv"))
    t0 = time.time()
    proc = subprocess.Popen([PY, str(WT / "scripts" / "migrate_storage.py"), "recordings",
                             str(DEST), "--execute"], cwd=BACKEND, env=ENV,
                            stdout=open(OUT / "migration-interrupted.txt", "w"),
                            stderr=subprocess.STDOUT)
    promoted = []
    while proc.poll() is None and time.time() - t0 < 1200:
        if DEST.exists():
            promoted = [p for p in DEST.iterdir() if p.suffix == ".mkv"]
            if promoted:
                break
        time.sleep(0.2)
    if proc.poll() is not None:
        tail = (OUT / "migration-interrupted.txt").read_text()[-800:]
        check(proc.returncode == 0, f"the migration exited with {proc.returncode}: {tail}")
        raise Fail("the migration finished before it could be interrupted")
    proc.send_signal(signal.SIGKILL)               # a crash, not a clean stop
    proc.wait(30)
    log(f"migration killed after {len(promoted)} file(s) promoted")
    out = migrate(str(DEST), "--execute")
    (OUT / "migration-resumed.txt").write_text(out.stdout + out.stderr)
    took = time.time() - t0
    check(out.returncode == 0 and "now lives at" in out.stdout, "resume: " + out.stdout[-600:])
    check("all match" in out.stdout, "final re-hash not reported")
    earlier = int(out.stdout.split(" verified earlier")[0].rsplit(", ", 1)[-1]) \
        if " verified earlier" in out.stdout else 0
    check(earlier >= 1, "the resumed run did not reuse the files verified before the crash")
    originals = list((DATA / "recordings").glob("*.mkv"))
    check(len(originals) == 3, f"{len(originals)} originals left on the internal disk")
    S.start()
    st = storage_state()
    check(st["online"] and Path(st["path"]) == DEST, str(st))
    for rid in ids:
        check(rec(rid)["status"] == "READY", f"{rid} not READY after the switch")
    small = upload(MEDIA / "small-0.mp4")
    check(small.status_code == 201, f"import after switch: HTTP {small.status_code}")
    sid = small.json()["recording"]["id"]
    wait_ready(sid, 120)
    check(rec(sid)["status"] == "READY" and any(
        p.name.startswith(sid) for p in DEST.iterdir()), "new import did not land on the SSD")
    FACTS["migration_mib_s"] = round(total / took / 2**20, 1)
    return (f"dry run clean; --execute killed after {len(promoted)} promoted file(s); "
            f"resumed ({earlier} verified earlier), final re-hash all match; "
            f"{total / GIB:.2f} GiB in {took:.0f} s overall incl. crash and resume; "
            f"originals kept (3); switched; a new import landed on the SSD")


def c5_throughput():
    p = MEDIA / "throughput.mkv"
    size = p.stat().st_size
    t0 = time.time()
    r = upload(p)
    up = time.time() - t0
    check(r.status_code == 201, f"HTTP {r.status_code}")
    rid = r.json()["recording"]["id"]
    d = wait_ready(rid, 1800)
    j = job(d, "INGEST")
    secs = (datetime.fromisoformat(j["completed_at"]) -
            datetime.fromisoformat(j["started_at"])).total_seconds()
    FACTS["throughput"] = {"bytes": size, "upload_s": round(up, 1),
                           "ingest_cross_volume_s": round(secs, 1),
                           "ingest_mib_s": round(size / secs / 2**20, 1)}
    check(any(x.name.startswith(rid) for x in DEST.iterdir()), "file not on the SSD")
    return (f"{size / GIB:.2f} GiB: upload to internal temp {up:.0f} s; cross-volume finalize "
            f"to the SSD incl. SHA-256 verification {secs:.0f} s "
            f"({size / secs / 2**20:.0f} MiB/s)")


def c7_floor():
    st = get("/api/v1/storage").json()
    vol = next(v for v in st["volumes"] if "recordings" in v["kinds"])
    check(vol["floor_bytes"] == FLOOR_GIB * GIB, f"floor {vol['floor_bytes']}")
    loc = storage_state()
    check(loc["fs_type"] == "apfs", str(loc))
    FACTS["ssd_volume"] = vol
    return (f"SSD volume floor {vol['floor_bytes'] / GIB:.0f} GiB, free "
            f"{(vol.get('free_bytes') or 0) / GIB:.0f} GiB; recordings location apfs; imports "
            "above were admitted against it")


def c1_identity(label: str) -> str:
    info = diskinfo(SSD)
    m = marker()
    st = storage_state()
    check(info.get("FilesystemType") == "apfs", f"{info.get('FilesystemType')}")
    check((m.get("hw_volume_uuid") or "").lower() == (info.get("VolumeUUID") or "").lower(),
          f"marker {m.get('hw_volume_uuid')} != diskutil {info.get('VolumeUUID')}")
    check(st["online"], str(st))
    return f"{label}: diskutil {info.get('VolumeUUID')} == marker; online"


def c2_idle_unplug():
    S.stop(("worker",))
    r = upload(MEDIA / "small-1.mp4")
    check(r.status_code == 201, f"HTTP {r.status_code}")
    rid = r.json()["recording"]["id"]
    t0 = now_iso()
    ask("PULL THE SSD CABLE NOW (do not eject first). macOS will warn about an improper "
        "eject; that is expected. Then press Enter.")
    wait_mount(False, 120)
    check(not SSD.exists(), f"{SSD} still exists after unplug")
    S.start()                                       # worker starts with the drive gone
    st = wait_state(lambda x: not x["online"], "location offline")
    check(st["reason"] == "volume_not_mounted", st["reason"])
    code, body = ready()
    check(code == 503 and not body["checks"]["storage"]["ok"], f"readiness {code}")
    temp_before = sorted(x.name for x in (DATA / "temp").iterdir())
    w = upload(MEDIA / "small-2.mp4")
    check(w.status_code == 503 and w.json()["error"]["code"] == "storage_offline",
          f"upload while unplugged: HTTP {w.status_code}")
    check(sorted(x.name for x in (DATA / "temp").iterdir()) == temp_before, "temp changed")
    time.sleep(8)
    j = job(rec(rid), "INGEST")
    check(j["status"] == "PENDING" and j["attempt_count"] == 0,
          f"job ran while unplugged: {j['status']} attempts={j['attempt_count']}")
    check(not SSD.exists(), f"something recreated {SSD} on the internal disk")
    ask("PLUG THE SSD BACK IN. Wait until it appears in Finder, then press Enter.")
    wait_mount(True, 300)
    d = wait_ready(rid, 300)
    check(job(d, "INGEST")["attempt_count"] == 1, "the waiting job used extra attempts")
    types = {e["event_type"] for e in events_since(t0)}
    check({"storage_offline", "storage_online"} <= types, str(sorted(types)))
    code, _ = ready()
    check(code == 200, f"readiness {code} after re-plug")
    ident = c1_identity("after physical re-plug")
    FACTS["identity_after_replug"] = ident
    return ("offline as volume_not_mounted; no folder at the mount path; readiness 503 "
            "(the dashboard banner's source); upload refused with nothing written; job waited "
            "with 0 attempts; storage_offline then storage_online; job completed on attempt 1; "
            + ident)


def c3_midcopy_unplug():
    p = MEDIA / "midcopy.mkv"
    ask("Next: a large import. As soon as the script prints UNPLUG NOW, pull the SSD cable. "
        "Press Enter to start.")
    r = upload(p)
    check(r.status_code == 201, f"HTTP {r.status_code}")
    rid = r.json()["recording"]["id"]
    t0 = time.time()
    running_since = None
    while time.time() - t0 < 300:
        j = job(rec(rid), "INGEST")
        if j["status"] == "RUNNING":
            running_since = running_since or time.time()
            if (j["progress_percent"] or 0) >= 3 or time.time() - running_since > 3:
                break
        if j["status"] == "COMPLETED":
            raise Fail("the copy finished before it could be interrupted")
        time.sleep(0.2)
    banner("UNPLUG NOW — pull the SSD cable (the copy is running)")
    wait_mount(False, 120)
    pct = job(rec(rid), "INGEST")["progress_percent"]
    log(f"unplugged at INGEST progress {pct}%")
    deadline = time.time() + 180
    while time.time() < deadline:
        j = job(rec(rid), "INGEST")
        if j["status"] != "RUNNING":
            break
        time.sleep(1)
    j = job(rec(rid), "INGEST")
    check(j["status"] in ("PENDING", "RETRY_WAIT"),
          f"after the unplug the job is {j['status']} ({j.get('last_error')})")
    check(rec(rid)["status"] != "READY", "recording READY although its copy was cut")
    outcome = f"{j['status']}, attempts {j['attempt_count']}, error: {j.get('last_error')}"
    ask("PLUG THE SSD BACK IN. Wait until it appears in Finder, then press Enter.")
    wait_mount(True, 300)
    d = wait_ready(rid, 1800)
    check(d["status"] == "READY", d["status"])
    bad = strays()
    check(not bad, f"unexpected files on the SSD: {bad}")
    return (f"unplugged at {pct}% of the cross-volume copy → job {outcome}; after re-plug the "
            "copy was redone and SHA-256-verified, recording READY; no partial or staging "
            "file left on the SSD")


def c4_swapped_drive():
    m = marker()
    (OUT / "marker-copied.json").write_text(json.dumps(m, indent=2))
    ask("UNPLUG THE SSD (you may eject it first this time). The script will then mount a "
        "throwaway APFS disk image with the same volume name in its place.")
    wait_mount(False, 120)
    img = TMP / "swap-test.dmg"
    name = SSD.name
    cr = subprocess.run(["hdiutil", "create", "-size", "512m", "-fs", "APFS", "-volname",
                         name, str(img)], capture_output=True, text=True, timeout=300,
                        check=False)
    check(cr.returncode == 0, cr.stderr)
    at = subprocess.run(["hdiutil", "attach", "-nobrowse", str(img)], capture_output=True,
                        text=True, timeout=120, check=False)
    check(at.returncode == 0, at.stderr)
    try:
        check(mounted(), f"the image did not mount at {SSD}: {at.stdout}")
        other = diskinfo(SSD)
        FACTS["swap_volume_uuid"] = other.get("VolumeUUID")
        check(other.get("VolumeUUID") != FACTS["ssd"]["VolumeUUID"], "same VolumeUUID?")
        DEST.mkdir()
        (DEST / ".riftwire-location.json").write_text(json.dumps(m))   # the copied folder
        st = wait_state(lambda x: not x["online"], "location offline", 120)
        check(st["reason"] == "wrong_volume", f"reason {st['reason']}")
        code, _ = ready()
        check(code == 503, f"readiness {code}")
        w = upload(MEDIA / "small-3.mp4")
        check(w.status_code == 503, f"upload onto the wrong drive: HTTP {w.status_code}")
        left = sorted(p.name for p in DEST.iterdir())
        left = [x for x in left if x != ".DS_Store"]
        check(left == [".riftwire-location.json"], f"RIFTWIRE wrote to the wrong drive: {left}")
    finally:
        subprocess.run(["hdiutil", "detach", str(SSD)], capture_output=True, timeout=120,
                       check=False)
        img.unlink(missing_ok=True)
    ask("PLUG THE REAL SSD BACK IN. Wait until it appears in Finder, then press Enter.")
    wait_mount(True, 300)
    wait_state(lambda x: x["online"], "location back online", 120)
    return (f"a different APFS volume ({FACTS['swap_volume_uuid']}) at the same path with the "
            "RIFTWIRE folder and marker copied onto it: refused as wrong_volume, readiness "
            "503, upload refused, nothing written to it; the real SSD came back online "
            "(disk image used as the second drive)")


def c8_sleep_wake():
    t0 = now_iso()
    ask("PUT THE MAC TO SLEEP NOW (Apple menu > Sleep), leave it asleep at least 2 minutes, "
        "wake it, log in, then press Enter here.")
    wait_mount(True, 300)
    wait_state(lambda x: x["online"], "location online after wake", 180)
    code, _ = ready()
    check(code == 200, f"readiness {code} after wake")
    ident = c1_identity("after sleep/wake")
    r = upload(MEDIA / "small-3.mp4")
    check(r.status_code == 201, f"import after wake: HTTP {r.status_code}")
    rid = r.json()["recording"]["id"]
    wait_ready(rid, 300)
    ev = [e["event_type"] for e in events_since(t0) if e["event_type"].startswith("storage_")]
    FACTS["sleep_events"] = ev
    return (f"after wake: location online, readiness 200, {ident}; an import landed on the "
            f"SSD. Storage events during sleep: {ev or 'none (stayed mounted)'}")


def main() -> int:
    log(f"isolated library: {DATA}; SSD test folder: {DEST}")
    try:
        step("0", "Preflight: APFS SSD, free space, synthetic media", preflight)
        if RESULTS[-1]["result"] != "PASS":
            return 1
        step("6", "Migration to the SSD: dry run, crash, resume, re-hash, switch", c6_migration)
        if RESULTS[-1]["result"] != "PASS":
            log("the remaining checks need the library on the SSD; stopping")
            return 1
        step("5", "Throughput: multi-GB cross-volume finalize incl. SHA-256", c5_throughput)
        step("7", "Per-volume floor and reservations on the SSD", c7_floor)
        step("1", "Real identity (APFS, OS VolumeUUID == marker)",
             lambda: c1_identity("before unplugging"))
        step("2", "Physical unplug while idle, then re-plug", c2_idle_unplug)
        step("3", "Physical unplug mid-copy, then re-plug", c3_midcopy_unplug)
        step("4", "Swapped drive at the same path", c4_swapped_drive)
        step("8", "Sleep/wake", c8_sleep_wake)
    finally:
        S.stop()
        (OUT / "results.json").write_text(json.dumps({"results": RESULTS, "facts": FACTS},
                                                     indent=2, default=str))
        if "--keep" not in sys.argv:
            shutil.rmtree(TMP, ignore_errors=True)
    return 0 if all(r["result"] == "PASS" for r in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
PYEOF

say ""
say "Running the checks. Follow the prompts; the SSD test folder will be:"
say "  $SSD/RIFTWIRE-M1-acceptance-$STAMP"
say ""
(cd "$WT/backend" && "$PY" "$OUT/driver.py" "$WT" "$OUT" "$SSD")
RC=$?
say ""
say "== results"
"$PY" - "$OUT/results.json" <<'EOF' | tee -a "$SUM"
import json, sys
d = json.load(open(sys.argv[1]))
for r in d["results"]:
    print(f"{r['result']:8} check {r['check']:>2}  {r['title']} — {r['note']}")
EOF
say ""
say "$( [ $RC -eq 0 ] && echo "ALL PASSED" || echo "NOT ALL PASSED (see summary, driver.log, logs/)" )"
say "The synthetic test folder stays on the SSD for you to inspect; delete it yourself:"
say "  $SSD/RIFTWIRE-M1-acceptance-$STAMP"
exit $RC
