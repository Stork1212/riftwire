"""Integration of URL acquisition (public_web) with highlight scans and Short export.

- Public footage may be scanned for highlights (``analyze``); exporting a Short (``edit``)
  needs the creator's permission.
- The highlight-scan migration (``0008``) and the public_web migration (``0009``) chain
  correctly over a populated database, keeping finished scan results."""

from __future__ import annotations

import json

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Job, JobType
from app.services import rights
from tests.conftest import requires_ffmpeg
from tests.test_ingest import drain
from tests.test_m2_audit import JOB, REC, T, _cfg, _populate, _sql
from tests.test_rights import ATTEST, CLIP, FULL_SCOPE, World
from tests.test_url_acquisition import FakeDownloader


@pytest.fixture
def w(db):
    return World(db)


@pytest.fixture
def client(db, settings):
    with TestClient(create_app(settings)) as c:
        yield c


@requires_ffmpeg
def test_public_footage_can_be_scanned_but_not_exported(w, db, client, settings, monkeypatch,
                                                        media_dir):
    settings.url_acquisition_enabled = True
    FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    src = client.post("/api/v1/sources", json={"url": CLIP, "creator_channel_id": w.twitch})
    assert src.status_code == 201, src.text
    drain(db, settings)
    with db.session() as s:
        acq = s.scalars(select(Job).where(Job.job_type == JobType.ACQUIRE.value)).one()
        rec_id = acq.recording_id
        # analysis (the highlight scan's use) is allowed for public footage
        assert rights.evaluate_recording(s, rec_id, "analyze").allowed
    r = client.post(f"/api/v1/recordings/{rec_id}/highlight-scan")
    assert r.status_code == 200, r.text                    # queued, not refused
    # exporting a Short is an edit: refused until the creator agrees
    r = client.post(f"/api/v1/recordings/{rec_id}/clip",
                    params={"start_ms": 0, "end_ms": 1000, "layout": "split"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["details"]["reason"] == "creator_permission_needed"
    r = client.post(f"/api/v1/recordings/{rec_id}/clip", params={"start_ms": 0, "end_ms": 1000})
    assert r.status_code == 403
    with db.session() as s:          # the creator says yes, covering the fetched footage
        rights.create_record(s, creator_id=w.creator, basis="creator_permission",
                             coverage="creator", attestation_text=ATTEST,
                             scope={**FULL_SCOPE, "acquisition_methods": ["url_fetch"]})
    with db.session() as s:
        assert rights.evaluate_recording(s, rec_id, "edit", clip_ms=1000).allowed


def test_highlight_scan_results_survive_the_public_web_migration(settings):
    """A database at the highlight-scan revision (0008) with a finished scan upgrades to
    0009 and back without losing the scan's result."""
    cfg = _cfg(settings.database_url)
    command.upgrade(cfg, "0008")
    _populate(settings)
    result = {"duration_ms": 60000, "coverage_ms": 60000, "speech_available": True,
              "items": [{"start_ms": 5000, "end_ms": 30000, "peak_ms": 12000, "score": 2.1,
                         "motion": 9.5, "speech_cues": ["pursuit"], "excerpt": "cops"}]}
    scan = "44444444-4444-4444-8444-444444444444"
    _sql(settings, "INSERT INTO jobs (id, recording_id, job_type, status, progress_percent,"
         " attempt_count, max_attempts, created_at, updated_at, result_json) VALUES "
         "(?, ?, 'HIGHLIGHT_SCAN', 'COMPLETED', 100, 1, 3, ?, ?, ?)",
         scan, REC, T, T, json.dumps(result))
    expected = sorted([(JOB, "METADATA", None), (scan, "HIGHLIGHT_SCAN", json.dumps(result))])
    for step in ("head", "0008", "head"):
        (command.upgrade if step == "head" else command.downgrade)(cfg, step)
        rows = sorted(_sql(settings, "SELECT id, job_type, result_json FROM jobs"))
        assert rows == expected, step
        assert _sql(settings, "SELECT related_job_id FROM system_events") == [(JOB,)]
    assert _sql(settings, "SELECT version_num FROM alembic_version") == [("0009",)]
    assert len(_sql(settings, "SELECT name FROM sqlite_master WHERE type='trigger'")) == 14
    assert _sql(settings, "PRAGMA foreign_key_check") == []

