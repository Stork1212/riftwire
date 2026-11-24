"""Signal-extraction benchmark on synthetic video with known ground truth.

Builds a video from seeded segments of visually distinct FFmpeg test sources (so every
segment boundary is a known hard cut) with an audio bed plus loud bursts at known times.
Then measures, on the Mac:

* proxy creation throughput (downscale to 640 px wide, 30 fps, hardware encode on macOS),
* scene-cut detection on the proxy (FFmpeg ``scene`` score) → recall/precision at ±250 ms,
* loudness-burst detection (EBU R128 momentary loudness) → recall/precision at ±250 ms,
* wall time / realtime factor of each step.

No real footage is used; this validates the pipeline mechanics, not highlight quality.
"""

from __future__ import annotations

import json
import platform
import random
import re
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Visually distinct, temporally steady sources: every boundary is a hard cut and no source
# changes violently between its own frames (which would create false "cuts").
SOURCES = ("testsrc2", "smptehdbars", "rgbtestsrc", "testsrc", "pal100bars", "yuvtestsrc",
           "color=c=0x203040", "color=c=0xd0a060")
TOL_MS = 250


@dataclass(frozen=True)
class VideoSpec:
    seconds: float = 600
    width: int = 1920
    height: int = 1080
    fps: int = 60
    seed: int = 11


def encoder_args(bitrate: str = "12M") -> list[str]:
    if platform.system() == "Darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate, "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]


def _run(args: list[str], timeout: float = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args],
                          capture_output=True, text=True, timeout=timeout, check=True)


def plan_segments(spec: VideoSpec) -> tuple[list[tuple[str, float]], list[float], list[float]]:
    rng = random.Random(spec.seed)
    segs: list[tuple[str, float]] = []
    t, prev = 0.0, None
    lo, hi = (3.0, 20.0) if spec.seconds >= 60 else (1.5, 4.0)
    while t < spec.seconds - 0.5:
        src = rng.choice([s for s in SOURCES if s != prev])
        # Whole frames only, so cut times are exact at the source frame rate.
        frames = max(1, round(min(rng.uniform(lo, hi), spec.seconds - t) * spec.fps))
        dur = frames / spec.fps
        segs.append((src, dur))
        t += frames / spec.fps
        prev = src
    cuts, acc = [], 0.0
    for _, d in segs[:-1]:
        acc += d
        cuts.append(round(acc, 4))
    bursts, b = [], rng.uniform(2, 6) if spec.seconds < 60 else rng.uniform(5, 15)
    gap = (4.0, 8.0) if spec.seconds < 60 else (15.0, 45.0)
    while b < spec.seconds - 2:
        bursts.append(round(b, 3))
        b += rng.uniform(*gap)
    return segs, cuts, bursts


def build_video(spec: VideoSpec, out: Path) -> dict:
    segs, cuts, bursts = plan_segments(spec)
    truth = out.with_suffix(".truth.json")
    if out.exists() and truth.exists():
        return json.loads(truth.read_text())
    size = f"{spec.width}x{spec.height}"
    inputs: list[str] = []
    chains = []
    for i, (src, dur) in enumerate(segs):
        base = f"{src}:s={size}:r={spec.fps}" if src.startswith("color=") \
            else f"{src}=s={size}:r={spec.fps}"
        inputs += ["-f", "lavfi", "-t", f"{dur}", "-i", base]
        chains.append(f"[{i}:v]format=yuv420p,setsar=1,setpts=PTS-STARTPTS[v{i}]")
    n = len(segs)
    burst_expr = "+".join(f"between(t,{b},{b + 0.5})" for b in bursts) or "0"
    audio = (f"anoisesrc=color=pink:amplitude=0.03:seed={spec.seed}:duration={spec.seconds},"
             f"volume='1+25*({burst_expr})':eval=frame")
    inputs += ["-f", "lavfi", "-i", audio]
    graph = ";".join(chains) + ";" + "".join(f"[v{i}]" for i in range(n)) + \
        f"concat=n={n}:v=1:a=0[v]"
    t0 = time.perf_counter()
    tmp = out.with_suffix(".tmp.mp4")
    _run([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", f"{n}:a",
          *encoder_args(), "-c:a", "aac", "-b:a", "128k", "-t", str(spec.seconds),
          "-movflags", "+faststart", str(tmp)])
    tmp.replace(out)
    meta = {"spec": spec.__dict__, "cuts_s": cuts, "bursts_s": bursts,
            "segments": len(segs), "build_s": round(time.perf_counter() - t0, 2),
            "bytes": out.stat().st_size}
    truth.write_text(json.dumps(meta))
    return meta


