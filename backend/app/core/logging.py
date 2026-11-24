"""Structured JSON logging with rotating files."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path

_CONTEXT_FIELDS = ("component", "job_id", "recording_id", "event_type", "worker_id")
_SENSITIVE_MARKERS = ("secret", "token", "password", "api_key", "apikey")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "component": getattr(record, "component", None) or record.name.split(".")[-1],
            "message": record.getMessage(),
        }
        for field in _CONTEXT_FIELDS[1:]:
            val = getattr(record, field, None)
            if val is not None:
                payload[field] = str(val)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = {
                k: ("[redacted]" if any(m in k.lower() for m in _SENSITIVE_MARKERS) else v)
                for k, v in extra.items()
            }
        return json.dumps(payload, default=str)


_configured: set[str] = set()


def configure_logging(component: str, logs_dir: Path, level: str = "INFO",
                      max_bytes: int = 10 * 1024 * 1024, backup_count: int = 5) -> None:
    """Configure root logging once per process component ("api" / "worker")."""
    if component in _configured:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, "_riftwire", False):
            root.removeHandler(h)

    fmt = JsonFormatter()
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / f"{component}.log", maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler._riftwire = True  # type: ignore[attr-defined]
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler._riftwire = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    # Keep third-party noise down.
    logging.getLogger("uvicorn.access").setLevel("WARNING")
    _configured.add(component)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"riftwire.{name}")
