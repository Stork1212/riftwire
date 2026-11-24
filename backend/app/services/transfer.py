"""Crash-safe, verified, no-overwrite file transfer onto a (possibly different) volume.

    src ─copy─► <staging>/<name>.<token>.partial   (destination volume, O_EXCL, fsync)
        ─verify─► re-read the STAGED FILE from disk and check SHA-256
        ─promote─► os.link(staged, dst)            (atomic; fails if dst exists)
        ─► fsync dst dir, unlink staged, fsync staging dir

* The destination path only ever appears complete and verified.
* Promotion can never overwrite: ``link(2)`` refuses an existing target atomically in the
  kernel, so concurrent attempts (threads or processes) cannot both win. The loser
  verifies the winner's file instead.
* Before linking, the staged file is checked to be the same regular file (device, inode,
  size) that was verified, so nothing can be swapped in between.
* The source is never modified or removed here.
* Attempt-scoped ``.partial`` files carry the attempt token, so the reconciler can remove
  leftovers of dead attempts.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

from app.core.testhooks import crashpoint
from app.services.storage import HASH_CHUNK, fsync_dir, sha256_file

CHUNK = 1024 * 1024
NO_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV}


class TransferError(Exception):
    def __init__(self, message: str, *, permanent: bool):
        super().__init__(message)
        self.permanent = permanent


def staged_name(name: str, token: str) -> str:
    return f"{name}.{token}.partial"


def stage_copy(src: Path, staging_dir: Path, name: str, token: str, *,
               progress: Callable[[int], None] | None = None,
               should_stop: Callable[[], bool] | None = None) -> Path:
    """Copy ``src`` into an exclusive, attempt-unique staging file and fsync it."""
    part = staging_dir / staged_name(name, token)
    done = 0
    with open(src, "rb") as fi, open(part, "xb") as fo:
        while chunk := fi.read(CHUNK):
            fo.write(chunk)
            done += len(chunk)
            if progress:
                progress(done)
            crashpoint("transfer_copy_midway")
            if should_stop and should_stop():
                raise InterruptedError("copy cancelled")
        fo.flush()
        os.fsync(fo.fileno())
    fsync_dir(staging_dir)
    return part


Identity = tuple[int, int, int, int, int]


def _ident(st: os.stat_result) -> Identity:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _identity(path: Path) -> Identity:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise TransferError(f"{path.name} is not a regular file", permanent=True)
    return _ident(st)


def _hash_fd(fd: int, should_stop: Callable[[], bool] | None) -> str:
    h = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, HASH_CHUNK):
        h.update(chunk)
        if should_stop and should_stop():
            raise InterruptedError("verification cancelled")
    return h.hexdigest()


def promote_no_overwrite(staged: Path, dst: Path,
                         expect: Identity | None = None) -> bool:
    """Atomically make ``staged`` appear at ``dst`` unless ``dst`` already exists.

    Returns False (``dst`` untouched) if ``dst`` exists, including when a concurrent
    attempt won. On success the staged name is removed. ``expect`` = (dev, inode, size)
    of the file that was verified; promotion is refused if the staged file changed."""
    if expect is not None and _identity(staged) != expect:
        raise TransferError("staged file changed after verification", permanent=False)
    try:
        os.link(staged, dst)
    except FileExistsError:
        return False
    except OSError as e:
        if e.errno in NO_LINKS:
            raise TransferError(
                "destination filesystem does not support hard links (APFS required)",
                permanent=True) from e
        raise
    fsync_dir(dst.parent)
    crashpoint("transfer_after_promote")
    staged.unlink()
    fsync_dir(staged.parent)
    return True


def transfer_file(src: Path, dst: Path, expected_sha256: str, staging_dir: Path,
                  token: str, *, progress: Callable[[int], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> str:
    """Copy ``src`` to ``dst`` (another volume allowed). Returns ``"promoted"`` or
    ``"already_present"`` (``dst`` existed with the expected content). Raises
    ``TransferError`` (permanent) if ``dst`` exists with different content."""

    def check_existing() -> str:
        if sha256_file(dst, should_stop=should_stop) != expected_sha256:
            raise TransferError(f"{dst.name} already exists at the destination with "
                                "different content; refusing to overwrite", permanent=True)
        return "already_present"

    if dst.exists():
        return check_existing()
    staging_dir.mkdir(exist_ok=True)
    part = stage_copy(src, staging_dir, dst.name, token, progress=progress,
                      should_stop=should_stop)
    try:
        crashpoint("transfer_before_verify")
        # Verify the staged file itself, re-read from disk through a descriptor that stays
        # open until after promotion: while it is open the inode cannot be recycled, so a
        # file swapped in at the staged name always has a different identity.
        fd = os.open(part, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise TransferError(f"{part.name} is not a regular file", permanent=True)
            if _hash_fd(fd, should_stop) != expected_sha256:
                raise TransferError(f"staged copy of {dst.name} failed SHA-256 verification",
                                    permanent=False)
            ident = _ident(os.fstat(fd))
            crashpoint("transfer_verified")
            promoted = promote_no_overwrite(part, dst, ident)
        finally:
            os.close(fd)
        if promoted:
            return "promoted"
        return check_existing()  # another attempt promoted first
    finally:
        part.unlink(missing_ok=True)  # no-op once promoted
