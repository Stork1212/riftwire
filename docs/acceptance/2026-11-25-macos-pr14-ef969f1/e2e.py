"""End-to-end: public link refused -> creator + platform permission (synthetic) -> saved URL
-> ACQUIRE -> recording -> highlight scan -> clips.txt -> 35/65 Short with the approved
RumbleFrame outro (PR #6, continue mode) and a second Short with the v1 launch-kit outro
(PR #11, no audio track, outro mode)."""
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
OUTRO_PR6, OUTRO_V1 = Path(sys.argv[5]), Path(sys.argv[6])
BACKEND = REPO / "backend"
PY = sys.executable
PORT = int(os.environ.get("E2E_PORT", "18941"))
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
    fake = BIN / "yt-dlp"
    fake.write_text(f"#!{PY}\nimport shutil, sys\na = sys.argv[1:]\n"
                    "if '--version' in a:\n    print('e2e-stand-in'); sys.exit(0)\n"
                    f"shutil.copyfile({str(src)!r}, a[a.index('-o') + 1])\n")
    fake.chmod(0o755)
    return src


SRC = make_media()
ENV = {k: v for k, v in os.environ.items() if not k.startswith("RIFTWIRE_")}
ENV.update({"RIFTWIRE_DATA_DIR": str(DATA), "RIFTWIRE_API_PORT": str(PORT),
            "RIFTWIRE_URL_ACQUISITION_ENABLED": "true",
            "RIFTWIRE_CLIP_WHISPER_PYTHON": WHISPER_PY,
            "RIFTWIRE_CLIP_WHISPER_MODEL": WHISPER_MODEL,
            "RIFTWIRE_CLIP_OUTRO_PATH": str(OUTRO_PR6),
            "RIFTWIRE_CLIP_OUTRO_DURATION_SECONDS": "2.5",
            "RIFTWIRE_CLIP_OUTRO_AUDIO": "continue",
            "RIFTWIRE_WORKER_POLL_SECONDS": "0.5",
            "PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"})
procs = []


def start(**over):
    ENV.update(over)
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
    procs.clear()


def ok(r, code=200):
    check(r.status_code == code, f"HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def get(path, **kw):
    return httpx.get(BASE + path, timeout=60, **kw)


def post(path, **kw):
    return httpx.post(BASE + path, timeout=900, **kw)


S: dict = {}


def step_refused():
    cr = ok(post("/api/v1/creators", json={"display_name": "E2E Test Streamer",
                                           "relationship": "unaffiliated"}), 201)
    ch = ok(post(f"/api/v1/creators/{cr['id']}/channels",
                 json={"platform": "twitch", "handle": "E2ETestStreamer"}), 201)
    S["creator"], S["channel"] = cr["id"], ch["id"]
    src = ok(post("/api/v1/sources", json={"url": "https://www.twitch.tv/videos/777000110",
                                          "creator_channel_id": ch["id"]}), 201)
    d = ok(get(f"/api/v1/sources/{src['id']}"))
    check(d["rights_status"] == "public_web" and d["records"] == [], d["rights_status"])
    check(d["authorized_uses"] == [] and d["acquire_job"] is None, str(d["acquire_job"]))
    acq = d["acquire"]
    check(not acq["allowed"] and acq["reason"] == "no_rights_record" and acq["action"], str(acq))
    r = post(f"/api/v1/sources/{src['id']}/acquire")
    check(r.status_code == 403, f"HTTP {r.status_code}")
    S["bare"] = src["id"]
    return (f"saved; classified public_web; no record written; not queued; acquire refused "
            f"(403 {acq['reason']}): \"{acq['action'][:70]}…\"")


def step_permission():
    perm = ok(post("/api/v1/rights", json={
        "creator_id": S["creator"], "basis": "creator_permission", "coverage": "creator",
        "scope": {"v": 1, "uses": ["acquire", "analyze"], "acquisition_methods": ["url_fetch"],
                  "max_clip_seconds": 60},
        "attestation_text": "E2E TEST ONLY: synthetic creator permission for a synthetic "
                            "test creator in an isolated instance."}), 201)
    ok(post(f"/api/v1/rights/{perm['id']}/evidence",
            params={"kind": "platform_permission",
                    "reference": "E2E TEST ONLY: synthetic written platform permission"}), 201)
    S["perm"] = perm["id"]
    return f"creator_permission {perm['id'][:8]} (acquire + analyze by url_fetch) + platform_permission evidence"


def step_acquire():
    src = ok(post("/api/v1/sources", json={"url": "https://www.twitch.tv/videos/777000111",
                                          "creator_channel_id": S["channel"]}), 201)
    S["source"] = src["id"]
    check(src["rights_status"] == "active", src["rights_status"])
    d = ok(get(f"/api/v1/sources/{S['source']}"))
    check(d["acquire_job"] is not None, "standing permission did not queue the saved link")
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
    check(ok(get(f"/api/v1/sources/{S['bare']}"))["acquire_job"] is None,
          "the unpermitted link was queued")
    S["rec"] = rid
    return f"queued on save (no prompt); ACQUIRE completed; recording {rid} READY, linked, url_fetch"


def step_history():
    prov = ok(get(f"/api/v1/sources/{S['source']}/provenance"))
    (OUT / "e2e-provenance.json").write_text(json.dumps(prov, indent=2))
    acts = {(p["entity"], p["action"]): p for p in prov}
    for need in (("source", "acquired_by_url"), ("recording", "attached_to_source")):
        check(need in acts, f"missing {need}")
    check(not any(a == "recorded_public_web" for _, a in acts), "a public_web record was written")
    att = acts[("recording", "attached_to_source")]["details"]
    check(att["method"] == "url_fetch" and att["rights_record_id"] == S["perm"], str(att))
    got = acts[("source", "acquired_by_url")]["details"]
    check(got["recording_id"] == S["rec"] and got["downloader"] == "yt-dlp", str(got))
    return (f"acquired_by_url ({got['downloader']} {got['downloader_version']}, sha256 "
            f"{got['sha256'][:12]}…) → attached_to_source (url_fetch, under the creator "
            "permission); no public_web record")


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
          r.json()["error"]["details"]["reason"] == "use_not_in_scope",
          f"export without edit permission was not refused: HTTP {r.status_code} {r.text[:200]}")
    ok(post(f"/api/v1/rights/{S['perm']}/supersede", json={
        "scope": {"v": 1, "uses": ["acquire", "analyze", "edit"],
                  "acquisition_methods": ["url_fetch"], "max_clip_seconds": 60},
        "attestation_text": "E2E TEST ONLY: synthetic creator permission now also allows "
                            "editing."}), 201)
    r = post(f"/api/v1/recordings/{S['rec']}/clip", params=params)
    check(r.status_code == 200 and r.headers["content-type"].startswith("video/mp4"),
          f"HTTP {r.status_code}: {r.text[:300]}")
    p = OUT / "e2e-short.mp4"
    p.write_bytes(r.content)
    S["short"] = p
    return (f"refused before edit permission (use_not_in_scope); exported {len(r.content)} "
            "bytes after the synthetic permission was superseded to include edit")


