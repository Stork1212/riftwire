"""M2 rights & sources: fail-closed evaluation, revocation/supersession, worker enforcement
and the output fence, matching, acquisition, evidence limits, provenance and the API.
Everything is created by the tests themselves; nothing is seeded (R5)."""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.errors import (
    InsufficientStorage,
    PayloadTooLarge,
    RightsRefused,
    RightsRefusedJobError,
)
from app.db.base import utcnow
from app.main import create_app
from app.models import (
    Job,
    JobType,
    ProvenanceEvent,
    Recording,
    RightsEvidence,
    RightsRecord,
    Source,
    SystemEvent,
)
from app.services import (
    acquisition,
    clips,
    evidence,
    matching,
    media,
    queue,
    rights,
    sources,
)
from app.services.source_urls import parse_source_url
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.test_ingest import drain

CLIP = "https://www.twitch.tv/somestreamer/clip/BigHeistMoment-AbC_123"
VOD = "https://www.twitch.tv/videos/2201234567"
YT = "https://youtu.be/dQw4w9WgXcQ"
ATTEST = "I confirm the creator granted this licence in writing."

FULL_SCOPE = {"v": 1, "uses": ["acquire", "analyze", "edit", "publish"],
              "acquisition_methods": ["local_recording", "creator_file", "platform_export"],
              "publish_platforms": ["youtube_shorts"], "brand_channels": "*",
              "monetization": True}


class World:
    def __init__(self, db):
        self.db = db
        with db.session() as s:
            self.game = sources.create_game(s, "gta6", "GTA VI").id
            self.brand = sources.create_brand_channel(s, "rumbleframe", "RumbleFrame",
                                                      "youtube_shorts").id
            c = sources.create_creator(s, "Some Streamer", "partner")
            self.creator = c.id
            self.twitch = sources.add_channel(s, c.id, "twitch", handle="SomeStreamer").id
            self.local = sources.add_channel(s, c.id, "local").id

    def licence(self, scope=None, **kw) -> str:
        with self.db.session() as s:
            kw.setdefault("coverage", "creator")
            return rights.create_record(s, creator_id=self.creator, basis="licence",
                                        scope=scope or FULL_SCOPE, attestation_text=ATTEST,
                                        **kw).id

    def source(self, url=CLIP, **kw) -> str:
        with self.db.session() as s:
            src = sources.create_source(s, url=url, **kw)
            return src.id

    def decide(self, source_id, use, **kw) -> rights.Decision:
        with self.db.session() as s:
            return rights.evaluate(s, source_id, use, **kw)


@pytest.fixture
def w(db):
    return World(db)


# =========================================================================== evaluation

def test_no_rights_means_no(w):
    sid = w.source()
    for use in rights.USES:
        d = w.decide(sid, use)
        assert not d.allowed and d.reason == rights.NO_RIGHTS_RECORD
    assert w.decide("missing", "analyze").reason == rights.NO_SOURCE


def test_standing_licence_covers_new_sources_without_new_attestation(w):
    rid = w.licence()
    s1, s2 = w.source(CLIP), w.source(VOD, creator_channel_id=w.twitch)
    for sid in (s1, s2):
        d = w.decide(sid, "analyze")
        assert d.allowed and d.record_id == rid and d.version == 1
    with w.db.session() as s:
        assert s.scalar(select(func.count()).select_from(RightsRecord)) == 1


def test_draft_records_never_authorize(w):
    with w.db.session() as s:
        rights.create_record(s, creator_id=w.creator, basis="licence", coverage="creator",
                             scope=FULL_SCOPE, attest=False)
    d = w.decide(w.source(), "analyze")
    assert not d.allowed and d.reason == rights.NOT_ATTESTED


def test_attestation_requires_a_real_statement(w):
    with pytest.raises(Exception, match="attestation"), w.db.session() as s:
        rights.create_record(s, creator_id=w.creator, basis="licence", coverage="creator",
                             scope=FULL_SCOPE, attestation_text="ok")


