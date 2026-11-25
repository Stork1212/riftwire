"""Public availability is a classification, never permission (owner decision 2026-11-25,
superseding the download-by-default policy of 2026-11-24; docs/URL_ACQUISITION.md §7).

* Saving a link grants nothing and queues nothing on its own.
* URL fetching is off by default; switched on, it still needs a user-confirmed record that
  allows ``acquire`` by ``url_fetch`` with written platform permission attached.
* ``public_web`` rows written by ``b13b91f`` stay as immutable history and authorize
  nothing: not acquisition, analysis, editing or publishing.
* Standing permission is used without asking again for every link.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core.config import Settings
from app.core.errors import RightsRefused
from app.main import create_app
from app.models import (
    Job,
    ProvenanceEvent,
    Recording,
    RightsRecord,
    Source,
    SystemEvent,
)
from app.services import evidence, rights, url_download
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file, legacy_public_web
from tests.test_ingest import drain
from tests.test_m2_audit import _cfg, _sql
from tests.test_rights import ATTEST, CLIP, FULL_SCOPE, World
from tests.test_url_acquisition import FakeDownloader

PUBLISH = {"platform": "youtube_shorts", "clip_ms": 30_000}
URL_SCOPE = {**FULL_SCOPE, "acquisition_methods": ["url_fetch"]}
OTHER = "https://www.twitch.tv/videos/24242424"
THIRD = "https://www.twitch.tv/videos/31313131"


@pytest.fixture
def w(db):
    return World(db)


@pytest.fixture
def client(db, settings):
    with TestClient(create_app(settings)) as c:
        yield c


def _save_link(client, w, url=CLIP) -> dict:
    r = client.post("/api/v1/sources", json={"url": url, "creator_channel_id": w.twitch})
    assert r.status_code == 201, r.text
    return r.json()


def _legacy(db, sid: str) -> RightsRecord:
    with db.session() as s:
        return legacy_public_web(s, sid)


def _permission(w, db, settings, *, scope=URL_SCOPE, platform: bool = True,
                **kw) -> str:
    """The creator's standing permission, confirmed by the owner (creator coverage)."""
    with db.session() as s:
        kw.setdefault("coverage", "creator")
        rid = rights.create_record(s, creator_id=w.creator, basis="creator_permission",
                                   scope=scope, attestation_text=ATTEST, **kw).id
    if platform:
        evidence.add_evidence(db, settings, rid, kind="platform_permission",
                              reference="Synthetic test permission")
    return rid


def _acquire_jobs(db) -> list[Job]:
    with db.session() as s:
        return list(s.scalars(select(Job).where(Job.job_type == "ACQUIRE")
                              .order_by(Job.created_at)))


# ------------------------------------------------------------------ defaults and saving

def test_url_acquisition_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RIFTWIRE_URL_ACQUISITION_ENABLED", raising=False)
    assert Settings.model_fields["url_acquisition_enabled"].default is False
    assert Settings(_env_file=None).url_acquisition_enabled is False


def test_saving_a_public_link_grants_nothing_and_queues_nothing(w, db, client, settings):
    for enabled, url in ((False, CLIP), (True, OTHER)):
        settings.url_acquisition_enabled = enabled
        src = _save_link(client, w, url)
        assert src["rights_status"] == "public_web"         # classification only
        assert src["authorized_uses"] == []
        detail = client.get(f"/api/v1/sources/{src['id']}").json()
        assert detail["records"] == []                      # no record is written
        acq = detail["acquire"]
        assert not acq["allowed"] and acq["reason"] == rights.NO_RIGHTS_RECORD
        assert "platform permission" in acq["action"]       # an actionable next step
    assert _acquire_jobs(db) == []
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(RightsRecord)) == 0
        ev = s.scalars(select(SystemEvent).where(
            SystemEvent.event_type == "acquire_not_queued")).one()   # only when switched on
        assert "no rights record" in ev.message


def test_enabling_fetching_does_not_bypass_rights(w, db, client, settings, monkeypatch,
                                                  media_dir):
    settings.url_acquisition_enabled = True
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    sid = _save_link(client, w)["id"]
    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 403
    assert r.json()["error"]["details"]["reason"] == rights.NO_RIGHTS_RECORD
    with pytest.raises(RightsRefused):
        url_download.acquire(db, settings, sid)
    assert fake.calls == [] and _acquire_jobs(db) == []


