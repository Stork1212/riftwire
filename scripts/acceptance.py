#!/usr/bin/env python3
"""RIFTWIRE end-to-end acceptance test (synthetic media only): Phase 1, M1 storage, M2 rights.

Runs a real API process (uvicorn), a real worker process and the built dashboard against an
ISOLATED temporary data directory (your real data dir is never touched), then walks through the
Phase 1 acceptance scenario and prints PASS/FAIL per step.

    backend/.venv/bin/python scripts/acceptance.py [--port 8765] [--report out.json]
                                                   [--min-free-gib 60] [--keep]

Requires: ffmpeg/ffprobe, backend/.venv, a built dashboard (frontend/dist) for step 4.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PY = BACKEND / ".venv" / "bin" / "python"

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("run with backend/.venv/bin/python (httpx is a backend dev dependency)")


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Stack:
    def __init__(self, data_dir: Path, port: int, env_extra: dict[str, str]):
        self.data_dir = data_dir
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("RIFTWIRE_")}
        self.env.update({
            "RIFTWIRE_ENV": "test",  # enables the test-only slow-FFmpeg hook used in step 19
            "RIFTWIRE_DATA_DIR": str(data_dir),
            "RIFTWIRE_API_PORT": str(port),
            "RIFTWIRE_JOB_LEASE_SECONDS": "6",
            "RIFTWIRE_WORKER_HEARTBEAT_SECONDS": "1",
            "RIFTWIRE_WORKER_POLL_SECONDS": "0.2",
            "RIFTWIRE_JOB_BACKOFF_BASE_SECONDS": "1",
            "RIFTWIRE_WORKER_SHUTDOWN_GRACE_SECONDS": "5",
            "RIFTWIRE_STALE_TEMP_SECONDS": "3600",
            # Never contact a platform: fetching is on by default in the app; only step
            # M2-7 switches it on, with a stand-in downloader first on PATH.
            "RIFTWIRE_URL_ACQUISITION_ENABLED": "false",
            **env_extra,
        })
        self.api: subprocess.Popen | None = None
        self.worker: subprocess.Popen | None = None
        self.logs = data_dir / "acceptance-logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def migrate(self):
        subprocess.run([str(BACKEND / ".venv" / "bin" / "alembic"), "upgrade", "head"],
                       cwd=BACKEND, env=self.env, check=True, capture_output=True)

    def start_api(self, **extra):
        env = {**self.env, **extra}
        self.api = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
             str(self.port), "--no-access-log"], cwd=BACKEND, env=env,
            stdout=open(self.logs / "api.out", "ab"), stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(self.base + "/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            check(self.api.poll() is None, "API exited during startup")
            time.sleep(0.2)
        raise Fail("API did not become healthy")

    def start_worker(self, **extra):
        env = {**self.env, **extra}
        self.worker = subprocess.Popen([str(PY), "-m", "app.workers.worker"], cwd=BACKEND,
                                       env=env, stdout=open(self.logs / "worker.out", "ab"),
                                       stderr=subprocess.STDOUT)
        time.sleep(1.0)
        check(self.worker.poll() is None, "worker exited during startup")

    def stop(self, which=("worker", "api"), sig=signal.SIGTERM):
        for name in which:
            p = getattr(self, name)
            if p and p.poll() is None:
                p.send_signal(sig)
                try:
                    p.wait(30)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(5)
            setattr(self, name, None)

    def get(self, path, **kw):
        return httpx.get(self.base + path, timeout=30, **kw)

    def post(self, path, **kw):
        return httpx.post(self.base + path, timeout=120, **kw)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True, timeout=300)


def make_media(d: Path) -> dict[str, Path]:
    m = {
        "valid": d / "gta-synthetic-heist.mp4",
        "noaudio": d / "synthetic-chase.webm",
        "invalid": d / "corrupted-capture.mp4",
        "unsupported": d / "notes.txt",
    }
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30000/1001:duration=12",
           "-f", "lavfi", "-i", "sine=frequency=330:duration=12", "-c:v", "libx264",
           "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(m["valid"]))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60:duration=4", "-c:v",
           "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "1M", str(m["noaudio"]))
    m["invalid"].write_bytes(os.urandom(256 * 1024))
    m["unsupported"].write_text("not a video\n")
    return m


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], capture_output=True, check=True).stdout
    return json.loads(out)


def temp_contents(data_dir: Path) -> list[Path]:
    """Files in temp/, excluding the location's identity marker (M1)."""
    return [p for p in (data_dir / "temp").iterdir() if p.name != ".riftwire-location.json"]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def upload(stack: Stack, path: Path, name: str | None = None, progress: list | None = None,
           params: dict | None = None):
    size = path.stat().st_size

    def body():
        sent = 0
        with open(path, "rb") as f:
            while chunk := f.read(256 * 1024):
                sent += len(chunk)
                if progress is not None:
                    progress.append(round(sent * 100 / size))
                yield chunk

    return stack.post("/api/v1/recordings/import",
                      params={"filename": name or path.name, **(params or {})},
                      content=body(), headers={"Content-Length": str(size),
                                               "Content-Type": "application/octet-stream"})


