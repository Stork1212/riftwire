from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.core.config import GIB, Settings
from app.db.migrations import schema_status
from app.db.session import Database
from app.models import Job, Recording
from app.services.storage import (
    file_extension,
    safe_join,
    sanitize_display_filename,
)
from tests.conftest import BACKEND_DIR, run_migrations

# ------------------------------------------------------------------ settings


def test_default_free_space_floor_is_60_gib(monkeypatch, tmp_path):
    monkeypatch.delenv("RIFTWIRE_MIN_FREE_DISK_BYTES", raising=False)
    s = Settings(_env_file=None, data_dir=tmp_path)
    assert s.min_free_disk_bytes == 60 * GIB
    assert s.api_host == "127.0.0.1"
    assert s.worker_concurrency == 1


def test_data_dir_is_configurable(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path / "ext")
    assert s.recordings_dir == tmp_path / "ext" / "recordings"
    assert s.db_path == tmp_path / "ext" / "riftwire.db"


# ------------------------------------------------------------------ filenames / paths

@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\x\\clip.mp4", "clip.mp4"),
    ("a\x00b.mp4", "a_b.mp4"),
    ("....", "recording"),
    ("", "recording"),
    ("GTA VI – heist ✓.mp4", "GTA VI – heist ✓.mp4"),
])
def test_sanitize_display_filename(raw, expected):
    assert sanitize_display_filename(raw) == expected


def test_long_filename_keeps_extension():
    name = sanitize_display_filename("x" * 500 + ".mkv")
    assert len(name) <= 200 and name.endswith(".mkv")


def test_file_extension():
    assert file_extension("A.MP4") == "mp4"
    assert file_extension("../x.webm") == "webm"
    assert file_extension("noext") == ""


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../x", "a\x00b"])
def test_safe_join_rejects_escape(tmp_path, bad):
    with pytest.raises(ValueError):
        safe_join(tmp_path, bad)


def test_safe_join_allows_inside(tmp_path):
    assert safe_join(tmp_path, "recordings/a.mp4") == (tmp_path / "recordings/a.mp4").resolve()



# ------------------------------------------------------------------ migrations / db

def test_migration_upgrade_and_downgrade(settings):
    from alembic.config import Config
    run_migrations(settings.database_url)
    db = Database(settings.database_url)
    tables = set(inspect(db.engine).get_table_names())
    assert {"recordings", "jobs", "system_events", "workers", "alembic_version",
            "storage_locations", "disk_reservations", "storage_migrations",
            "storage_migration_items"} <= tables
    assert schema_status(db.engine)["ok"]
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["database_url"] = settings.database_url
    cfg.attributes["configure_logger"] = False
    command.downgrade(cfg, "base")
    db.engine.dispose()
    assert set(inspect(db.engine).get_table_names()) <= {"alembic_version"}
    assert not schema_status(db.engine)["ok"]
    db.dispose()


def test_unmigrated_db_reports_not_ready(settings):
    db = Database(settings.database_url)
    st = schema_status(db.engine)
    assert st["ok"] is False and st["current"] is None
    db.dispose()


def test_pragmas(db):
    with db.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 5000


def _rec(**kw):
    base = dict(original_filename="a.mp4", stored_filename="x.mp4", file_path="recordings/x.mp4",
                sha256="0" * 64, file_size_bytes=1)
    base.update(kw)
    return Recording(**base)


def test_recording_create_and_utc(db):
    with db.session() as s:
        s.add(_rec(fps_num=30000, fps_den=1001))
    with db.session() as s:
        r = s.query(Recording).one()
        assert r.imported_at.tzinfo is UTC
        assert r.fps_rational == "30000/1001"
        assert abs(r.fps - 29.97) < 0.01
        assert r.status == "STAGED" and r.thumbnail_status == "PENDING"


def test_sha_unique(db):
    with db.session() as s:
        s.add(_rec())
    with pytest.raises(IntegrityError), db.session() as s:
        s.add(_rec(stored_filename="y.mp4", file_path="recordings/y.mp4"))


def test_foreign_keys_enforced(db):
    with pytest.raises(IntegrityError), db.session() as s:
        s.add(Job(recording_id="nope", job_type="INGEST"))


def test_check_constraints(db):
    with db.session() as s:
        s.add(_rec(id="r1"))
    with pytest.raises(IntegrityError), db.session() as s:
        s.add(Job(recording_id="r1", job_type="BOGUS"))
    with pytest.raises(IntegrityError), db.session() as s:
        s.execute(text("UPDATE recordings SET status='WHATEVER'"))


def test_naive_datetime_rejected(db):
    with pytest.raises((ValueError, StatementError)), db.session() as s:
        s.add(_rec(imported_at=datetime(2026, 1, 1)))