def test_local_sources_are_not_classified_as_public(w):
    sid = w.source(kind="local", url=None, creator_channel_id=w.local)
    with w.db.session() as s:
        assert rights.status_summary(s, s.get(Source, sid)) == "none"


# ------------------------------------------------------- legacy public_web rows (b13b91f)

def test_existing_public_web_records_authorize_nothing(w, db, client, settings):
    settings.url_acquisition_enabled = True
    sid = w.source()
    _legacy(db, sid)
    for use, kw in (("acquire", {"method": "url_fetch", "url_acquisition_enabled": True}),
                    ("analyze", {}), ("edit", {"clip_ms": 30_000}), ("publish", PUBLISH)):
        d = w.decide(sid, use, **kw)
        assert not d.allowed and d.reason == rights.CREATOR_PERMISSION_NEEDED, use
        assert "not permission" in d.message and "creator" in d.action.lower()
    with db.session() as s:
        src = s.get(Source, sid)
        assert rights.authorized_uses(s, src, url_acquisition_enabled=True) == []
        assert rights.status_summary(s, src) == "public_web"
    chk = client.get(f"/api/v1/sources/{sid}/rights-check", params={"use": "analyze"}).json()
    assert not chk["allowed"] and chk["reason"] == "creator_permission_needed"


def test_public_web_authorizes_nothing_whatever_its_scope_says(w, db):
    """Even a public_web row whose scope lists every use and method authorizes nothing."""
    sid = w.source()
    with db.session() as s:
        sc = rights.Scope(v=1, uses=["acquire", "analyze", "edit", "publish"],
                          acquisition_methods=["url_fetch", "platform_export"],
                          publish_platforms=["youtube_shorts"], brand_channels="*",
                          monetization=True)
        text = rights.canonical_scope(sc)
        r = RightsRecord(creator_id=w.creator, basis="public_web", coverage="sources",
                         scope_json=text, scope_sha256="0" * 64,
                         valid_from=rights.utcnow(), created_at=rights.utcnow(), version=1)
        s.add(r)
        s.flush()
        from app.models import RightsSourceLink
        s.add(RightsSourceLink(rights_record_id=r.id, source_id=sid))
        s.flush()
        r.attestation_text, r.attested_by, r.attested_at = "x" * 20, "system", rights.utcnow()
    for use in rights.USES:
        assert not w.decide(sid, use, clip_ms=1000, platform="youtube_shorts",
                            method="url_fetch", url_acquisition_enabled=True).allowed


def test_previously_queued_acquisition_cannot_bypass_the_policy(
        w, db, settings, monkeypatch, media_dir):
    """An ACQUIRE job queued under the 2026-11-24 policy (legacy public_web row, fetching
    on) is refused by the worker before any download, permanently, with provenance."""
    settings.url_acquisition_enabled = True
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    sid = w.source()
    _legacy(db, sid)
    with db.session() as s:                                # the row b13b91f's queue wrote
        now = rights.utcnow()
        s.add(Job(source_id=sid, job_type="ACQUIRE", status="PENDING", max_attempts=3,
                  created_at=now, updated_at=now, next_attempt_at=now))
    drain(db, settings)
    (job,) = _acquire_jobs(db)
    assert job.status == "FAILED" and job.attempt_count == 1 and job.recording_id is None
    assert "RightsRefused" in job.last_error
    with db.session() as s:
        assert s.scalars(select(ProvenanceEvent).where(
            ProvenanceEvent.entity_id == job.id,
            ProvenanceEvent.action == "rights_refused")).one()
        assert s.scalar(select(func.count()).select_from(Recording)) == 0
    assert fake.downloads == []