def test_revocation_is_immediate_and_final(w):
    rid, sid = w.licence(), w.source()
    with w.db.session() as s:
        rights.revoke(s, rid, "creator withdrew permission")
    d = w.decide(sid, "analyze")
    assert not d.allowed and d.reason == rights.REVOKED
    with pytest.raises(Exception, match="Already revoked"), w.db.session() as s:
        rights.revoke(s, rid, "again")


def test_superseded_versions_cannot_authorize(w):
    rid, sid = w.licence(), w.source()
    with w.db.session() as s:
        new = rights.supersede(s, rid, scope={**FULL_SCOPE, "uses": ["acquire", "analyze"]},
                               attestation_text=ATTEST)
        new_id = new.id
    d = w.decide(sid, "analyze")
    assert d.allowed and d.record_id == new_id and d.version == 2
    assert w.decide(sid, "edit").reason == rights.USE_NOT_IN_SCOPE   # narrowed scope
    with w.db.session() as s:
        old = s.get(RightsRecord, rid)
        assert rights.record_state(old, utcnow()) == rights.SUPERSEDED
    with pytest.raises(Exception, match="current"), w.db.session() as s:
        rights.supersede(s, rid, scope=FULL_SCOPE, attestation_text=ATTEST)
    with w.db.session() as s:                                   # revoking the head
        rights.revoke(s, new_id, "ended")
    assert w.decide(sid, "analyze").reason == rights.REVOKED     # old one never revives


def test_validity_window(w):
    now = utcnow()
    w.licence(valid_from=now + timedelta(days=1), valid_until=now + timedelta(days=30))
    sid = w.source()
    assert w.decide(sid, "analyze").reason == rights.NOT_YET_VALID
    assert w.decide(sid, "analyze", at=now + timedelta(days=2)).allowed
    assert w.decide(sid, "analyze", at=now + timedelta(days=30)).reason == rights.EXPIRED


def test_unknown_scope_fails_closed(w):
    with w.db.session() as s:
        r = rights.create_record(s, creator_id=w.creator, basis="licence",
                                 coverage="creator", scope=FULL_SCOPE, attest=False)
        r.scope_json = json.dumps({**FULL_SCOPE, "future_field": True})   # draft: editable
        rights.attest_record(s, r, ATTEST)
    assert w.decide(w.source(), "analyze").reason == rights.SCOPE_UNSUPPORTED
    for bad in ({**FULL_SCOPE, "v": 2}, {**FULL_SCOPE, "extra": 1},
                {**FULL_SCOPE, "uses": []}, {**FULL_SCOPE, "uses": ["sell"]}):
        with pytest.raises(Exception, match="scope"), w.db.session() as s:
            rights.create_record(s, creator_id=w.creator, basis="licence",
                                 coverage="creator", scope=bad, attestation_text=ATTEST)


def test_specific_coverage_wins_and_channel_coverage(w):
    sid = w.source()
    w.licence(scope={**FULL_SCOPE, "uses": ["analyze"]})                  # creator-wide
    rid = w.licence(scope=FULL_SCOPE, coverage="sources", source_ids=[sid])
    assert w.decide(sid, "edit").record_id == rid                         # source-specific
    other = w.source(VOD, creator_channel_id=w.twitch)
    assert w.decide(other, "edit").reason == rights.USE_NOT_IN_SCOPE
    cid = w.licence(coverage="channels", channel_ids=[w.twitch])
    assert w.decide(other, "edit").record_id == cid


