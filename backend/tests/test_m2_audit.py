"""Regression tests for the independent audit of 63a2e5e (docs/M2_REVIEW.md §9).
Each test was run against the audited code first and failed there."""

from __future__ import annotations

import json
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app.core.errors import RightsRefused
from app.db.base import utcnow
from app.main import create_app
from app.models import (
    ProvenanceEvent,
    Recording,
    RightsRecord,
    RightsRecordChannel,
    RightsSourceLink,
)
from app.services import acquisition, rights, sources
from tests.conftest import BACKEND_DIR, requires_ffmpeg
from tests.helpers import import_file
from tests.test_rights import ATTEST, FULL_SCOPE, World

REC = "11111111-1111-4111-8111-111111111111"
JOB = "22222222-2222-4222-8222-222222222222"
T = "2026-11-24T00:00:00+00:00"


def _cfg(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def _sql(settings, sql: str, *args) -> list[tuple]:
    c = sqlite3.connect(settings.db_path)
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def _version(settings) -> str:
    return _sql(settings, "SELECT version_num FROM alembic_version")[0][0]


# ------------------------------------------------- finding 1: job history across migrations

def _populate(settings) -> None:
    """One recording with a completed job and a system event pointing at both, written the
    way Phase 1 and M1 code writes them (raw SQL so it works at any revision)."""
    _sql(settings, "INSERT INTO recordings (id, original_filename, stored_filename, file_path,"
         " sha256, file_size_bytes, imported_at, status, thumbnail_status) VALUES "
         "(?, 'a.mp4', 'x.mp4', 'recordings/x.mp4', ?, 10, ?, 'READY', 'READY')",
         REC, "ab" * 32, T)
    _sql(settings, "INSERT INTO jobs (id, recording_id, job_type, status, progress_percent,"
         " attempt_count, max_attempts, created_at, updated_at) VALUES "
         "(?, ?, 'METADATA', 'COMPLETED', 100, 1, 3, ?, ?)", JOB, REC, T, T)
    _sql(settings, "INSERT INTO system_events (timestamp, severity, component, event_type,"
         " message, related_job_id, related_recording_id) VALUES "
         "(?, 'INFO', 'worker', 'job_completed', 'done', ?, ?)", T, JOB, REC)


def _history(settings) -> tuple:
    return (_sql(settings, "SELECT id FROM recordings"),
            _sql(settings, "SELECT id, recording_id, status FROM jobs"),
            _sql(settings, "SELECT related_job_id, related_recording_id FROM system_events"))


EXPECTED = ([(REC,)], [(JOB, REC, "COMPLETED")], [(JOB, REC)])


@pytest.mark.parametrize("start, path", [
    ("0003", ["0004", "0005", "head"]),                  # 0004 upgrade rebuilds recordings
    ("head", ["0005", "0004"]),                          # 0005 downgrade rebuilds recordings
    ("0004", ["0003"]),                                  # 0004 downgrade rebuilds recordings
    ("0001", ["head", "base:0001", "head"]),             # full round trip
])
def test_migrations_preserve_jobs_and_event_references(settings, start, path):
    cfg = _cfg(settings.database_url)
    command.upgrade(cfg, start)
    _populate(settings)
    assert _history(settings) == EXPECTED
    for step in path:
        if step.startswith("base:"):
            command.downgrade(cfg, step.split(":")[1])
        elif step == "head" or step > _version(settings):
            command.upgrade(cfg, step)
        else:
            command.downgrade(cfg, step)
        assert _history(settings) == EXPECTED, f"history lost at {step}"
    assert _sql(settings, "PRAGMA foreign_key_check") == []


def test_migration_is_atomic_and_refuses_dangling_references(settings):
    """A foreign-key violation found after the schema change rolls back the whole run,
    DDL included: the version row and the schema stay at the starting revision."""
    cfg = _cfg(settings.database_url)
    command.upgrade(cfg, "0003")
    c = sqlite3.connect(settings.db_path)              # enforcement off: plant an orphan
    c.execute("INSERT INTO jobs (id, recording_id, job_type, status, progress_percent,"
              " attempt_count, max_attempts, created_at, updated_at) VALUES "
              "(?, ?, 'METADATA', 'COMPLETED', 100, 1, 3, ?, ?)", (JOB, REC, T, T))
    c.commit()
    c.close()
    with pytest.raises(Exception, match="foreign-key violation"):
        command.upgrade(cfg, "head")
    assert _version(settings) == "0003"
    tables = {r[0] for r in _sql(settings, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sources" not in tables and "rights_records" not in tables
    cols = {r[1] for r in _sql(settings, "PRAGMA table_info(recordings)")}
    assert "source_id" not in cols


def test_application_connections_still_enforce_foreign_keys(db):
    with db.session() as s:
        assert s.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


# ------------------------------------------------- finding 2: backfill of the actual method

@pytest.fixture
def w(db):
    return World(db)


def _rebackfill(settings) -> None:
    """Simulate data linked before 0005: drop the column and re-run 0005's backfill."""
    cfg = _cfg(settings.database_url)
    command.downgrade(cfg, "0004")
    command.upgrade(cfg, "head")


@requires_ffmpeg
def test_backfill_uses_the_recorded_method_not_the_declared_one(w, db, settings, media_dir):
    """Source declared local_recording; the attachment was recorded as creator_file. The
    licence permits only local_recording, so analysis must be refused after migration."""
    w.licence(scope={**FULL_SCOPE, "acquisition_methods": ["local_recording", "creator_file"]})
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="local_recording")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id, "creator_file")
    with db.session() as s:                             # narrow the licence to the declared one
        rid = s.scalars(select(RightsRecord.id)).one()
        rights.supersede(s, rid, scope={**FULL_SCOPE, "acquisition_methods":
                                        ["local_recording"]}, attestation_text=ATTEST)
    db.dispose()
    _rebackfill(settings)
    with db.session() as s:
        assert s.get(Recording, rec.id).acquisition_method == "creator_file"
        d = rights.evaluate_recording(s, rec.id, "analyze")
        assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED
        # the source-level question (no specific media) still uses the declared method
        assert rights.evaluate(s, sid, "analyze").allowed


@requires_ffmpeg
@pytest.mark.parametrize("events", [
    [],                                                                  # no provenance
    [{"method": "creator_file"}, {"method": "platform_export"}],         # ambiguous
    [{"method": "carrier_pigeon"}],                                      # unknown method
])
def test_missing_or_ambiguous_method_stays_blocked_until_recorded(w, db, settings, media_dir,
                                                                  client, events):
    w.licence()
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="local_recording")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    # Pre-0005 data: linked directly, with whatever attachment events existed then.
    _sql(settings, "UPDATE recordings SET source_id = ? WHERE id = ?", sid, rec.id)
    with db.session() as s:
        for e in events:
            rights.provenance(s, "recording", rec.id, "attached_to_source",
                              source_id=sid, **e)
    db.dispose()
    _rebackfill(settings)
    with db.session() as s:
        assert s.get(Recording, rec.id).acquisition_method is None
        for use in ("analyze", "edit", "publish"):
            d = rights.evaluate_recording(s, rec.id, use)
            assert not d.allowed and d.reason == rights.METHOD_UNKNOWN
        assert rights.REASON_ACTION[rights.METHOD_UNKNOWN]
    # The worker and the queue use the same decision: enqueueing a gated job is refused.
    body = client.get(f"/api/v1/recordings/{rec.id}").json()
    assert body["acquisition_method"] is None
    # The owner records the method once (checked against the licence, with provenance).
    r = client.post(f"/api/v1/sources/{sid}/recordings",
                    json={"recording_id": rec.id, "acquisition_method": "creator_file"})
    assert r.status_code == 200, r.text
    with db.session() as s:
        assert s.get(Recording, rec.id).acquisition_method == "creator_file"
        assert rights.evaluate_recording(s, rec.id, "analyze").allowed
        ev = s.scalars(select(ProvenanceEvent).where(
            ProvenanceEvent.entity_id == rec.id,
            ProvenanceEvent.action == "acquisition_method_recorded")).one()
        assert json.loads(ev.details_json)["method"] == "creator_file"
    # Once on record it cannot be changed by re-attaching.
    r = client.post(f"/api/v1/sources/{sid}/recordings",
                    json={"recording_id": rec.id, "acquisition_method": "platform_export"})
    assert r.status_code == 409


