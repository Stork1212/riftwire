#!/usr/bin/env python3
"""RIFTWIRE Phase 1 end-to-end acceptance test (synthetic media only).

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


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def upload(stack: Stack, path: Path, name: str | None = None, progress: list | None = None):
    size = path.stat().st_size

    def body():
        sent = 0
        with open(path, "rb") as f:
            while chunk := f.read(256 * 1024):
                sent += len(chunk)
                if progress is not None:
                    progress.append(round(sent * 100 / size))
                yield chunk

    return stack.post("/api/v1/recordings/import", params={"filename": name or path.name},
                      content=body(), headers={"Content-Length": str(size),
                                               "Content-Type": "application/octet-stream"})


def wait_ready(stack: Stack, rid: str, timeout=120, observe: list | None = None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = stack.get(f"/api/v1/recordings/{rid}").json()
        if observe is not None:
            observe.append({j["job_type"]: (j["status"], j["progress_percent"]) for j in d["jobs"]})
        if (len(d["jobs"]) == 3 and all(j["status"] == "COMPLETED" for j in d["jobs"])
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
        check(list((data_dir / "temp").iterdir()) == [], "temp dir not empty")
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
        check(list((data_dir / "temp").iterdir()) == [], "temp not cleaned")
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
        while time.time() < deadline and list((data_dir / "temp").iterdir()):
            time.sleep(0.2)
        check(list((data_dir / "temp").iterdir()) == [], "partial upload left in temp/")
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