def test_acquisition_method_permissions(w, settings):
    w.licence(scope={**FULL_SCOPE, "acquisition_methods": ["creator_file", "url_fetch"]})
    sid = w.source(acquisition_method="creator_file")
    assert w.decide(sid, "acquire", method="creator_file").allowed
    assert w.decide(sid, "acquire", method="platform_export").reason == \
        rights.METHOD_NOT_PERMITTED
    assert w.decide(sid, "acquire", method="url_fetch").reason == \
        rights.URL_ACQUISITION_DISABLED
    assert w.decide(sid, "acquire", method="url_fetch",
                    url_acquisition_enabled=True).reason == rights.PLATFORM_PERMISSION_MISSING
    # A source obtained by a method the rights do not list cannot be analysed (R1).
    other = w.source(VOD, creator_channel_id=w.twitch, acquisition_method="platform_export")
    assert w.decide(other, "analyze").reason == rights.METHOD_NOT_PERMITTED


def test_publish_constraints_fail_closed(w, db):
    with db.session() as s:
        mon = sources.create_brand_channel(s, "rf-money", "RF Money", "youtube_shorts",
                                           monetized=True).id
        ig = sources.create_brand_channel(s, "rf-ig", "RF IG", "instagram_reels").id
    scope = {**FULL_SCOPE, "brand_channels": [w.brand, ig], "monetization": False,
             "max_clip_seconds": 60, "attribution_required": "Credit: @somestreamer",
             "publish_platforms": ["youtube_shorts", "instagram_reels"]}
    w.licence(scope=scope)
    sid = w.source(brand_channel_ids=[w.brand, mon])
    pub = dict(platform="youtube_shorts", brand_channel_id=w.brand, clip_ms=30_000)
    d = w.decide(sid, "publish", **pub)
    assert d.allowed and d.obligations == ({"type": "attribution",
                                           "text": "Credit: @somestreamer"},)
    cases = [
        (dict(pub, clip_ms=None), rights.CLIP_LENGTH_REQUIRED),
        (dict(pub, clip_ms=60_001), rights.CLIP_TOO_LONG),
        (dict(pub, brand_channel_id=None), rights.BRAND_CHANNEL_REQUIRED),
        (dict(pub, brand_channel_id=ig, platform="instagram_reels"),
         rights.BRAND_CHANNEL_NOT_ASSIGNED),
        (dict(pub, brand_channel_id=mon), rights.BRAND_CHANNEL_NOT_IN_SCOPE),
        (dict(pub, platform="x"), rights.PLATFORM_NOT_IN_SCOPE),
        (dict(pub, platform="instagram_reels"), rights.PLATFORM_NOT_IN_SCOPE),  # bc platform
    ]
    for kw, reason in cases:
        assert w.decide(sid, "publish", **kw).reason == reason, kw
    rights.require_obligations(d, {"attribution"})
    with pytest.raises(RightsRefused):
        rights.require_obligations(d, set())


def test_monetization_and_territories(w, db):
    with db.session() as s:
        mon = sources.create_brand_channel(s, "rf-money", "RF Money", "youtube_shorts",
                                           monetized=True).id
    rid = w.licence(scope={**FULL_SCOPE, "monetization": False})
    sid = w.source(brand_channel_ids=[mon])
    kw = dict(platform="youtube_shorts", brand_channel_id=mon, clip_ms=1000)
    assert w.decide(sid, "publish", **kw).reason == rights.MONETIZATION_NOT_PERMITTED
    with db.session() as s:
        rights.supersede(s, rid, scope={**FULL_SCOPE, "territories": ["CA"]},
                         attestation_text=ATTEST)
    assert w.decide(sid, "publish", **kw).reason == rights.TERRITORIES_UNSUPPORTED


def test_archived_creator_and_blocked_source(w, db):
    w.licence()
    sid = w.source()
    with db.session() as s:
        sources.update_source(s, sid, status="blocked")
    assert w.decide(sid, "analyze").reason == rights.SOURCE_BLOCKED
    with db.session() as s:
        sources.update_source(s, sid, status="active")
        sources.update_creator(s, w.creator, archived=True)
    assert w.decide(sid, "analyze").reason == rights.CREATOR_ARCHIVED