def test_unpermitted_method_cannot_be_recorded(w, db, settings):
    w.licence(scope={**FULL_SCOPE, "acquisition_methods": ["local_recording"]})
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="local_recording")
    with db.session() as s:
        rec = Recording(original_filename="a.mp4", stored_filename="y.mp4",
                        file_path="recordings/y.mp4", sha256="cd" * 32, file_size_bytes=1,
                        imported_at=utcnow(), status="READY", thumbnail_status="READY",
                        source_id=sid)
        s.add(rec)
        s.flush()
        rec_id = rec.id
    with pytest.raises(RightsRefused), db.session() as s:
        acquisition.attach_media(s, settings, sid, rec_id, "creator_file")
    with db.session() as s:
        assert s.get(Recording, rec_id).acquisition_method is None


# ------------------------------------------------- finding 3: attested coverage on UPDATE

def _record(db, w, coverage: str, *, attest: bool, **links) -> str:
    with db.session() as s:
        return rights.create_record(s, creator_id=w.creator, basis="licence", coverage=coverage,
                                    scope=FULL_SCOPE, attestation_text=ATTEST, attest=attest,
                                    **links).id


def _other_channel(db, w) -> str:
    with db.session() as s:
        return sources.add_channel(s, w.creator, "youtube", handle="other").id


