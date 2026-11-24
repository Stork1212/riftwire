from __future__ import annotations

from pathlib import Path

from app.services.ingest import ImportResult, UploadSession, complete_upload
from app.services.locations import MARKER


def user_files(d: Path, files_only: bool = False) -> list[str]:
    """Names in ``d`` excluding the location identity marker (sorted)."""
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.name != MARKER and (not files_only or p.is_file()))


def import_file(db, settings, path: Path, name: str | None = None,
                chunk: int = 256 * 1024) -> ImportResult:
    """Stream a local file through the same code path the API uses."""
    size = path.stat().st_size
    up = UploadSession(settings, name or path.name, size, db)
    try:
        with open(path, "rb") as f:
            while data := f.read(chunk):
                up.write(data)
        digest = up.finish()
    except BaseException:
        up.abort()
        raise
    return complete_upload(db, settings, up, digest)