# =========================================================================== enforcement

def test_every_job_type_declares_its_rights_use():
    assert set(rights.JOB_USE) == set(JobType)
    assert all(v is None or v in rights.USES for v in rights.JOB_USE.values())
    # R2: Phase 1 housekeeping stays ungated.
    assert rights.JOB_USE[JobType.INGEST] is None


@pytest.fixture
def gated_metadata(monkeypatch):
    """Test-only: treat METADATA as an analysis job (no gated job type exists in M2)."""
    monkeypatch.setitem(rights.JOB_USE, JobType.METADATA, "analyze")


def _job(db, rid, jt):
    with db.session() as s:
        return s.scalar(select(Job).where(Job.recording_id == rid, Job.job_type == jt.value))


@requires_ffmpeg
def test_gated_job_never_queued_for_unlinked_media(db, settings, media_dir, gated_metadata):
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    drain(db, settings)
    assert _job(db, rec.id, JobType.INGEST).status == "COMPLETED"   # housekeeping ran
    assert _job(db, rec.id, JobType.METADATA) is None               # gated: not queued
    with db.session() as s:
        assert s.scalar(select(SystemEvent).where(SystemEvent.event_type == "rights_refused"))
        with pytest.raises(RightsRefused):
            queue.enqueue(s, rec.id, JobType.METADATA, 3)


@requires_ffmpeg
def test_worker_refuses_revoked_rights_permanently(w, db, settings, media_dir,
                                                   gated_metadata, monkeypatch):
    rid = w.licence()
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="local_recording")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id)
    # Let INGEST queue METADATA (allowed), then revoke before the worker claims it.
    from app.services import queue as q
    real = q.claim_next

    def claim_then_revoke(*a, **k):
        c = real(*a, **k)
        if c is not None and c.job_type == JobType.METADATA:
            with db.session() as s:
                if s.get(RightsRecord, rid).revoked_at is None:
                    rights.revoke(s, rid, "withdrawn")
        return c
    monkeypatch.setattr(q, "claim_next", claim_then_revoke)
    drain(db, settings)
    job = _job(db, rec.id, JobType.METADATA)
    assert job.status == "FAILED" and job.attempt_count == 1          # not retried
    assert "RightsRefused" in job.last_error and "revoked" in job.last_error
    with db.session() as s:
        assert s.scalar(select(ProvenanceEvent).where(
            ProvenanceEvent.entity == "job", ProvenanceEvent.action == "rights_refused"))


@requires_ffmpeg
def test_output_fence_refuses_completion_after_revocation(w, db, settings, media_dir,
                                                          gated_metadata):
    rid = w.licence()
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="local_recording")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id)
        job = queue.enqueue(s, rec.id, JobType.METADATA, 3)
        jid = job.id
    from app.workers.worker import WorkerProcess
    wp = WorkerProcess(settings, db)
    with db.session() as s:
        s.get(Job, jid).next_attempt_at = utcnow() - timedelta(seconds=1)
    c = None
    for _ in range(5):
        c = queue.claim_next(db, wp.id, 60)
        if c is not None and c.id == jid:
            break
        if c is not None:
            wp._execute(c)
    c = wp._authorize(c)
    assert c.decision is not None and c.decision.allowed
    with db.session() as s:                                   # revoked while "running"
        rights.revoke(s, rid, "withdrawn mid-job")
    applied = []
    with pytest.raises(RightsRefusedJobError):
        queue.complete(db, c, lambda s: applied.append(1))
    assert applied == []                                      # the output never landed
    assert _job(db, rec.id, JobType.METADATA).status == "RUNNING"


