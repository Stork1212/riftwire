"""Machine, swap and thermal snapshots (macOS commands; tolerant elsewhere)."""

from __future__ import annotations

import platform
import shutil
import subprocess

from riftbench.config import GIB, data_dir


def _cmd(*args: str) -> str:
    if not shutil.which(args[0]):
        return ""
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def machine() -> dict:
    mem = _cmd("sysctl", "-n", "hw.memsize")
    return {
        "platform": platform.platform(), "machine": platform.machine(),
        "python": platform.python_version(),
        "chip": _cmd("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor(),
        "memory_gb": round(int(mem) / GIB, 1) if mem.isdigit() else None,
        "macos": _cmd("sw_vers", "-productVersion") or None,
        "ffmpeg": (_cmd("ffmpeg", "-version").splitlines() or [""])[0].split(" Copyright")[0],
    }


def snapshot() -> dict:
    return {
        "swap": _cmd("sysctl", "-n", "vm.swapusage") or None,
        "thermal": _cmd("pmset", "-g", "therm") or None,
        "free_gib": round(shutil.disk_usage(data_dir()).free / GIB, 2)
        if data_dir().exists() else None,
    }
