"""Paths, disk-floor checks and shared constants for the benchmark harness."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3
FLOOR_BYTES = 60 * GIB          # owner requirement: keep ≥ 60 GiB free
MARGIN_BYTES = 1 * GIB          # extra safety margin for benchmark artefacts


def data_dir() -> Path:
    d = Path(os.environ.get("RIFTWIRE_DATA_DIR", ROOT / "data")).expanduser()
    return d if d.is_absolute() else (ROOT / d).resolve()


def models_dir() -> Path:
    return data_dir() / "models"


def bench_dir() -> Path:
    return data_dir() / "bench"


class InsufficientSpace(RuntimeError):
    pass


@dataclass
class SpaceCheck:
    free_bytes: int
    needed_bytes: int
    floor_bytes: int

    @property
    def ok(self) -> bool:
        return self.free_bytes - self.needed_bytes >= self.floor_bytes + MARGIN_BYTES

    @property
    def headroom_after(self) -> int:
        return self.free_bytes - self.needed_bytes - self.floor_bytes


def check_space(path: Path, needed_bytes: int, floor_bytes: int = FLOOR_BYTES) -> SpaceCheck:
    path.mkdir(parents=True, exist_ok=True)
    return SpaceCheck(shutil.disk_usage(path).free, needed_bytes, floor_bytes)


def require_space(path: Path, needed_bytes: int, what: str,
                  floor_bytes: int = FLOOR_BYTES) -> SpaceCheck:
    c = check_space(path, needed_bytes, floor_bytes)
    if not c.ok:
        raise InsufficientSpace(
            f"Refusing {what}: needs {needed_bytes / 1e6:.0f} MB; free {c.free_bytes / GIB:.1f}"
            f" GiB would fall below the {floor_bytes / GIB:.0f} GiB floor + "
            f"{MARGIN_BYTES / GIB:.0f} GiB margin.")
    return c


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total