def test_output_fence_and_revocation_serialize(w, db):
    """A revocation racing an output: exactly one order happens, never both."""
    rid = w.licence()
    sid = w.source()
    d = w.decide(sid, "analyze")
    entered, release = threading.Event(), threading.Event()
    written = []

    def write(_s):
        entered.set()
        release.wait(5)
        written.append(utcnow())

    t = threading.Thread(target=rights.authorize_output, args=(db, d, write))
    t.start()
    assert entered.wait(5)
    revoked_at = {}

    def do_revoke():
        with db.session() as s:
            revoked_at["t"] = rights.revoke(s, rid, "race").revoked_at
    r = threading.Thread(target=do_revoke)
    r.start()
    time.sleep(0.3)
    assert "t" not in revoked_at                  # blocked behind the output transaction
    release.set()
    t.join(5)
    r.join(10)
    assert written and revoked_at["t"] >= written[0]
    with pytest.raises(RightsRefused):            # afterwards: the same decision is refused
        rights.authorize_output(db, d, lambda _s: written.append("late"))
    assert len(written) == 1


def test_fence_refuses_after_supersession(w, db):
    rid, sid = w.licence(), w.source()
    d = w.decide(sid, "analyze")
    with db.session() as s:
        rights.supersede(s, rid, scope=FULL_SCOPE, attestation_text=ATTEST)
    with pytest.raises(RightsRefused) as e:
        rights.authorize_output(db, d, lambda _s: None)
    assert e.value.details["reason"] == rights.RIGHTS_CHANGED


# =========================================================================== matching

def test_twitch_clip_matches_its_creator_channel(w, db):
    with db.session() as s:
        m = matching.match(s, matching.Hint(parsed_url=parse_source_url(CLIP)))
        assert m.status == matching.MATCHED and m.creator_channel_id == w.twitch
        m = matching.match(s, matching.Hint(parsed_url=parse_source_url(
            "https://www.twitch.tv/otherperson/clip/Xyz")))
        assert m.status == matching.UNMATCHED
        m = matching.match(s, matching.Hint(parsed_url=parse_source_url(YT)))
        assert m.status == matching.UNMATCHED and "M3" in m.reason


def test_source_creation_auto_matches_and_dedupes(w, db):
    sid = w.source(CLIP)                                   # no channel given: matched
    with db.session() as s:
        assert s.get(Source, sid).creator_channel_id == w.twitch
    with pytest.raises(Exception, match="already a source"):
        w.source("https://clips.twitch.tv/BigHeistMoment-AbC_123")
    with pytest.raises(Exception, match="creator channel"):
        w.source(YT)                                       # YouTube: owner must choose
    with db.session() as s:
        m = matching.match(s, matching.Hint(parsed_url=parse_source_url(CLIP)))
        assert m.matcher == "existing_source" and m.source_id == sid


@requires_ffmpeg
def test_file_hash_matcher(w, db, settings, media_dir):
    w.licence()
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="local_recording")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id)
        m = matching.match(s, matching.Hint(sha256=rec.sha256))
        assert m.status == matching.MATCHED and m.source_id == sid


# =========================================================================== acquisition

@requires_ffmpeg
def test_attach_checks_both_permissions(w, db, settings, media_dir):
    w.licence(scope={**FULL_SCOPE, "acquisition_methods": ["creator_file"]})
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="creator_file")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with pytest.raises(acquisition.AcquisitionUnavailable), db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id, "direct_link")
    with pytest.raises(RightsRefused), db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id, "platform_export")
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id)          # creator_file
        assert s.get(Recording, rec.id).source_id == sid


def test_network_methods_are_declared_but_unavailable():
    avail = {m for m, p in acquisition.PROVIDERS.items() if p.available}
    assert avail == {"local_recording", "creator_file", "platform_export"}
    assert set(acquisition.PROVIDERS) == {
        "local_recording", "creator_file", "platform_export", "direct_link",
        "platform_api", "watch_folder", "url_fetch"}


# =========================================================================== evidence

