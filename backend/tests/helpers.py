from __future__ import annotations

from pathlib import Path

from app.services.ingest import ImportResult, UploadSession, complete_upload


def import_file(db, settings, path: Path, name: str | None = None,
                chunk: int = 256 * 1024) -> ImportResult:
    """Stream a local file through the same code path the API uses."""
    size = path.stat().st_size
    up = UploadSession(settings, name or path.name, size)
    try:
        with open(path, "rb") as f:
            while data := f.read(chunk):
                up.write(data)
        digest = up.finish()
    except BaseException:
        up.abort()
        raise
    return complete_upload(db, settings, up, digest)
