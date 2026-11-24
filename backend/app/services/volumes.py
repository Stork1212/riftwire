"""Volume facts for a path: mount point, filesystem type and the OS's own volume UUID.

Read-only. On macOS this uses ``diskutil info -plist <path>`` (no administrator rights
needed); on Linux (development/CI) it parses ``/proc/self/mountinfo``. Results are cached
briefly per device so per-write verification stays cheap.

A probe is injectable (``set_probe``) so tests can simulate an APFS external drive, an
exFAT drive or a swapped drive on any OS.
"""

from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("volumes")

CACHE_SECONDS = 10.0
DISKUTIL = "/usr/sbin/diskutil"


@dataclass(frozen=True)
class VolumeInfo:
    mount_point: str
    fs_type: str            # lower-case: "apfs", "exfat", "msdos", "ext4", "tmpfs", ...
    hw_volume_uuid: str | None
    label: str | None = None


class VolumeProbe:
    # Results are cached per device (st_dev): correct for real drives. Simulated drives in
    # tests share one device, so test probes set this to False.
    cacheable = True

    def info(self, path: Path) -> VolumeInfo:  # pragma: no cover - interface
        raise NotImplementedError


Runner = Callable[[list[str]], str]


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=15,
                          check=True).stdout


def parse_df_mount_point(df_out: str) -> str:
    """``df -P <path>`` → the "Mounted on" column (may contain spaces)."""
    lines = [ln for ln in df_out.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("unexpected df output")
    parts = lines[-1].split(None, 5)
    if len(parts) < 6:
        raise ValueError("unexpected df output")
    return parts[5]


def parse_mount_fs(mount_out: str, mount_point: str) -> str | None:
    """``mount`` (macOS): ``/dev/disk3s5 on /System/Volumes/Data (apfs, local, ...)``."""
    for line in mount_out.splitlines():
        head, sep, opts = line.rpartition(" (")
        if not sep or " on " not in head:
            continue
        mnt = head.split(" on ", 1)[1]
        if mnt == mount_point:
            return opts.split(",", 1)[0].strip(" )").lower()
    return None


class DarwinProbe(VolumeProbe):
    """macOS: mount point from ``df -P``, filesystem from ``mount``, OS volume UUID and
    name from ``diskutil info -plist <mount point>`` (diskutil accepts mount points and
    devices, not arbitrary folders). All read-only; no administrator rights needed."""

    def __init__(self, run: Runner | None = None):
        self.run = run or _run

    def info(self, path: Path) -> VolumeInfo:
        mnt = parse_df_mount_point(self.run(["/bin/df", "-P", str(path)]))
        fs = parse_mount_fs(self.run(["/sbin/mount"]), mnt)
        hw = label = None
        try:
            d = plistlib.loads(self.run([DISKUTIL, "info", "-plist", mnt]).encode())
            hw, label = d.get("VolumeUUID"), d.get("VolumeName")
            fs = fs or (d.get("FilesystemType") or "").lower() or None
        except Exception as e:  # identity unavailable: verify() treats a recorded UUID
            log.warning(f"diskutil could not describe {mnt}: {e}")  # mismatch as offline
        if not fs:
            raise RuntimeError(f"could not determine the filesystem of {mnt}")
        return VolumeInfo(mount_point=mnt, fs_type=fs, hw_volume_uuid=hw, label=label)


class LinuxProbe(VolumeProbe):
    def info(self, path: Path) -> VolumeInfo:
        target = str(path.resolve())
        best: tuple[str, str] = ("/", "unknown")
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                fields = left.split()
                if len(fields) < 5 or not right:
                    continue
                mnt = fields[4].replace("\\040", " ")
                fs = right.split()[0]
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) \
                        and len(mnt) >= len(best[0]):
                    best = (mnt, fs)
        return VolumeInfo(mount_point=best[0], fs_type=best[1].lower(), hw_volume_uuid=None)


class GenericProbe(VolumeProbe):
    def info(self, path: Path) -> VolumeInfo:
        p = path.resolve()
        while not os.path.ismount(p) and p != p.parent:
            p = p.parent
        return VolumeInfo(mount_point=str(p), fs_type="unknown", hw_volume_uuid=None)


def default_probe() -> VolumeProbe:
    system = platform.system()
    if system == "Darwin" and os.path.exists(DISKUTIL):
        return DarwinProbe()
    if system == "Linux" and os.path.exists("/proc/self/mountinfo"):
        return LinuxProbe()
    return GenericProbe()


_probe: VolumeProbe = default_probe()
_cache: dict[int, tuple[float, VolumeInfo]] = {}
_lock = threading.Lock()


def set_probe(probe: VolumeProbe | None) -> None:
    """Replace the probe (tests). ``None`` restores the platform default."""
    global _probe
    with _lock:
        _probe = probe or default_probe()
        _cache.clear()


def volume_info(path: Path) -> VolumeInfo:
    """Facts about the volume holding the existing path ``path``."""
    dev = os.stat(path).st_dev
    now = time.monotonic()
    with _lock:
        hit = _cache.get(dev)
        probe = _probe
        if probe.cacheable and hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    info = probe.info(path)
    if not probe.cacheable:
        return info
    with _lock:
        _cache[dev] = (now, info)
    return info


# --------------------------------------------------------------------------- policy

# APFS is required for every storage location on macOS (owner decision, 2026-11-24): hard
# links and atomic rename are needed for crash-safe, no-overwrite promotion. There is
# deliberately no override. Other operating systems are development/CI only; there,
# filesystems without POSIX hard-link semantics are refused.
REFUSED_ANYWHERE = {"exfat", "msdos", "vfat", "fat", "fat32", "ntfs", "fuseblk"}


def filesystem_allowed(fs_type: str, system: str | None = None) -> bool:
    system = system or platform.system()
    fs = (fs_type or "").lower()
    if system == "Darwin":
        return fs == "apfs"
    return fs not in REFUSED_ANYWHERE


def unmounted_volume_root(path: Path) -> Path | None:
    """If ``path`` lives under ``/Volumes/<name>`` and that volume is not mounted, return
    ``/Volumes/<name>``. Creating directories there would silently write to the internal
    disk, so callers must refuse. Returns None when the path is not under /Volumes or the
    volume is mounted."""
    parts = Path(os.path.abspath(path)).parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        root = Path("/Volumes", parts[2])
        if not os.path.ismount(root):
            return root
    return None