def frames(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True, timeout=300)
    return int(r.stdout.strip())


def peak_db(path, start, dur):
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{start}", "-t", f"{dur}",
                        "-i", str(path), "-vn", "-af",
                        "astats=measure_overall=Peak_level:measure_perchannel=none",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    lines = [ln for ln in r.stderr.splitlines() if "Peak level dB" in ln]
    return float(lines[-1].split(":")[-1])


def rms_db(samples):
    import math
    if not samples:
        return -120.0
    v = math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768
    return 20 * math.log10(v) if v > 0 else -120.0


def spec(path):
    info = probe(path)
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    return {"file": path.name, "bytes": path.stat().st_size,
            "duration_s": float(info["format"]["duration"]),
            "video": {k: v.get(k) for k in ("codec_name", "profile", "width", "height",
                                             "pix_fmt", "r_frame_rate", "duration")},
            "video_frames": frames(path),
            "audio": ({k: a[0].get(k) for k in ("codec_name", "sample_rate", "channels",
                                                 "duration")} if a else None)}


def step_short_format():
    sp = spec(S["short"])
    S.setdefault("specs", {})["short"] = sp
    length = (S["clip"][1] - S["clip"][0]) / 1000
    check((sp["video"]["width"], sp["video"]["height"]) == (1080, 1920), str(sp["video"]))
    check(sp["video"]["r_frame_rate"] == "30/1", sp["video"]["r_frame_rate"])
    check(sp["audio"] and sp["audio"]["sample_rate"] == "48000", str(sp["audio"]))
    check(abs(sp["duration_s"] - (length + 2.5)) <= 0.05,
          f"duration {sp['duration_s']:.3f} s, expected {length + 2.5:.1f}")
    check(sp["video_frames"] == round(length * 30) + 75, f"{sp['video_frames']} frames")
    av = abs(float(sp["audio"]["duration"]) - float(sp["video"]["duration"]))
    check(av <= 0.05, f"audio/video durations differ by {av:.3f} s")
    S["dur"] = sp["duration_s"]
    return (f"1080x1920 30 fps, AAC 48 kHz, {sp['duration_s']:.2f} s = {length:.0f} s clip + "
            f"2.5 s outro; {sp['video_frames']} frames; A/V within {av * 1000:.0f} ms")


def _mean_diff(a, b):
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _whole(path, index):
    """Frame ``index`` (0-based, decoded in order: exact, also for the last frame)."""
    return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vf",
                           f"select=eq(n\\,{index}),scale=270:480", "-fps_mode", "passthrough",
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True, timeout=120).stdout


def outro_matches(short, asset, clip_s):
    first = round(clip_s * 30)
    diffs = []
    for i in (0, 20, 37, 55, 74):
        a, b = _whole(short, first + i), _whole(asset, i)
        check(a and b, f"outro frame {i + 1} could not be decoded")
        diffs.append(round(_mean_diff(a, b), 2))
    before = _mean_diff(_whole(short, first - 1), _whole(asset, 0))
    return diffs, before


