"""Volume facts for a path: mount point, filesystem type and the OS's own volume UUID.

Read-only. On macOS this uses ``df``, ``mount`` and ``diskutil info -plist <mount point>``
(no administrator rights needed); on Linux (development/CI) it parses
``/proc/self/mountinfo``.

Every call probes afresh. There is deliberately no cache: a cache keyed by device number
could return a previous drive's identity after an unplug/replug that reuses the number
(review finding 2). Callers that need cheap repeated checks cache whole verification
results briefly (``Layout.state(max_age=...)``), never identity used before a write.

On macOS the OS volume UUID is **required**: if it cannot be established the probe raises
``IdentityUnavailable`` and every caller fails closed (location offline, registration and
migration refused).

A probe is injectable (``set_probe``) so tests can simulate an APFS external drive, an
exFAT drive or a swapped drive on any OS.
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger

log = get_logger("volumes")

DISKUTIL = "/usr/sbin/diskutil"
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


class IdentityUnavailable(RuntimeError):
    """The OS volume UUID could not be established (macOS): treat the drive as unknown."""


def valid_volume_uuid(value: object) -> str | None:
    """Normalised (upper-case) volume UUID, or None if ``value`` is not one."""
    if not isinstance(value, str):
        return None
    v = value.strip().upper()
    return v if UUID_RE.match(v) else None


@dataclass(frozen=True)
class VolumeInfo:
    mount_point: str
    fs_type: str            # lower-case: "apfs", "exfat", "msdos", "ext4", "tmpfs", ...
    hw_volume_uuid: str | None
    label: str | None = None


class VolumeProbe:
    # True where the platform gives every volume a stable OS UUID (macOS). Storage then
    # requires it: no location is registered, verified or migrated without it.
    requires_hw_uuid = False

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

    requires_hw_uuid = True

    def __init__(self, run: Runner | None = None):
        self.run = run or _run

    def info(self, path: Path) -> VolumeInfo:
        mnt = parse_df_mount_point(self.run(["/bin/df", "-P", str(path)]))
        fs = parse_mount_fs(self.run(["/sbin/mount"]), mnt)
        try:
            d = plistlib.loads(self.run([DISKUTIL, "info", "-plist", mnt]).encode())
        except Exception as e:
            raise IdentityUnavailable(f"diskutil could not describe {mnt}: {e}") from e
        if not isinstance(d, dict):
            raise IdentityUnavailable(f"diskutil returned unexpected data for {mnt}")
        hw = valid_volume_uuid(d.get("VolumeUUID"))
        if hw is None:
            raise IdentityUnavailable(f"no valid volume UUID for {mnt}")
        label = d.get("VolumeName") if isinstance(d.get("VolumeName"), str) else None
        fs = fs or (str(d.get("FilesystemType") or "")).lower() or None
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


def set_probe(probe: VolumeProbe | None) -> None:
    """Replace the probe (tests). ``None`` restores the platform default."""
    global _probe
    _probe = probe or default_probe()


def identity_required() -> bool:
    """True when every storage volume must have a valid OS volume UUID (macOS)."""
    return _probe.requires_hw_uuid


def volume_info(path: Path) -> VolumeInfo:
    """Fresh facts about the volume holding the existing path ``path`` (never cached).

    Where identity is required, the result always carries a valid ``hw_volume_uuid``;
    otherwise ``IdentityUnavailable`` is raised."""
    os.stat(path)  # raises if the path is gone
    info = _probe.info(path)
    if _probe.requires_hw_uuid:
        hw = valid_volume_uuid(info.hw_volume_uuid)
        if hw is None:
            raise IdentityUnavailable(f"no valid volume UUID for {info.mount_point}")
        if hw != info.hw_volume_uuid:
            info = VolumeInfo(info.mount_point, info.fs_type, hw, info.label)
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