def wait_ready(stack: Stack, rid: str, timeout=120, observe: list | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = stack.get(f"/api/v1/recordings/{rid}").json()
        if observe is not None:
            observe.append({j["job_type"]: (j["status"], j["progress_percent"]) for j in d["jobs"]})
        stages = {j["job_type"]: j["status"] for j in d["jobs"]
                  if j["job_type"] in ("INGEST", "METADATA", "THUMBNAIL")}
        if (len(stages) == 3 and set(stages.values()) == {"COMPLETED"}
                and d["status"] == "READY"):
            return d
        if d["status"] == "FAILED":
            raise Fail(f"recording failed: {d['error_message']}")
        time.sleep(0.3)
    raise Fail("processing did not complete in time")


def run(args) -> int:
    results: list[dict] = []
    tmp = Path(tempfile.mkdtemp(prefix="riftwire-acceptance-"))
    data_dir, media_dir = tmp / "data", tmp / "media"
    media_dir.mkdir(parents=True)
    extra = {"RIFTWIRE_MIN_FREE_DISK_BYTES": str(int(args.min_free_gib * 1024**3))}
    stack = Stack(data_dir, args.port, extra)
    ctx: dict = {}

    def step(n, title, fn):
        t0 = time.time()
        try:
            note = fn() or ""
            results.append({"step": n, "title": title, "result": "PASS", "note": note,
                            "seconds": round(time.time() - t0, 2)})
            print(f"  PASS  {n:>4}  {title}" + (f" — {note}" if note else ""))
        except Exception as e:
            results.append({"step": n, "title": title, "result": "FAIL",
                            "note": f"{type(e).__name__}: {e}",
                            "seconds": round(time.time() - t0, 2)})
            print(f"  FAIL  {n:>4}  {title} — {type(e).__name__}: {e}")
            if args.verbose:
                traceback.print_exc()

    print(f"RIFTWIRE acceptance · data dir {data_dir} · port {args.port}")
    media = make_media(media_dir)
    stack.migrate()

    step("1", "Start the backend", lambda: (stack.start_api(), "API healthy")[1])
    step("2", "Start the worker", lambda: (stack.start_worker(), "worker process running")[1])

    def s3():
        d = stack.get("/api/v1/system/status").json()
        check(d["worker"]["state"] == "RUNNING", f"worker state {d['worker']['state']}")
        check(d["readiness"]["ready"], f"not ready: {d['readiness']}")
        return f"ready; ffmpeg: {d['ffmpeg']['version']}; videotoolbox={d['ffmpeg']['videotoolbox']}"
    step("3", "Backend ready and worker reporting heartbeats", s3)

    def s4():
        r = stack.get("/")
        check(r.status_code == 200 and "<div id=\"root\">" in r.text,
              "dashboard not served (build it: cd frontend && npm run build)")
        assets = [ln.split('"')[1] for ln in r.text.split() if ln.startswith('src="/assets/')]
        for a in assets:
            check(stack.get(a).status_code == 200, f"asset {a} not served")
        check(stack.get("/recordings/some-deep-link").status_code == 200, "SPA fallback")
        return f"dashboard + {len(assets)} asset(s) served"
    step("4", "Open the local dashboard (served by the API)", s4)

    def s5():
        prog: list = []
        r = upload(stack, media["valid"], "GTA synthetic heist.mp4", prog)
        check(r.status_code == 201, f"HTTP {r.status_code}: {r.text}")
        ctx["rid"] = r.json()["recording"]["id"]
        ctx["upload_progress_samples"] = len(prog)
        return f"HTTP 201, recording {ctx['rid'][:8]}…, {media['valid'].stat().st_size} bytes"
    step("5", "Import a valid test video", s5)

    def s6():
        obs: list = []
        ctx["detail"] = wait_ready(stack, ctx["rid"], observe=obs)
        seen = {s for o in obs for v in o.values() for s in [v[0]]}
        return (f"upload streamed in {ctx['upload_progress_samples']} chunks; processing states "
                f"observed: {sorted(seen)}")
    step("6", "Observe import + processing progress", s6)

    def s7():
        d = ctx["detail"]
        stored = data_dir / "recordings" / d["stored_filename"]
        check(stored.exists(), "stored file missing")
        check(sha256(stored) == sha256(media["valid"]) == d["sha256"], "hash mismatch")
        check(not stored.stat().st_mode & stat.S_IWUSR, "stored file should be read-only")
        check(temp_contents(data_dir) == [], "temp dir not empty")
        return f"recordings/{d['stored_filename']} (SHA-256 verified, read-only)"
    step("7", "Recording is stored", s7)

    def s8():
        d = ctx["detail"]
        probe = ffprobe_json(media["valid"])
        v = next(s for s in probe["streams"] if s["codec_type"] == "video")
        check((d["width"], d["height"]) == (v["width"], v["height"]), "resolution")
        check(d["fps_rational"] == v["r_frame_rate"] == "30000/1001", "frame rate")
        check(abs(d["duration_ms"] - float(probe["format"]["duration"]) * 1000) < 50, "duration")
        check(d["video_codec"] == "h264" and d["audio_codec"] == "aac" and d["has_audio"], "codecs")
        return (f"{d['width']}x{d['height']} @ {d['fps_rational']}, {d['duration_ms']} ms, "
                f"{d['video_codec']}/{d['audio_codec']}")
    step("8", "Metadata extracted correctly (vs. independent ffprobe)", s8)

    def s9():
        r = stack.get(f"/api/v1/recordings/{ctx['rid']}/thumbnail")
        check(r.headers["content-type"] == "image/jpeg", r.headers["content-type"])
        p = tmp / "thumb.jpg"
        p.write_bytes(r.content)
        s = ffprobe_json(p)["streams"][0]
        check((s["width"], s["height"]) == (480, 270), f"{s['width']}x{s['height']}")
        return "480x270 JPEG (16:9 preserved)"
    step("9", "Thumbnail generated", s9)

    def s10():
        lst = stack.get("/api/v1/recordings").json()
        check(any(i["id"] == ctx["rid"] for i in lst["items"]), "not listed")
        return f"{lst['total']} recording(s) listed"
    step("10", "Recording appears in the dashboard API", s10)

    def s11():
        jobs = ctx["detail"]["jobs"]
        check([j["status"] for j in jobs] == ["COMPLETED"] * 3, str(jobs))
        return ", ".join(f"{j['job_type']}={j['status']}" for j in jobs)
    step("11", "Processing jobs reach COMPLETED", s11)

    def s12():
        stack.stop()
        stack.start_api()
        stack.start_worker()
        return "API and worker restarted (SIGTERM → start)"
    step("12", "Restart the application and worker", s12)

    def s13():
        d = stack.get(f"/api/v1/recordings/{ctx['rid']}").json()
        check(d["status"] == "READY" and len(d["jobs"]) == 3, "state lost")
        check(stack.get("/api/v1/jobs").json()["total"] == 3, "job history lost")
        return "recording + 3 completed jobs persisted"
    step("13", "Recordings and job history persist", s13)

    def s14_15():
        r = upload(stack, media["valid"], "same video renamed.mp4")
        check(r.status_code == 409, f"HTTP {r.status_code}")
        err = r.json()["error"]
        check(err["code"] == "duplicate_recording", err["code"])
        check(err["details"]["existing_recording_id"] == ctx["rid"], "wrong existing id")
        check(stack.get("/api/v1/recordings").json()["total"] == 1, "duplicate stored")
        return f"HTTP 409 — {err['message']}"
    step("14-15", "Import the same video again → duplicate detected", s14_15)

    def s16_17():
        r = upload(stack, media["invalid"])
        check(r.status_code == 422 and r.json()["error"]["code"] == "invalid_media",
              f"HTTP {r.status_code}")
        r2 = upload(stack, media["unsupported"])
        check(r2.status_code == 415, f"HTTP {r2.status_code}")
        check(temp_contents(data_dir) == [], "temp not cleaned")
        check(stack.get("/api/v1/recordings").json()["total"] == 1, "invalid file stored")
        return f"422 — {r.json()['error']['message'][:70]}…; .txt → 415"
    step("16-17", "Import an invalid file → safe rejection, clear error", s16_17)

    def s_interrupt_upload():
        size = media["valid"].stat().st_size
        with socket.create_connection(("127.0.0.1", args.port)) as s:
            s.sendall((f"POST /api/v1/recordings/import?filename=cut.mp4 HTTP/1.1\r\n"
                       f"Host: 127.0.0.1\r\nContent-Type: application/octet-stream\r\n"
                       f"Content-Length: {size}\r\n\r\n").encode())
            with open(media["valid"], "rb") as f:
                s.sendall(f.read(size // 3))
            time.sleep(0.5)
        deadline = time.time() + 10
        while time.time() < deadline and temp_contents(data_dir):
            time.sleep(0.2)
        check(temp_contents(data_dir) == [], "partial upload left in temp/")
        check(stack.get("/api/v1/recordings").json()["total"] == 1, "partial upload stored")
        ev = stack.get("/api/v1/events", params={"limit": 200}).json()["items"]
        check(any(e["event_type"] == "upload_interrupted" for e in ev), "no event recorded")
        return "client disconnected at 33% → partial file removed, event logged"
    step("X1", "Interrupted upload is cleaned up safely", s_interrupt_upload)

    def s_interrupt_job():
        # Worker with a test-only slow FFmpeg child inside THUMBNAIL; SIGKILL it mid-job.
        stack.stop(("worker",))
        stack.start_worker(RIFTWIRE_TEST_SLOW_FFMPEG_SECONDS="60")
        r = upload(stack, media["noaudio"])
        check(r.status_code == 201, f"HTTP {r.status_code}: {r.text}")
        rid = r.json()["recording"]["id"]
        deadline, pid = time.time() + 60, None
        while time.time() < deadline and not pid:
            for j in stack.get(f"/api/v1/recordings/{rid}").json()["jobs"]:
                if j["job_type"] == "THUMBNAIL" and j["status"] == "RUNNING":
                    pid = _job_pid(data_dir, j["id"])
            time.sleep(0.3)
        check(pid, "THUMBNAIL job never started its FFmpeg child")
        stack.worker.send_signal(signal.SIGKILL)
        stack.worker.wait(10)
        stack.worker = None
        time.sleep(0.5)
        orphan_alive = _alive(pid)
        check(orphan_alive, "expected an orphaned FFmpeg after SIGKILL")
        stack.start_worker()  # normal worker: must kill the orphan, then retry
        d = wait_ready(stack, rid, timeout=120)
        check(not _alive(pid), "orphaned FFmpeg still running")
        thumb = next(j for j in d["jobs"] if j["job_type"] == "THUMBNAIL")
        check(thumb["attempt_count"] == 2, f"attempts {thumb['attempt_count']}")
        ev = stack.get("/api/v1/events", params={"limit": 200}).json()["items"]
        rec_ev = [e for e in ev if e["event_type"] == "lease_recovered"
                  and e["related_recording_id"] == rid]
        check(rec_ev and "orphan process: killed" in rec_ev[0]["message"], str(rec_ev))
        check(not d["has_audio"] and d["video_codec"] == "vp9", "webm/no-audio metadata")
        return ("worker SIGKILLed mid-THUMBNAIL; orphan FFmpeg killed on restart; job completed "
                "on attempt 2; WEBM without audio handled")
    step("X2", "Interrupted processing job recovers (crash + orphan FFmpeg)", s_interrupt_job)

    def s_disk():
        stack.stop(("api",))
        stack.start_api(RIFTWIRE_MIN_FREE_DISK_BYTES=str(10**18))
        r = upload(stack, media["noaudio"], "space-test.webm")
        stack.stop(("api",))
        stack.start_api()
        check(r.status_code == 507 and r.json()["error"]["code"] == "insufficient_storage",
              f"HTTP {r.status_code}")
        return f"HTTP 507 — {r.json()['error']['message'][:80]}"
    step("X3", "Import refused when free space would drop below the floor", s_disk)

    def s_logs():
        logs = data_dir / "logs"
        for name in ("api.log", "worker.log"):
            line = (logs / name).read_text().strip().splitlines()[-1]
            rec = json.loads(line)
            check({"timestamp", "level", "component", "message"} <= set(rec), name)
        return "api.log and worker.log are structured JSON"
    step("X4", "Structured logs written", s_logs)

    # ------------------------------------------------------------------ storage (M1)
    marker_name = ".riftwire-location.json"

    def extra_clip(name: str, seconds: float) -> Path:
        p = media_dir / name
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={seconds}",
               "-f", "lavfi", "-i", f"sine=frequency=550:duration={seconds}", "-c:v",
               "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p))
        return p

    def s_locations():
        st = stack.get("/api/v1/storage").json()
        kinds = {loc["kind"]: loc for loc in st["locations"]}
        check(set(kinds) == {"recordings", "temp", "models", "exports", "assets"}, str(kinds))
        check(all(loc["online"] for loc in kinds.values()), "a location is offline")
        for k in kinds:
            check((data_dir / k / marker_name).is_file(), f"no identity marker in {k}")
        ready = stack.get("/health/ready").json()["checks"]["storage"]
        check(ready["ok"], str(ready))
        return f"5 locations online with identity markers; {len(st['volumes'])} volume(s)"
    step("M1", "Storage locations registered and verified", s_locations)

    def s_offline():
        rec_dir = data_dir / "recordings"
        marker = rec_dir / marker_name
        hidden = data_dir / "recordings-marker.hidden"
        # (a) Writes refused while a needed location is offline; nothing is written.
        marker.rename(hidden)
        before = sorted(p.name for p in (data_dir / "temp").iterdir())
        r = upload(stack, extra_clip("offline-a.mp4", 2), "offline-a.mp4")
        check(r.status_code == 503 and r.json()["error"]["code"] == "storage_offline",
              f"upload while offline: HTTP {r.status_code}")
        check(sorted(p.name for p in (data_dir / "temp").iterdir()) == before,
              "temp changed while offline")
        rd = httpx.get(stack.base + "/health/ready", timeout=10)
        check(rd.status_code == 503 and "storage_offline:recordings"
              in rd.json()["checks"]["storage"]["offline"], "readiness did not report offline")
        hidden.rename(marker)
        # (b) A queued job waits (no attempt used) while its drive is gone, then completes.
        stack.stop(("worker",))
        r = upload(stack, extra_clip("offline-b.mp4", 3), "offline-b.mp4")
        check(r.status_code == 201, f"upload: HTTP {r.status_code}")
        rid = r.json()["recording"]["id"]
        marker.rename(hidden)
        stack.start_worker()
        time.sleep(4)
        jobs = stack.get(f"/api/v1/recordings/{rid}").json()["jobs"]
        ingest_job = next(j for j in jobs if j["job_type"] == "INGEST")
        check(ingest_job["status"] == "PENDING" and ingest_job["attempt_count"] == 0,
              f"job ran while storage offline: {ingest_job['status']} "
              f"attempts={ingest_job['attempt_count']}")
        check(not any(p.name.startswith(rid) for p in rec_dir.iterdir()),
              "a file was written to the offline location")
        hidden.rename(marker)
        d = wait_ready(stack, rid, timeout=60)
        ingest_job = next(j for j in d["jobs"] if j["job_type"] == "INGEST")
        check(ingest_job["attempt_count"] == 1, f"attempts {ingest_job['attempt_count']}")
        ev = stack.get("/api/v1/events", params={"limit": 200}).json()["items"]
        types = {e["event_type"] for e in ev}
        check({"storage_offline", "storage_online"} <= types, str(sorted(types)))
        return ("upload refused (503) with nothing written; INGEST waited with 0 attempts, "
                "then completed on attempt 1 after the drive returned")
    step("M2", "Offline location: writes refused, jobs wait, recovery on return", s_offline)

    def s_migration():
        rec_dir = data_dir / "recordings"
        files = {p.name: sha256(p) for p in rec_dir.iterdir()
                 if p.is_file() and p.name != marker_name}
        check(files, "no recordings to migrate")
        dest = tmp / "moved" / "recordings"
        tool = [str(PY), str(BACKEND.parent / "scripts" / "migrate_storage.py"), "recordings",
                str(dest)]
        dry = subprocess.run(tool, env=stack.env, capture_output=True, text=True, timeout=120)
        check(dry.returncode == 0 and "DRY RUN" in dry.stdout, dry.stdout + dry.stderr)
        check(not dest.exists() and not dest.parent.exists(), "dry run created folders")
        refused = subprocess.run([*tool, "--execute"], env=stack.env, capture_output=True,
                                 text=True, timeout=120)
        check(refused.returncode == 1 and "running" in refused.stdout, refused.stdout)
        stack.stop()
        ex = subprocess.run([*tool, "--execute"], env=stack.env, capture_output=True,
                            text=True, timeout=600)
        check(ex.returncode == 0, ex.stdout + ex.stderr)
        moved = {p.name: sha256(p) for p in dest.iterdir()
                 if p.is_file() and p.name != marker_name}
        check(moved == files, "destination differs from source")
        kept = {p.name: sha256(p) for p in rec_dir.iterdir()
                if p.is_file() and p.name != marker_name}
        check(kept == files, "originals changed or deleted")
        stack.start_api()
        stack.start_worker()
        st = {x["kind"]: x for x in stack.get("/api/v1/storage").json()["locations"]}
        # The tool stores the canonical path (macOS: /var -> /private/var).
        check(st["recordings"]["online"]
              and st["recordings"]["path"] == os.path.realpath(dest), str(st["recordings"]))
        r = upload(stack, extra_clip("after-move.mp4", 2), "after-move.mp4")
        check(r.status_code == 201, f"HTTP {r.status_code}")
        rid = r.json()["recording"]["id"]
        wait_ready(stack, rid, timeout=60)
        check(any(p.name.startswith(rid) for p in dest.iterdir()), "new import not on new path")
        check(not any(p.name.startswith(rid) for p in rec_dir.iterdir()),
              "new import written to the old location")
        return (f"dry run wrote nothing; refused while running; moved {len(files)} file(s) "
                "byte-identical; originals kept; new imports land in the new location")
    step("M3", "Verified migration of the recordings location", s_migration)

    # ------------------------------------------------------------------ M2 rights & sources
    clip = "https://www.twitch.tv/acceptancestreamer/clip/HeistFinale-Xy_9?filter=clips"
    attest = "Acceptance test: synthetic licence for a fictional creator."
    scope = {"v": 1, "uses": ["acquire", "analyze", "edit", "publish"],
             "acquisition_methods": ["local_recording", "creator_file", "platform_export"],
             "publish_platforms": ["youtube_shorts"], "brand_channels": "*"}
    m2: dict = {}

    def ok(r, code=200):
        check(r.status_code == code, f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def s_m2_setup():
        m2["game"] = ok(stack.post("/api/v1/games", json={"slug": "test-game",
                                                          "name": "Test Game"}), 201)["id"]
        m2["brand"] = ok(stack.post("/api/v1/brand-channels", json={
            "slug": "test-brand", "name": "Test Brand", "platform": "youtube_shorts"}),
            201)["id"]
        cr = ok(stack.post("/api/v1/creators", json={"display_name": "Acceptance Streamer",
                                                     "relationship": "partner"}), 201)
        m2["creator"] = cr["id"]
        m2["twitch"] = ok(stack.post(f"/api/v1/creators/{cr['id']}/channels", json={
            "platform": "twitch", "handle": "AcceptanceStreamer"}), 201)["id"]
        m2["local"] = ok(stack.post(f"/api/v1/creators/{cr['id']}/channels",
                                    json={"platform": "local"}), 201)["id"]
        lic = ok(stack.post("/api/v1/rights", json={
            "creator_id": cr["id"], "basis": "licence", "coverage": "creator", "scope": scope,
            "attestation_text": attest}), 201)
        check(lic["state"] == "ok" and lic["version"] == 1, str(lic))
        m2["licence"] = lic["id"]
        return "game, brand channel, creator with 2 channels, standing licence (v1, attested)"
    step("M2-1", "Configure catalog, creator and a standing licence", s_m2_setup)

    def s_m2_paste():
        res = ok(stack.post("/api/v1/sources/resolve", json={"url": clip}))
        check(res["match"]["status"] == "matched"
              and res["channel"]["id"] == m2["twitch"] and res["covered"], str(res))
        src = ok(stack.post("/api/v1/sources", json={
            "url": clip, "game_id": m2["game"], "brand_channel_ids": [m2["brand"]]}), 201)
        check(src["rights_status"] == "active" and src["creator_channel_id"] == m2["twitch"],
              str(src))
        dup = stack.post("/api/v1/sources",
                         json={"url": "https://clips.twitch.tv/HeistFinale-Xy_9"})
        check(dup.status_code == 409
              and dup.json()["error"]["details"]["source_id"] == src["id"], dup.text)
        m2["clip_source"] = src["id"]
        return "creator matched from the link; covered by the licence; variant URL deduplicated"
    step("M2-2", "Paste a URL: automatic match, rights shown, duplicates refused", s_m2_paste)

    def s_m2_import():
        ids = []
        for i in (1, 2):
            p = extra_clip(f"licensed-{i}.mp4", 2 + i / 10)  # unique bytes
            r = ok(upload(stack, p, p.name, params={"creator_channel_id": m2["local"]}), 201)
            check(r["source_id"] and r["source_error"] is None, str(r))
            ids.append(r["source_id"])
            wait_ready(stack, r["recording"]["id"], timeout=60)
        d = ok(stack.get(f"/api/v1/sources/{ids[0]}"))
        check(d["analyze"]["allowed"] and d["recording_count"] == 1, str(d["analyze"]))
        prov = ok(stack.get(f"/api/v1/sources/{ids[0]}/provenance"))
        acts = {(p["entity"], p["action"]) for p in prov}
        for need in [("creator", "created"), ("rights", "attested"), ("source", "created"),
                     ("recording", "attached_to_source")]:
            check(need in acts, f"missing provenance {need}")
        recs = [r for r in ok(stack.get(f"/api/v1/creators/{m2['creator']}/rights"))
                if r["basis"] != "public_web"]      # legacy rows are never permission
        check(len(recs) == 1, f"{len(recs)} licences")
        return ("2 imports linked automatically under one standing licence (no new "
                "attestation); provenance chain creator → rights → source → recording")
    step("M2-3", "Import under a standing licence: linked with provenance", s_m2_import)

    def s_m2_revoke():
        rv = ok(stack.post(f"/api/v1/rights/{m2['licence']}/revoke",
                           json={"reason": "acceptance: creator withdrew"}))
        check(rv["state"] == "revoked", str(rv))
        chk = ok(stack.get(f"/api/v1/sources/{m2['clip_source']}/rights-check",
                           params={"use": "edit", "clip_ms": 1000}))
        check(not chk["allowed"] and chk["reason"] == "revoked", str(chk))
        d = ok(stack.get(f"/api/v1/sources/{m2['clip_source']}"))
        check(d["rights_status"] == "revoked" and not d["analyze"]["allowed"],
              f"{d['rights_status']} {d['analyze']}")
        before = ok(stack.get("/api/v1/recordings"))["total"]
        p = extra_clip("after-revoke.mp4", 2.5)
        r = upload(stack, p, p.name, params={"creator_channel_id": m2["local"]})
        check(r.status_code == 403 and r.json()["error"]["code"] == "rights_refused", r.text)
        check(ok(stack.get("/api/v1/recordings"))["total"] == before, "recording created")
        h = ok(upload(stack, extra_clip("housekeeping.mp4", 2.7), "housekeeping.mp4"), 201)
        wait_ready(stack, h["recording"]["id"], timeout=60)
        check(h["recording"]["source_id"] is None, "unlinked import got a source")
        return ("revoked immediately: editing refused (reason: revoked), licensed import "
                "refused before any byte; public availability does not keep the link usable; "
                "plain imports (housekeeping) still work, unlinked")
    step("M2-4", "Revocation: refused everywhere, housekeeping continues", s_m2_revoke)

    def s_m2_second():
        lic = ok(stack.post("/api/v1/rights", json={
            "creator_id": m2["creator"], "basis": "licence", "coverage": "creator",
            "scope": scope, "attestation_text": attest + " (renewed)"}), 201)
        m2["licence2"] = lic["id"]
        vod = ok(stack.post("/api/v1/sources", json={
            "url": "https://www.twitch.tv/videos/987654321",
            "creator_channel_id": m2["twitch"]}), 201)
        check(vod["rights_status"] == "active" and "analyze" in vod["authorized_uses"], str(vod))
        m2["vod"] = vod["id"]
        clip_now = ok(stack.get(f"/api/v1/sources/{m2['clip_source']}/rights-check",
                                params={"use": "edit", "clip_ms": 1000}))
        check(clip_now["allowed"] and clip_now["record_id"] == lic["id"], str(clip_now))
        n = len([r for r in ok(stack.get(f"/api/v1/creators/{m2['creator']}/rights"))
                 if r["basis"] != "public_web"])
        check(n == 2, f"{n} licences")
        return "one new licence covers the existing and a new source; no per-source attestation"
    step("M2-5", "A standing licence covers further sources automatically", s_m2_second)

    def s_m2_history():
        new = ok(stack.post(f"/api/v1/rights/{m2['licence2']}/supersede", json={
            "scope": {**scope, "uses": ["acquire", "analyze"]},
            "attestation_text": attest + " (narrowed)"}), 201)
        check(new["version"] == 2, str(new))
        old = ok(stack.get(f"/api/v1/rights/{m2['licence2']}"))
        check(old["state"] == "superseded", old["state"])
        chk = ok(stack.get(f"/api/v1/sources/{m2['clip_source']}/rights-check",
                           params={"use": "edit", "clip_ms": 1000}))
        check(not chk["allowed"] and chk["reason"] == "use_not_in_scope", str(chk))
        import sqlite3
        con = sqlite3.connect(str(data_dir / "riftwire.db"), timeout=10)
        refused = 0
        lid = (m2["licence"],)
        for sql, args in (("UPDATE rights_records SET scope_json='{}' WHERE id=?", lid),
                          ("DELETE FROM rights_records WHERE id=?", lid),
                          ("UPDATE rights_records SET revoked_at=NULL WHERE id=?", lid),
                          ("DELETE FROM provenance_events", ()),
                          ("UPDATE provenance_events SET action='x'", ())):
            try:
                con.execute(sql, args)
                con.commit()
            except sqlite3.DatabaseError:
                con.rollback()
                refused += 1
        con.close()
        check(refused == 5, f"only {refused}/5 tampering attempts refused by the database")
        return ("edit = new version (v2), old one superseded and powerless; direct database "
                "edits/deletes of attested rights and provenance refused (5/5)")
    step("M2-6", "Supersession and immutable history", s_m2_history)

    def s_m2_acquire():
        """Public availability is not permission (owner decision 2026-11-25). A saved link
        with no permission is saved, explained and not fetched. Once the creator's standing
        permission (acquire + analyze by url_fetch) has written platform permission
        attached, a newly saved link is queued without another prompt and becomes a linked
        recording; editing stays refused. Real API and worker processes; the platform
        downloader is a stand-in script writing a synthetic clip; no platform is
        contacted."""
        fake_bin = tmp / "fake-bin"
        fake_bin.mkdir()
        clip_file = extra_clip("acquired-vod.mp4", 2.9)
        fake = fake_bin / "yt-dlp"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import shutil, sys\n"
            "a = sys.argv[1:]\n"
            "if '--version' in a:\n"
            "    print('acceptance-stand-in'); sys.exit(0)\n"
            f"shutil.copyfile({str(clip_file)!r}, a[a.index('-o') + 1])\n")
        fake.chmod(0o755)
        env = {"RIFTWIRE_URL_ACQUISITION_ENABLED": "true",
               "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
        stack.stop()
        stack.start_api(**env)
        stack.start_worker(**env)
        other = ok(stack.post("/api/v1/creators", json={"display_name": "Public Streamer",
                                                        "relationship": "unaffiliated"}), 201)
        ch = ok(stack.post(f"/api/v1/creators/{other['id']}/channels", json={
            "platform": "twitch", "handle": "PublicStreamer"}), 201)
        # 1. Public link, no permission: saved and explained, nothing fetched.
        bare = ok(stack.post("/api/v1/sources", json={
            "url": "https://www.twitch.tv/videos/555000110",
            "creator_channel_id": ch["id"]}), 201)
        d = ok(stack.get(f"/api/v1/sources/{bare['id']}"))
        check(d["rights_status"] == "public_web" and d["records"] == []
              and d["acquire_job"] is None, f"{d['rights_status']} {d['acquire_job']}")
        check(not d["acquire"]["allowed"] and d["acquire"]["reason"] == "no_rights_record"
              and d["acquire"]["action"], str(d["acquire"]))
        refused = stack.post(f"/api/v1/sources/{bare['id']}/acquire")
        check(refused.status_code == 403, refused.text)
        # 2. The owner records the creator's standing permission, with platform permission.
        perm = ok(stack.post("/api/v1/rights", json={
            "creator_id": other["id"], "basis": "creator_permission", "coverage": "creator",
            "scope": {"v": 1, "uses": ["acquire", "analyze"],
                      "acquisition_methods": ["url_fetch"]},
            "attestation_text": "Acceptance: synthetic creator permission for URL fetching."}),
            201)
        ok(stack.post(f"/api/v1/rights/{perm['id']}/evidence",
                      params={"kind": "platform_permission",
                              "reference": "Acceptance: synthetic platform permission"}), 201)
        # 3. A newly saved link is queued with no further confirmation.
        src = ok(stack.post("/api/v1/sources", json={
            "url": "https://www.twitch.tv/videos/555000111",
            "creator_channel_id": ch["id"]}), 201)
        sid = src["id"]
        d = ok(stack.get(f"/api/v1/sources/{sid}"))
        check(d["acquire_job"] is not None, "standing permission did not queue the link")
        deadline = time.time() + 90
        while time.time() < deadline:
            d = ok(stack.get(f"/api/v1/sources/{sid}"))
            if d["acquire_job"]["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.5)
        job = d["acquire_job"]
        check(job["status"] == "COMPLETED", f"ACQUIRE {job['status']}: {job['last_error']}")
        rec = wait_ready(stack, job["recording_id"], timeout=60)
        check(rec["source_id"] == sid and rec["acquisition_method"] == "url_fetch",
              f"{rec['source_id']} {rec['acquisition_method']}")
        again = ok(stack.post(f"/api/v1/sources/{sid}/acquire"), 202)
        check(again["job"]["id"] == job["id"], "a second ACQUIRE job was created")
        prov = ok(stack.get(f"/api/v1/sources/{sid}/provenance"))
        acts = [p["action"] for p in prov]
        for need in ("acquired_by_url", "attached_to_source"):
            check(need in acts, f"missing provenance {need}")
        check("recorded_public_web" not in acts, "a public_web record was written")
        scan = stack.post(f"/api/v1/recordings/{rec['id']}/highlight-scan")
        check(scan.status_code == 200, f"fetched footage not analysable: {scan.text[:300]}")
        chk = ok(stack.get(f"/api/v1/sources/{sid}/rights-check",
                           params={"use": "edit", "clip_ms": 1000}))
        check(not chk["allowed"] and chk["reason"] == "use_not_in_scope", str(chk))
        check(ok(stack.get(f"/api/v1/sources/{sid}"))["recording_count"] == 1,
              "more than one recording for the source")
        check(ok(stack.get(f"/api/v1/sources/{bare['id']}"))["acquire_job"] is None,
              "the earlier link was queued without being asked")
        leftovers = list((data_dir / "temp").glob("source-download-*"))
        check(not leftovers, f"download workspace left behind: {leftovers}")
        return ("public link without permission: saved, explained (no_rights_record), not "
                "fetched → creator permission + platform permission recorded → next link "
                "queued without a prompt → recording READY, url_fetch; editing refused "
                "(use_not_in_scope); stand-in downloader, no platform contacted")
    step("M2-7", "Public availability is not permission; standing permission fetches",
         s_m2_acquire)

    stack.stop()
    passed = sum(r["result"] == "PASS" for r in results)
    summary = {
        "passed": passed, "failed": len(results) - passed, "total": len(results),
        "platform": platform.platform(), "python": sys.version.split()[0],
        "ffmpeg": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        .stdout.splitlines()[0],
        "min_free_gib": args.min_free_gib, "results": results,
    }
    print(f"\n{passed}/{len(results)} steps passed")
    if args.report:
        Path(args.report).write_text(json.dumps(summary, indent=2))
        print(f"report: {args.report}")
    if args.keep:
        print(f"kept: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if passed == len(results) else 1


def _job_pid(data_dir: Path, job_id: str) -> int | None:
    import sqlite3
    con = sqlite3.connect(f"file:{data_dir / 'riftwire.db'}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute("SELECT process_pid FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--report", help="write JSON results here")
    ap.add_argument("--min-free-gib", type=float, default=60.0,
                    help="free-space floor for the isolated run (default 60, as in production)")
    ap.add_argument("--keep", action="store_true", help="keep the temporary data directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    sys.exit(run(ap.parse_args()))