def make_proxy(src: Path, out: Path) -> float:
    t0 = time.perf_counter()
    tmp = out.with_suffix(".tmp.mp4")
    _run(["-i", str(src), "-vf", "scale=640:-2,fps=30", *encoder_args("1500k"),
          "-c:a", "aac", "-b:a", "96k", str(tmp)])
    tmp.replace(out)
    return time.perf_counter() - t0


_PTS = re.compile(r"pts_time:([0-9.]+)")


def detect_cuts(video: Path, threshold: float = 0.3) -> tuple[list[float], float]:
    t0 = time.perf_counter()
    p = _run(["-i", str(video), "-an", "-vf",
              f"select='gt(scene,{threshold})',metadata=print:file=-", "-f", "null", "-"])
    # Ignore the first 100 ms (the score of the first frame is not a cut).
    times = [t for t in (float(m.group(1)) for m in _PTS.finditer(p.stdout)) if t > 0.1]
    return times, time.perf_counter() - t0


def detect_bursts(video: Path, jump_lu: float = 12.0) -> tuple[list[float], float]:
    """Onsets where momentary loudness rises ≥ jump_lu above the running median."""
    t0 = time.perf_counter()
    p = _run(["-i", str(video), "-vn", "-af",
              "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=-", "-f", "null", "-"])
    series: list[tuple[float, float]] = []
    t = None
    for line in p.stdout.splitlines():
        m = _PTS.search(line)
        if m:
            t = float(m.group(1))
        elif line.startswith("lavfi.r128.M=") and t is not None:
            v = float(line.split("=", 1)[1])
            if v > -120:
                series.append((t, v))
    base = statistics.median(v for _, v in series) if series else 0.0
    onsets, above, last = [], False, -10.0
    for tt, v in series:
        hit = v >= base + jump_lu
        if hit and not above and tt - last > 1.0:
            # Momentary loudness (400 ms window) is reported at 100 ms steps; the frame
            # timestamp at the first step above threshold is the onset estimate.
            onsets.append(round(tt, 3))
            last = tt
        above = hit
    return onsets, time.perf_counter() - t0


def match(truth: list[float], found: list[float], tol_ms: float = TOL_MS) -> dict:
    used: set[int] = set()
    errs = []
    for t in truth:
        best = None
        for j, f in enumerate(found):
            if j in used:
                continue
            d = abs(f - t) * 1000
            if d <= tol_ms and (best is None or d < best[1]):
                best = (j, d)
        if best:
            used.add(best[0])
            errs.append(best[1])
    tp = len(errs)
    return {"truth": len(truth), "found": len(found), "true_positives": tp,
            "recall": tp / len(truth) if truth else float("nan"),
            "precision": tp / len(found) if found else float("nan"),
            "median_error_ms": statistics.median(errs) if errs else float("nan")}


def run(spec: VideoSpec, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    stem = f"synthetic_{spec.width}x{spec.height}p{spec.fps}_{int(spec.seconds)}s_s{spec.seed}"
    src = work / f"{stem}.mp4"
    meta = build_video(spec, src)
    proxy = work / (src.stem + "_proxy.mp4")
    proxy_s = make_proxy(src, proxy)
    cuts, cut_s = detect_cuts(proxy)
    bursts, burst_s = detect_bursts(proxy)
    dur = spec.seconds
    return {
        "video": {**meta, "file": src.name},
        "proxy": {"wall_s": round(proxy_s, 2), "realtime_x": round(dur / proxy_s, 1),
                  "bytes": proxy.stat().st_size},
        "scene_cuts": {**match(meta["cuts_s"], cuts), "wall_s": round(cut_s, 2),
                       "realtime_x": round(dur / cut_s, 1)},
        "loudness_bursts": {**match(meta["bursts_s"], bursts), "wall_s": round(burst_s, 2),
                            "realtime_x": round(dur / burst_s, 1)},
        "encoder": encoder_args()[1],
    }