def test_public_web_is_never_upgraded_into_a_permission(w, db, client, settings):
    sid = w.source()
    legacy = _legacy(db, sid)
    before = (legacy.basis, legacy.scope_sha256, legacy.attested_by)
    perm = _permission(w, db, settings)
    with db.session() as s:
        again = s.get(RightsRecord, legacy.id)
        assert (again.basis, again.scope_sha256, again.attested_by) == before
        assert s.get(RightsRecord, perm).basis == "creator_permission"
        assert s.scalar(select(func.count()).select_from(RightsRecord)) == 2
    assert w.decide(sid, "analyze").record_id == perm          # the permission, never legacy
    r = client.post(f"/api/v1/rights/{legacy.id}/supersede",
                    json={"scope": FULL_SCOPE, "attestation_text": ATTEST})
    assert r.status_code == 422 and r.json()["error"]["code"] == "public_web_automatic"
    with db.session() as s, pytest.raises(Exception, match="immutable"):
        s.execute(update(RightsRecord).where(RightsRecord.id == legacy.id)
                  .values(basis="creator_permission"))
        s.flush()


def test_public_web_cannot_be_recorded_by_hand(w, client):
    r = client.post("/api/v1/rights", json={
        "creator_id": w.creator, "basis": "public_web", "coverage": "creator",
        "scope": FULL_SCOPE, "attestation_text": ATTEST})
    assert r.status_code == 422 and r.json()["error"]["code"] == "public_web_automatic"


# ------------------------------------------------------------- what does authorize it

def test_creator_permission_without_platform_permission_cannot_fetch(w, db, settings):
    sid = w.source()
    _permission(w, db, settings, platform=False)
    d = w.decide(sid, "acquire", method="url_fetch", url_acquisition_enabled=True)
    assert not d.allowed and d.reason == rights.PLATFORM_PERMISSION_MISSING
    assert w.decide(sid, "acquire", method="url_fetch").reason == \
        rights.URL_ACQUISITION_DISABLED


def test_both_content_and_method_must_be_authorized(w, db, settings):
    sid = w.source()
    # platform permission attached, but the method is not url_fetch
    _permission(w, db, settings, scope=FULL_SCOPE)
    d = w.decide(sid, "acquire", method="url_fetch", url_acquisition_enabled=True)
    assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED
    other = w.source(OTHER, creator_channel_id=w.twitch)
    # the method, but not the use
    with db.session() as s:
        rid = rights.create_record(s, creator_id=w.creator, basis="creator_permission",
                                   coverage="sources", source_ids=[other],
                                   scope={**URL_SCOPE, "uses": ["analyze"]},
                                   attestation_text=ATTEST).id
    evidence.add_evidence(db, settings, rid, kind="platform_permission", reference="x")
    d = w.decide(other, "acquire", method="url_fetch", url_acquisition_enabled=True)
    assert not d.allowed


def test_standing_permission_acquires_without_asking_again(w, db, client, settings):
    settings.url_acquisition_enabled = True
    perm = _permission(w, db, settings)
    first, second = _save_link(client, w), _save_link(client, w, OTHER)
    jobs = _acquire_jobs(db)
    assert [j.source_id for j in jobs] == [first["id"], second["id"]]
    with db.session() as s:                    # no per-link records were needed
        assert s.scalar(select(func.count()).select_from(RightsRecord)) == 1
    assert first["rights_status"] == "active"
    assert w.decide(first["id"], "acquire", method="url_fetch",
                    url_acquisition_enabled=True).record_id == perm


@pytest.mark.parametrize("lapse", ["revoked", "expired", "superseded"])
def test_lapsed_permission_refuses_acquisition(w, db, client, settings, lapse):
    settings.url_acquisition_enabled = True
    now = rights.utcnow()
    if lapse == "expired":
        _permission(w, db, settings, valid_from=now - timedelta(days=30),
                    valid_until=now - timedelta(days=1))
    else:
        rid = _permission(w, db, settings)
        with db.session() as s:
            if lapse == "revoked":
                rights.revoke(s, rid, "creator withdrew")
            else:                                    # new version drops URL fetching
                rights.supersede(s, rid, scope=FULL_SCOPE, attestation_text=ATTEST)
    sid = _save_link(client, w)["id"]
    assert _acquire_jobs(db) == []
    d = w.decide(sid, "acquire", method="url_fetch", url_acquisition_enabled=True)
    expected = {"revoked": rights.REVOKED, "expired": rights.EXPIRED,
                "superseded": rights.METHOD_NOT_PERMITTED}[lapse]
    assert not d.allowed and d.reason == expected
    r = client.post(f"/api/v1/sources/{sid}/acquire")
    assert r.status_code == 403


