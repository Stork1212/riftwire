"""Run one transcription in a fresh subprocess and measure wall time and peak memory.

macOS: ``/usr/bin/time -l`` reports *maximum resident set size* and *peak memory footprint*
(the latter includes Metal/unified-memory allocations, the better number for MLX).
Linux (tests): ``/usr/bin/time -v`` reports maximum RSS in kB.
The child runs with HF_HUB_OFFLINE=1: it can only load the already-downloaded local model.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]


def parse_time_output(stderr: str) -> dict:
    out: dict = {}
    m = re.search(r"^\s*(\d+)\s+maximum resident set size", stderr, re.M)
    if m:
        out["max_rss_bytes"] = int(m.group(1))  # macOS: bytes
    m = re.search(r"^\s*(\d+)\s+peak memory footprint", stderr, re.M)
    if m:
        out["peak_footprint_bytes"] = int(m.group(1))
    m = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", stderr)
    if m:
        out["max_rss_bytes"] = int(m.group(1)) * 1024  # GNU time: kB
    return out


def time_wrapper() -> list[str]:
    if not Path("/usr/bin/time").exists():
        return []
    return ["/usr/bin/time", "-l"] if platform.system() == "Darwin" else ["/usr/bin/time", "-v"]


def run_child(python: str, child_args: list[str], timeout: float = 7200,
              module: str = "riftbench.transcribe_child") -> dict:
    env = dict(os.environ, PYTHONPATH=str(BENCH), HF_HUB_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
    cmd = [*time_wrapper(), python, "-m", module, *child_args]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    wall = time.perf_counter() - t0
    res = {"returncode": p.returncode, "wall_s": round(wall, 3), **parse_time_output(p.stderr)}
    if p.returncode != 0:
        res["stderr_tail"] = p.stderr[-2000:]
    return res


def transcribe(python: str, model_dir: Path, audio: Path, out_json: Path,
               language: str = "en", timeout: float = 7200) -> dict:
    res = run_child(python, [str(model_dir), str(audio), str(out_json), language], timeout)
    if res["returncode"] == 0 and out_json.exists():
        res["result"] = json.loads(out_json.read_text())
    return res


def this_python() -> str:
    return sys.executable