def test_evidence_is_documentation_not_proof(w, db, settings):
    with db.session() as s:
        r = rights.create_record(s, creator_id=w.creator, basis="licence",
                                 coverage="creator", scope=FULL_SCOPE, attest=False)
        rid = r.id
    ev = evidence.add_evidence(db, settings, rid, kind="platform_permission",
                               reference="email from partner manager")
    assert ev.stored_name is None
    sid = w.source()
    assert w.decide(sid, "analyze").reason == rights.NOT_ATTESTED     # still a draft


def test_evidence_file_stored_read_only_with_hash(w, db, settings):
    rid = w.licence()
    data = b"%PDF-1.4 licence " * 100
    ev = evidence.add_evidence(db, settings, rid, kind="document", filename="licence.pdf",
                               content_length=len(data), chunks=[data[:500], data[500:]])
    p = settings.evidence_dir / ev.stored_name
    assert p.read_bytes() == data and oct(p.stat().st_mode & 0o777) == "0o444"
    assert ev.sha256 and ev.size_bytes == len(data) and ev.original_filename == "licence.pdf"
    assert [x.name for x in settings.evidence_dir.iterdir()] == [ev.stored_name]


def test_evidence_limits(w, db, settings, monkeypatch):
    rid = w.licence()
    with pytest.raises(PayloadTooLarge):
        evidence.add_evidence(db, settings, rid, kind="document", filename="big.pdf",
                              content_length=settings.evidence_max_file_bytes + 1)
    monkeypatch.setattr(settings, "evidence_max_total_bytes", 1500)
    evidence.add_evidence(db, settings, rid, kind="document", filename="a.pdf",
                          content_length=1000, chunks=[b"x" * 1000])
    with pytest.raises(evidence.EvidenceQuotaExceeded):
        evidence.add_evidence(db, settings, rid, kind="document", filename="b.pdf",
                              content_length=600, chunks=[b"x" * 600])
    with pytest.raises(Exception, match="one of"):
        evidence.add_evidence(db, settings, rid, kind="document", filename="x.exe",
                              content_length=10, chunks=[b"x" * 10])
    with pytest.raises(Exception, match="larger"):
        evidence.add_evidence(db, settings, rid, kind="document", filename="c.pdf",
                              content_length=10, chunks=[b"x" * 11])
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(RightsEvidence)) == 1
    assert len([p for p in settings.evidence_dir.iterdir()]) == 1     # no temp left


def test_evidence_respects_the_storage_floor(w, db, settings, monkeypatch):
    rid = w.licence()
    monkeypatch.setattr(evidence, "free_bytes", lambda _p: settings.min_free_disk_bytes)
    with pytest.raises(InsufficientStorage):
        evidence.add_evidence(db, settings, rid, kind="document", filename="a.pdf",
                              content_length=100, chunks=[b"x" * 100])
    assert list(settings.evidence_dir.iterdir()) == []