@requires_ffmpeg
def test_superseded_during_download_leaves_no_output(w, db, settings, monkeypatch,
                                                     media_dir):
    settings.url_acquisition_enabled = True
    rid = _permission(w, db, settings)
    sid = w.source()

    def supersede():
        with db.session() as s:
            rights.supersede(s, rid, scope=URL_SCOPE, attestation_text=ATTEST)
    FakeDownloader(monkeypatch, media_dir / "basic.mp4", after=supersede)
    with pytest.raises(RightsRefused) as e:
        url_download.acquire(db, settings, sid)
    assert e.value.details["reason"] == rights.RIGHTS_CHANGED
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Recording)) == 0
    assert list(settings.temp_dir.glob("source-download-*")) == []


@requires_ffmpeg
def test_permitted_fetch_records_the_actual_method_and_gates_editing(
        w, db, client, settings, monkeypatch, media_dir):
    settings.url_acquisition_enabled = True
    _permission(w, db, settings, scope={**URL_SCOPE, "uses": ["acquire", "analyze"]})
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    src = _save_link(client, w)                 # queued by the standing permission
    drain(db, settings)
    (job,) = _acquire_jobs(db)
    assert job.status == "COMPLETED" and len(fake.downloads) == 1
    with db.session() as s:
        rec = s.get(Recording, job.recording_id)
        assert rec.source_id == src["id"] and rec.acquisition_method == "url_fetch"
        assert rights.evaluate_recording(s, rec.id, "analyze").allowed
        d = rights.evaluate_recording(s, rec.id, "edit", clip_ms=10_000)
        assert not d.allowed and d.reason == rights.USE_NOT_IN_SCOPE


@requires_ffmpeg
def test_analysis_needs_media_obtained_by_a_permitted_method(w, db, settings, media_dir):
    """A hand-imported copy is judged by its own method: a URL-only permission does not
    cover it, and a legacy public_web row never does."""
    sid = w.source()
    _legacy(db, sid)
    _permission(w, db, settings)
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        r = s.get(Recording, rec.id)
        r.source_id, r.acquisition_method = sid, "creator_file"
    with db.session() as s:
        d = rights.evaluate_recording(s, rec.id, "analyze")
        assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED


# --------------------------------------------------------------------------- migration

TAMPER = ("UPDATE rights_records SET scope_json='{}'",
          "DELETE FROM rights_records",
          "UPDATE rights_source_links SET source_id = source_id",
          "DELETE FROM rights_source_links")


def _triggers(settings) -> set[str]:
    return {r[0] for r in _sql(settings, "SELECT name FROM sqlite_master WHERE type='trigger'")}


def _refusals(settings) -> int:
    c = sqlite3.connect(settings.db_path)
    refused = 0
    for sql in TAMPER:
        try:
            c.execute(sql)
            c.commit()
        except sqlite3.DatabaseError:
            c.rollback()
            refused += 1
    c.close()
    return refused


def test_migration_keeps_every_trigger_and_existing_records(w, db, settings):
    cfg = _cfg(settings.database_url)
    sid = w.source()
    rid = w.licence(coverage="sources", source_ids=[sid])
    db.dispose()
    before = _triggers(settings)
    assert len(before) == 14
    command.downgrade(cfg, "0008")
    assert _triggers(settings) == before
    assert _sql(settings, "SELECT id, basis FROM rights_records") == [(rid, "licence")]
    command.upgrade(cfg, "head")
    assert _triggers(settings) == before
    assert _sql(settings, "SELECT id, basis FROM rights_records") == [(rid, "licence")]
    assert _refusals(settings) == len(TAMPER)          # still immutable after the rebuild
    assert _sql(settings, "PRAGMA foreign_key_check") == []


def test_downgrade_refuses_while_legacy_public_web_records_exist(w, db, settings):
    _legacy(db, w.source())
    db.dispose()
    with pytest.raises(Exception, match="public_web"):
        command.downgrade(_cfg(settings.database_url), "0008")
    assert _sql(settings, "SELECT version_num FROM alembic_version") == [("0009",)]
    assert len(_triggers(settings)) == 14
