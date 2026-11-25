"""The public_web basis: a saved link is public footage that may be fetched and analysed,
recorded automatically and honestly, and never authorizes editing or publishing.

Owner acquisition policy (2026-11-24), points 1 and 2; docs/URL_ACQUISITION.md §7."""

from __future__ import annotations

import json
import sqlite3

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.errors import RightsRefused
from app.main import create_app
from app.models import (
    Job,
    ProvenanceEvent,
    Recording,
    RightsRecord,
    RightsSourceLink,
    Source,
    SystemEvent,
)
from app.services import queue, rights, sources
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.test_ingest import drain
from tests.test_m2_audit import _cfg, _sql
from tests.test_rights import ATTEST, CLIP, FULL_SCOPE, World
from tests.test_url_acquisition import FakeDownloader

PUBLISH = {"platform": "youtube_shorts", "clip_ms": 30_000}


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


def _public(db, sid: str) -> RightsRecord:
    with db.session() as s:
        return s.scalars(select(RightsRecord).join(
            RightsSourceLink, RightsSourceLink.rights_record_id == RightsRecord.id).where(
            RightsSourceLink.source_id == sid, RightsRecord.basis == "public_web")).one()


# ------------------------------------------------------------------------- the record

def test_saving_a_link_records_public_footage_honestly(w, db, client):
    src = _save_link(client, w)
    r = _public(db, src["id"])
    assert r.coverage == "sources" and r.creator_id == w.creator
    assert r.attested_by == "system" and r.attested_at is not None
    assert "Not creator-confirmed" in r.attestation_text
    scope = json.loads(r.scope_json)
    assert scope["uses"] == ["acquire", "analyze"]
    assert scope["acquisition_methods"] == ["url_fetch"]
    assert scope["publish_platforms"] == [] and scope["monetization"] is False
    with db.session() as s:
        ev = s.scalars(select(ProvenanceEvent).where(
            ProvenanceEvent.entity_id == r.id,
            ProvenanceEvent.action == "recorded_public_web")).one()
        assert ev.actor == "system" and json.loads(ev.details_json)["source_id"] == src["id"]
        # idempotent: never a second record for the same link
        again = rights.record_public_web(s, s.get(Source, src["id"]))
        assert again.id == r.id
    assert src["rights_status"] == "public_web"


def test_local_sources_get_no_public_web_record(w, db):
    sid = w.source(kind="local", url=None, creator_channel_id=w.local, public_web=True)
    with db.session() as s:
        assert s.scalar(select(RightsRecord.id)) is None
        assert rights.record_public_web(s, s.get(Source, sid)) is None


def test_public_web_authorizes_fetch_and_analysis_only(w, db, client, settings):
    settings.url_acquisition_enabled = True
    src = _save_link(client, w)
    sid = src["id"]
    with db.session() as s:
        assert rights.authorized_uses(s, s.get(Source, sid),
                                      url_acquisition_enabled=True) == ["acquire", "analyze"]
    assert w.decide(sid, "acquire", method="url_fetch", url_acquisition_enabled=True).allowed
    assert w.decide(sid, "analyze").allowed
    for use, kw in (("edit", {"clip_ms": 30_000}), ("publish", PUBLISH)):
        d = w.decide(sid, use, **kw)
        assert not d.allowed and d.reason == rights.CREATOR_PERMISSION_NEEDED
        assert "creator" in rights.REASON_ACTION[d.reason].lower()
    chk = client.get(f"/api/v1/sources/{sid}/rights-check",
                     params={"use": "edit", "clip_ms": 30000}).json()
    assert not chk["allowed"] and chk["reason"] == "creator_permission_needed"
    assert chk["action"]


def test_public_web_never_authorizes_editing_whatever_its_scope_says(w, db):
    """Defence in depth: even a public_web row whose scope lists every use (which the
    application never writes) cannot authorize edit or publish."""
    sid = w.source()
    with db.session() as s:
        sc = rights.Scope(v=1, uses=["acquire", "analyze", "edit", "publish"],
                          acquisition_methods=["url_fetch"],
                          publish_platforms=["youtube_shorts"], brand_channels="*",
                          monetization=True)
        text = rights.canonical_scope(sc)
        r = RightsRecord(creator_id=w.creator, basis="public_web", coverage="sources",
                         scope_json=text, scope_sha256="0" * 64,
                         valid_from=rights.utcnow(), created_at=rights.utcnow(), version=1)
        s.add(r)
        s.flush()
        s.add(RightsSourceLink(rights_record_id=r.id, source_id=sid))
        s.flush()
        r.attestation_text, r.attested_by, r.attested_at = "x" * 20, "system", rights.utcnow()
    d = w.decide(sid, "edit", clip_ms=1000)
    assert not d.allowed and d.reason == rights.CREATOR_PERMISSION_NEEDED


