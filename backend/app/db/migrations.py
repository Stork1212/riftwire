"""Schema version checks (the app never creates tables itself)."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def schema_status(engine: Engine) -> dict:
    head = head_revision()
    try:
        current = current_revision(engine)
    except Exception as e:  # database unreadable
        return {"ok": False, "current": None, "head": head, "error": str(e)}
    return {"ok": current == head and head is not None, "current": current, "head": head}