def _raw_update(db, sql: str, **params) -> None:
    with db.session() as s:
        s.execute(text(sql), params)


def test_attested_channel_coverage_cannot_be_repointed(w, db):
    other = _other_channel(db, w)
    rid = _record(db, w, "channels", attest=True, channel_ids=[w.twitch])
    draft = _record(db, w, "channels", attest=False, channel_ids=[other])
    for sql in (
        # change which channel an attested link names
        "UPDATE rights_record_channels SET creator_channel_id = :other "
        "WHERE rights_record_id = :rid",
        # move the attested record's link onto a draft (removes attested coverage)
        "UPDATE rights_record_channels SET rights_record_id = :draft "
        "WHERE rights_record_id = :rid",
        # move a draft's link onto the attested record (adds attested coverage)
        "UPDATE rights_record_channels SET rights_record_id = :rid "
        "WHERE rights_record_id = :draft",
    ):
        with pytest.raises(DatabaseError, match="frozen"):
            _raw_update(db, sql, rid=rid, other=other, draft=draft)
    with db.session() as s:
        links = s.execute(select(RightsRecordChannel.rights_record_id,
                                 RightsRecordChannel.creator_channel_id)).all()
    assert sorted(links) == sorted([(rid, w.twitch), (draft, other)])


def test_attested_source_coverage_cannot_be_repointed(w, db):
    s1 = w.source(kind="local", url=None, creator_channel_id=w.local, title_user="one")
    s2 = w.source(kind="local", url=None, creator_channel_id=w.local, title_user="two")
    rid = _record(db, w, "sources", attest=True, source_ids=[s1])
    draft = _record(db, w, "sources", attest=False, source_ids=[s2])
    for sql in (
        "UPDATE rights_source_links SET source_id = :s2 WHERE rights_record_id = :rid",
        "UPDATE rights_source_links SET rights_record_id = :draft WHERE rights_record_id = :rid",
        "UPDATE rights_source_links SET rights_record_id = :rid WHERE rights_record_id = :draft",
    ):
        with pytest.raises(DatabaseError, match="frozen"):
            _raw_update(db, sql, rid=rid, draft=draft, s2=s2)
    with db.session() as s:
        links = s.execute(select(RightsSourceLink.rights_record_id,
                                 RightsSourceLink.source_id)).all()
    assert sorted(links) == sorted([(rid, s1), (draft, s2)])


def test_draft_coverage_stays_editable(w, db):
    other = _other_channel(db, w)
    d1 = _record(db, w, "channels", attest=False, channel_ids=[w.twitch])
    d2 = _record(db, w, "channels", attest=False, channel_ids=[other])
    _raw_update(db, "UPDATE rights_record_channels SET creator_channel_id = :other "
                "WHERE rights_record_id = :d1", d1=d1, other=other)
    _raw_update(db, "UPDATE rights_record_channels SET rights_record_id = :d1, "
                "creator_channel_id = :twitch WHERE rights_record_id = :d2",
                d1=d1, d2=d2, twitch=w.twitch)
    with db.session() as s:
        assert sorted(s.execute(select(RightsRecordChannel.rights_record_id,
                                       RightsRecordChannel.creator_channel_id)).all()) == \
            sorted([(d1, other), (d1, w.twitch)])


# ------------------------------------------------- finding 4: malformed URLs

@pytest.mark.parametrize("url", ["https://[", "https://[::1", "https://www.youtube.com:99999/"])
def test_malformed_url_is_a_validation_error(client, url):
    r = client.post("/api/v1/sources/resolve", json={"url": url})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] in ("invalid_url", "ip_address_host", "port_not_allowed")
    assert err["message"]


@pytest.fixture
def client(db, settings):
    with TestClient(create_app(settings)) as c:
        yield c