def test_public_web_cannot_be_created_or_widened_by_hand(w, db, client):
    r = client.post("/api/v1/rights", json={
        "creator_id": w.creator, "basis": "public_web", "coverage": "creator",
        "scope": FULL_SCOPE, "attestation_text": ATTEST})
    assert r.status_code == 422 and r.json()["error"]["code"] == "public_web_automatic"
    src = _save_link(client, w)
    pub = _public(db, src["id"])
    r = client.post(f"/api/v1/rights/{pub.id}/supersede", json={
        "scope": FULL_SCOPE, "attestation_text": ATTEST})
    assert r.status_code == 422 and r.json()["error"]["code"] == "public_web_automatic"
    # immutable like any attested record
    with db.session() as s, pytest.raises(Exception, match="immutable"):
        s.execute(update(RightsRecord).where(RightsRecord.id == pub.id)
                  .values(scope_json="{}"))
        s.flush()


def test_creator_permission_unlocks_editing_and_publishing(w, db, client):
    src = _save_link(client, w)
    sid = src["id"]
    assert not w.decide(sid, "edit", clip_ms=30_000).allowed
    with db.session() as s:                          # the creator said yes
        rights.create_record(s, creator_id=w.creator, basis="creator_permission",
                             coverage="creator", scope=FULL_SCOPE, attestation_text=ATTEST)
    assert w.decide(sid, "edit", clip_ms=30_000).allowed
    detail = client.get(f"/api/v1/sources/{sid}").json()
    assert detail["rights_status"] == "active"
    assert {r["basis"] for r in detail["records"]} == {"public_web", "creator_permission"}
    perm = next(r for r in detail["records"] if r["basis"] == "creator_permission")
    client.post(f"/api/v1/rights/{perm['id']}/revoke", json={"reason": "creator withdrew"})
    d = w.decide(sid, "edit", clip_ms=30_000)        # the more useful reason is reported
    assert not d.allowed and d.reason == rights.REVOKED
    assert w.decide(sid, "analyze").allowed          # public footage stays analysable


# ------------------------------------------------------------------ saved → acquired

def test_saving_a_link_queues_its_acquisition(w, db, client, settings):
    settings.url_acquisition_enabled = False
    off = _save_link(client, w)
    assert not _acquire_jobs(db)
    settings.url_acquisition_enabled = True
    on = _save_link(client, w, url="https://www.twitch.tv/videos/24242424")
    (job,) = _acquire_jobs(db)
    assert job.source_id == on["id"] and job.status == "PENDING" and off["id"] != on["id"]


def test_a_refused_acquisition_never_blocks_saving_the_link(w, db, client, settings,
                                                          monkeypatch):
    settings.url_acquisition_enabled = True

    def refuse(*_a, **_k):
        raise RightsRefused(rights.Decision(False, rights.REVOKED, "acquire", None))
    monkeypatch.setattr(queue, "enqueue_acquire", refuse)
    src = _save_link(client, w)
    assert src["id"] and not _acquire_jobs(db)
    with db.session() as s:
        ev = s.scalars(select(SystemEvent).where(
            SystemEvent.event_type == "acquire_not_queued")).one()
        assert src["id"] in ev.message


@requires_ffmpeg
def test_public_footage_is_fetched_and_analysable_but_not_editable(
        w, db, client, settings, monkeypatch, media_dir):
    settings.url_acquisition_enabled = True
    fake = FakeDownloader(monkeypatch, media_dir / "basic.mp4")
    src = _save_link(client, w)                       # no licence, no form, no evidence
    drain(db, settings)                                # ACQUIRE → INGEST → METADATA → THUMBNAIL
    (job,) = _acquire_jobs(db)
    assert job.status == "COMPLETED" and len(fake.downloads) == 1
    with db.session() as s:
        rec = s.get(Recording, job.recording_id)
        assert rec.status == "READY" and rec.source_id == src["id"]
        assert rec.acquisition_method == "url_fetch"
        assert rights.evaluate_recording(s, rec.id, "analyze").allowed
        d = rights.evaluate_recording(s, rec.id, "edit", clip_ms=10_000)
        assert not d.allowed and d.reason == rights.CREATOR_PERMISSION_NEEDED


@requires_ffmpeg
def test_public_web_covers_fetched_media_only(w, db, client, settings, media_dir):
    """A copy imported by hand is not footage RIFTWIRE fetched: public_web does not
    cover it."""
    src = _save_link(client, w)
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        sources_rec = s.get(Recording, rec.id)
        sources_rec.source_id, sources_rec.acquisition_method = src["id"], "creator_file"
    with db.session() as s:
        d = rights.evaluate_recording(s, rec.id, "analyze")
        assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED


def _acquire_jobs(db) -> list[Job]:
    with db.session() as s:
        return list(s.scalars(select(Job).where(Job.job_type == "ACQUIRE")))


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


def test_downgrade_refuses_while_public_web_records_exist(w, db, client, settings):
    _save_link(client, w)
    db.dispose()
    with pytest.raises(Exception, match="public_web"):
        command.downgrade(_cfg(settings.database_url), "0008")
    assert _sql(settings, "SELECT version_num FROM alembic_version") == [("0009",)]
    assert len(_triggers(settings)) == 14


def test_sources_module_still_records_by_default(w, db):
    with db.session() as s:
        src = sources.create_source(s, url="https://www.twitch.tv/videos/31337",
                                    creator_channel_id=w.twitch)
        assert rights.record_public_web(s, src) is not None