def step_outro():
    length = (S["clip"][1] - S["clip"][0]) / 1000
    diffs, before = outro_matches(S["short"], OUTRO_PR6, length)
    check(max(diffs) < 6, f"outro frames differ from the approved asset: {diffs}")
    check(before > 12, f"the last gameplay frame already looks like the outro ({before:.1f})")
    pk = peak_db(S["short"], length, 2.5)
    check(pk < 0.0, f"outro audio peaks at {pk:.2f} dBFS")
    shot = OUT / "e2e-outro-frame.png"
    ff("-ss", f"{length + 1.25:.3f}", "-i", str(S["short"]), "-frames:v", "1", str(shot))
    return (f"approved RumbleFrame outro (PR #6) intact: frames 1, 21, 38, 56, 75 match the "
            f"asset (mean |Δ| ≤ {max(diffs):.1f}/255); gameplay before it (Δ {before:.0f}); "
            f"outro audio peak {pk:.2f} dBFS (gameplay continues, sting mixed beneath)")


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
    over = _red(S["dur"] - 1.0)
    check(over < 50, f"red in the caption band over the outro ({over} px)")
    shot = OUT / "e2e-caption-frame.png"
    ff("-ss", f"{at:.3f}", "-i", str(S["short"]), "-frames:v", "1", str(shot))
    return (f"red highlight under the spoken word: {best} px at {at:.2f} s; none before "
            f"speech ({silent} px) or over the outro ({over} px) (e2e-caption-frame.png)")


def step_audio():
    start = S["clip"][0] / 1000
    probe_at = SPEECH_AT - start
    a = pcm(SRC, start + probe_at, 6)
    b = pcm(S["short"], probe_at, 6)
    best = max(corr(a[k:], b) for k in range(0, 400, 8))
    check(best >= 0.7, f"export audio does not match the source (r={best:.2f})")
    return f"export audio matches the source commentary (r={best:.2f}, 6 s from {probe_at:.1f} s)"


def step_v1_outro():
    stop()
    start(RIFTWIRE_CLIP_OUTRO_PATH=str(OUTRO_V1), RIFTWIRE_CLIP_OUTRO_AUDIO="outro",
          RIFTWIRE_CLIP_OUTRO_FADE_MS="0")
    start_ms, end_ms = S["clip"]
    r = post(f"/api/v1/recordings/{S['rec']}/clip",
             params={"start_ms": start_ms, "end_ms": end_ms, "layout": "split",
                     "title": "Getaway"})
    check(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}")
    p = OUT / "e2e-short-v1-silent-outro.mp4"
    p.write_bytes(r.content)
    sp = spec(p)
    S["specs"]["short_v1"] = sp
    length = (end_ms - start_ms) / 1000
    check(abs(sp["duration_s"] - (length + 2.5)) <= 0.05, f"{sp['duration_s']:.3f} s")
    check(sp["video_frames"] == round(length * 30) + 75, f"{sp['video_frames']} frames")
    diffs, _ = outro_matches(p, OUTRO_V1, length)
    check(max(diffs) < 6, f"outro frames differ from the v1 asset: {diffs}")
    silence = rms_db(pcm(p, length + 0.1, 2.3))
    check(silence < -60, f"outro is not silent ({silence:.1f} dB)")
    same = corr(pcm(p, 0.2, length - 0.4), pcm(S["short"], 0.2, length - 0.4))
    check(same > 0.99, f"gameplay audio differs between the two Shorts (r={same:.3f})")
    return (f"v1 launch-kit outro (no audio track), outro mode: {sp['duration_s']:.2f} s, "
            f"{sp['video_frames']} frames, outro matches the asset (≤ {max(diffs):.1f}), "
            f"outro audio {silence:.0f} dB (silence), gameplay audio identical (r={same:.3f})")


try:
    start()
    steps = [("public link without permission: saved, explained, not fetched", step_refused),
             ("standing creator permission + platform permission (synthetic)", step_permission),
             ("saved URL → queued acquisition → linked recording", step_acquire),
             ("acquisition history", step_history),
             ("Find highlights (queued scan)", step_scan),
             ("transcript evidence", step_transcript),
             ("sampled frame", step_frame),
             ("review a timestamp (clips.txt)", step_review),
             ("export 35/65 Short (edit permission gate)", step_export),
             ("Short format, duration and A/V sync", step_short_format),
             ("approved 2.5 s outro, not truncated", step_outro),
             ("red word highlights", step_red_words),
             ("original audio", step_audio),
             ("second Short: silent v1 outro without an audio track", step_v1_outro)]
    for name, fn in steps:
        if not record(name, fn) and name in ("saved URL → queued acquisition → linked recording",
                                             "standing creator permission + platform permission (synthetic)",
                                             "Find highlights (queued scan)",
                                             "export 35/65 Short (edit permission gate)"):
            log("stopping: later checks depend on this one")
            break
finally:
    stop()
    (OUT / "e2e-results.json").write_text(json.dumps({"checks": CHECKS}, indent=2))
    (OUT / "e2e-media-specs.json").write_text(json.dumps(S.get("specs", {}), indent=2))
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if CHECKS and all(c["result"] == "PASS" for c in CHECKS) and len(CHECKS) == 14 else 1)
