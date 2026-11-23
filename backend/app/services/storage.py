"""Filesystem helpers: safe names, path containment, hashing, fsync, disk space."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path

HASH_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\:]")


def sanitize_display_filename(name: str, max_len: int = 200) -> str:
    """Return a safe *display* name. Never used to build a filesystem path."""
    name = unicodedata.normalize("NFC", name or "")
    # Keep only the last path component, whatever separator the client used.
    name = re.split(r"[/\\]", name)[-1]
    name = _UNSAFE.sub("_", name).strip().strip(".")
    if not name:
        name = "recording"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def file_extension(name: str) -> str:
    base = re.split(r"[/\\]", name or "")[-1]
    _, dot, ext = base.rpartition(".")
    return ext.lower() if dot else ""


def safe_join(root: Path, relative: str) -> Path:
    """Join ``relative`` onto ``root`` and refuse anything that escapes ``root``."""
    root = root.resolve()
    if os.path.isabs(relative) or "\x00" in relative:
        raise ValueError("unsafe path")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes data directory")
    return candidate


def sha256_file(path: Path, progress: Callable[[int], None] | None = None,
                should_stop: Callable[[], bool] | None = None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
            if should_stop and should_stop():
                raise InterruptedError("hashing cancelled")
    return h.hexdigest()


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems do not support directory fsync
    finally:
        os.close(fd)


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class SpaceReservations:
    """Tracks bytes promised to in-flight uploads so concurrent uploads cannot jointly
    breach the free-space floor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reserved: dict[str, int] = {}

    def try_reserve(self, key: str, nbytes: int, free_now: int, floor: int) -> bool:
        with self._lock:
            others = sum(self._reserved.values())
            if free_now - others - nbytes < floor:
                return False
            self._reserved[key] = nbytes
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._reserved.pop(key, None)

    def total(self) -> int:
        with self._lock:
            return sum(self._reserved.values())