def test_concurrent_evidence_never_exceeds_the_total(w, db, settings, monkeypatch):
    rid = w.licence()
    monkeypatch.setattr(settings, "evidence_max_total_bytes", 2500)
    errors = []

    def up(i):
        try:
            evidence.add_evidence(db, settings, rid, kind="document", filename=f"{i}.pdf",
                                  content_length=1000, chunks=[b"x" * 1000])
        except PayloadTooLarge as e:
            errors.append(e)
    ts = [threading.Thread(target=up, args=(i,)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    with db.session() as s:
        assert evidence.total_bytes(s) <= 2500
        assert s.scalar(select(func.count()).select_from(RightsEvidence)) == 2
    assert len(errors) == 3


# =========================================================================== API

@pytest.fixture
def client(db, settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_api_end_to_end_paste_url_flow(client, db):
    c = client
    g = c.post("/api/v1/games", json={"slug": "gta6", "name": "GTA VI"}).json()
    bc = c.post("/api/v1/brand-channels", json={"slug": "rumbleframe", "name": "RumbleFrame",
                                               "platform": "youtube_shorts"}).json()
    cr = c.post("/api/v1/creators", json={"display_name": "Some Streamer",
                                         "relationship": "partner"}).json()
    ch = c.post(f"/api/v1/creators/{cr['id']}/channels",
                json={"platform": "twitch", "handle": "somestreamer"}).json()
    r = c.post("/api/v1/resolve-does-not-exist")
    assert r.status_code in (404, 405)
    res = c.post("/api/v1/sources/resolve", json={"url": CLIP}).json()
    assert res["match"]["status"] == "matched" and res["channel"]["id"] == ch["id"]
    assert res["covered"] is False and res["existing_source"] is None
    lic = c.post("/api/v1/rights", json={
        "creator_id": cr["id"], "basis": "licence", "coverage": "creator",
        "scope": {**FULL_SCOPE, "brand_channels": [bc["id"]]},
        "attestation_text": ATTEST}).json()
    assert lic["state"] == "ok" and lic["version"] == 1
    assert c.post("/api/v1/sources/resolve", json={"url": CLIP}).json()["covered"] is True
    src = c.post("/api/v1/sources", json={"url": CLIP, "game_id": g["id"],
                                         "brand_channel_ids": [bc["id"]]})
    assert src.status_code == 201, src.text
    src = src.json()
    assert src["rights_status"] == "active" and src["creator_name"] == "Some Streamer"
    dup = c.post("/api/v1/sources", json={"url": CLIP})
    assert dup.status_code == 409 and dup.json()["error"]["details"]["source_id"] == src["id"]
    chk = c.get(f"/api/v1/sources/{src['id']}/rights-check",
                params={"use": "publish", "platform": "youtube_shorts",
                        "brand_channel_id": bc["id"], "clip_ms": 30000}).json()
    assert chk["allowed"] is True
    detail = c.get(f"/api/v1/sources/{src['id']}").json()
    assert detail["analyze"]["allowed"] and detail["records"][0]["id"] == lic["id"]
    rv = c.post(f"/api/v1/rights/{lic['id']}/revoke", json={"reason": "ended"})
    assert rv.json()["state"] == "revoked"
    assert c.get(f"/api/v1/sources/{src['id']}").json()["rights_status"] == "revoked"
    prov = c.get(f"/api/v1/sources/{src['id']}/provenance").json()
    actions = [(p["entity"], p["action"]) for p in prov]
    for expected in [("creator", "created"), ("creator", "channel_added"),
                     ("rights", "created"), ("rights", "attested"), ("source", "created"),
                     ("source", "assignments_set"), ("rights", "revoked")]:
        assert expected in actions
    assert [p["id"] for p in prov] == sorted(p["id"] for p in prov)


def test_api_validation_errors(client, w):
    c = client
    assert c.post("/api/v1/sources/resolve", json={"url": "https://vimeo.com/1"}).json()[
        "error"]["code"] == "unsupported_host"
    assert c.post("/api/v1/sources", json={"url": YT}).json()["error"]["code"] == \
        "creator_channel_required"
    assert c.post("/api/v1/creators", json={"display_name": "X", "relationship": "self",
                                           "extra": 1}).status_code == 422
    assert c.post("/api/v1/rights", json={
        "creator_id": w.creator, "basis": "licence", "coverage": "creator",
        "scope": {**FULL_SCOPE, "v": 2}, "attestation_text": ATTEST}).status_code == 422
    assert c.post("/api/v1/games", json={"slug": "gta6", "name": "dup"}).status_code == 409
    assert c.get("/api/v1/sources/missing").status_code == 404
    assert c.get("/api/v1/acquisition-methods").json()[0]["method"] == "local_recording"


def test_api_evidence_upload_and_download(client, w, settings):
    rid = w.licence()
    data = b"licence text"
    r = client.post(f"/api/v1/rights/{rid}/evidence",
                    params={"kind": "document", "filename": "licence.txt"}, content=data)
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["has_file"] and ev["size_bytes"] == len(data)
    f = client.get(ev["file_url"])
    assert f.status_code == 200 and f.content == data
    big = client.post(f"/api/v1/rights/{rid}/evidence",
                      params={"kind": "document", "filename": "x.pdf"},
                      headers={"content-length": str(settings.evidence_max_file_bytes + 1)},
                      content=b"")
    assert big.status_code in (413, 422)
    note = client.post(f"/api/v1/rights/{rid}/evidence",
                       params={"kind": "note", "reference": "phone call 2026-11-20"})
    assert note.status_code == 201 and not note.json()["has_file"]


@requires_ffmpeg
def test_api_import_with_standing_licence_links_every_file(client, w, media_dir, db):
    w.licence()
    for name in ("basic.mp4", "basic.mkv"):
        data = (media_dir / name).read_bytes()
        r = client.post("/api/v1/recordings/import",
                        params={"filename": name, "creator_channel_id": w.local},
                        content=data)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["source_id"] and body["source_error"] is None
        assert body["recording"]["source_id"] == body["source_id"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(RightsRecord)) == 1   # one licence


@requires_ffmpeg
def test_api_import_refused_before_any_byte_without_rights(client, w, media_dir, db,
                                                           settings):
    data = (media_dir / "basic.mp4").read_bytes()
    r = client.post("/api/v1/recordings/import",
                    params={"filename": "basic.mp4", "creator_channel_id": w.local},
                    content=data)
    assert r.status_code == 403 and r.json()["error"]["code"] == "rights_refused"
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Recording)) == 0
        assert s.scalar(select(func.count()).select_from(Source)) == 0   # dry run rolled back
    assert [p.name for p in settings.temp_dir.iterdir() if not p.name.startswith(".")] == []


@requires_ffmpeg
def test_clip_cut_checks_edit_rights_and_length(w, client, db, settings, media_dir, monkeypatch):
    scope = {**FULL_SCOPE, "max_clip_seconds": 1}
    licence = w.licence(scope=scope)
    source_id = w.source(url=None, kind="local", creator_channel_id=w.local)
    recording = import_file(db, settings, media_dir / "basic.mp4").recording
    drain(db, settings)
    path = f"/api/v1/recordings/{recording.id}/clip"
    params = {"start_ms": 0, "end_ms": 1000}
    assert client.post(path, params=params).status_code == 403

    with db.session() as session:
        acquisition.attach_media(session, settings, source_id, recording.id)
    too_long = client.post(path, params={"start_ms": 0, "end_ms": 1500})
    assert too_long.status_code == 403
    assert too_long.json()["error"]["details"]["reason"] == rights.CLIP_TOO_LONG
    result = client.post(path, params=params)
    assert result.status_code == 200, result.text
    assert result.headers["content-type"] == "video/mp4"
    assert result.content[4:8] == b"ftyp"

    monkeypatch.setattr(clips, "_transcribe", lambda *_: [
        {"w": "Clutch!", "start_ms": 100, "end_ms": 800},
    ])
    result = client.post(path, params={**params, "subtitles": "true"})
    assert result.status_code == 200, result.text
    assert b"tx3g" in result.content
    assert b"Clutch!" in result.content

    settings.clip_outro_path = media_dir / "basic.mp4"
    result = client.post(path, params={**params, "layout": "split_facecam", "title": "Clutch!"})
    assert result.status_code == 200, result.text
    assert b"tx3g" not in result.content
    rendered = settings.temp_dir / "verified-short.mp4"
    rendered.write_bytes(result.content)
    info = media.probe(settings.ffprobe_path, rendered, settings.ffprobe_timeout_seconds)
    assert (info.width, info.height) == (1080, 1920)
    assert 2950 <= info.duration_ms <= 3100

    with db.session() as session:
        rights.revoke(session, licence, "rights withdrawn")
    assert client.post(path, params=params).status_code == 403

