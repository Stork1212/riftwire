"""Integration of URL acquisition with highlight scans and Short export.

- Fetched footage may be scanned (``analyze``) and exported (``edit``) only as far as a
  user-confirmed permission allows; public availability allows neither (2026-11-25).
- The highlight-scan migration (``0008``) and the public_web migration (``0009``) chain
  correctly over a populated database, keeping finished scan results."""

from __future__ import annotations

import json

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Job, JobType, Recording
from app.services import evidence, rights
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file, legacy_public_web
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
def test_scan_and_export_follow_the_creators_permission(w, db, client, settings, monkeypatch,
                                                         media_dir):
    settings.url_acquisition_enabled = True
    FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    with db.session() as s:          # the creator allows fetching and analysis, not editing
        rid = rights.create_record(
            s, creator_id=w.creator, basis="creator_permission", coverage="creator",
            attestation_text=ATTEST,
            scope={**FULL_SCOPE, "uses": ["acquire", "analyze"],
                   "acquisition_methods": ["url_fetch"]}).id
    evidence.add_evidence(db, settings, rid, kind="platform_permission",
                          reference="Synthetic test permission")
    src = client.post("/api/v1/sources", json={"url": CLIP, "creator_channel_id": w.twitch})
    assert src.status_code == 201, src.text
    drain(db, settings)
    with db.session() as s:
        acq = s.scalars(select(Job).where(Job.job_type == JobType.ACQUIRE.value)).one()
        rec_id = acq.recording_id
        assert rights.evaluate_recording(s, rec_id, "analyze").allowed
    r = client.post(f"/api/v1/recordings/{rec_id}/highlight-scan")
    assert r.status_code == 200, r.text                    # queued, not refused
    for params in ({"start_ms": 0, "end_ms": 1000, "layout": "split"},
                   {"start_ms": 0, "end_ms": 1000}):
        r = client.post(f"/api/v1/recordings/{rec_id}/clip", params=params)
        assert r.status_code == 403, r.text
        assert r.json()["error"]["details"]["reason"] == "use_not_in_scope"
    with db.session() as s:          # the creator later agrees to editing too
        rights.supersede(s, rid, attestation_text=ATTEST,
                         scope={**FULL_SCOPE, "acquisition_methods": ["url_fetch"]})
    with db.session() as s:
        assert rights.evaluate_recording(s, rec_id, "edit", clip_ms=1000).allowed


@requires_ffmpeg
def test_public_footage_without_permission_cannot_be_scanned(w, db, client, settings,
                                                             media_dir):
    """A copy of public footage linked to a saved link, with only the legacy public_web
    row: no analysis, so no highlight scan is queued."""
    src = client.post("/api/v1/sources", json={"url": CLIP, "creator_channel_id": w.twitch})
    sid = src.json()["id"]
    with db.session() as s:
        legacy_public_web(s, sid)
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    drain(db, settings)
    with db.session() as s:
        r = s.get(Recording, rec.id)
        r.source_id, r.acquisition_method = sid, "url_fetch"
    r = client.post(f"/api/v1/recordings/{rec.id}/highlight-scan")
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == "creator_permission_needed"
    with db.session() as s:
        assert s.scalar(select(Job.id).where(
            Job.job_type == JobType.HIGHLIGHT_SCAN.value)) is None


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
    assert _sql(settings, "SELECT version_num FROM alembic_version") == [("0012",)]
    names = [r[0] for r in _sql(settings, "SELECT name FROM sqlite_master WHERE type='trigger'")]
    m4 = ("trg_transcript", "trg_signal", "trg_keyframes", "trg_ai_runs",
          "trg_highlight_candidates")
    assert len([n for n in names if not n.startswith(m4)]) == 14
    assert len([n for n in names if n.startswith("trg_transcript")]) == 10
    assert len([n for n in names if n.startswith(("trg_signal", "trg_keyframes"))]) == 4
    assert len([n for n in names if n.startswith(("trg_ai_runs",
                                                  "trg_highlight_candidates"))]) == 4
    assert _sql(settings, "PRAGMA foreign_key_check") == []

