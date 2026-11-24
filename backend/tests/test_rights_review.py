"""Regression tests for the independent M2 review of 9cbe705 (docs/M2_REVIEW.md §7).
Each was written to reproduce a suspected defect before any fix."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Recording, RightsRecord
from app.services import acquisition, rights, sources
from app.services.source_urls import SourceUrlError, parse_source_url
from tests.conftest import requires_ffmpeg
from tests.helpers import import_file
from tests.test_rights import ATTEST, FULL_SCOPE, World


@pytest.fixture
def w(db):
    return World(db)


# ---------------------------------------------------------------- finding A: actual method

@requires_ffmpeg
def test_actual_acquisition_method_is_recorded_and_enforced(w, db, settings, media_dir):
    """Media attached via creator_file to a source declared as platform_export: when the
    licence stops permitting creator_file, analysing that media must be refused even though
    the source's declared method is still permitted."""
    rid = w.licence(scope={**FULL_SCOPE,
                           "acquisition_methods": ["platform_export", "creator_file"]})
    sid = w.source(kind="local", url=None, creator_channel_id=w.local,
                   acquisition_method="platform_export")
    rec = import_file(db, settings, media_dir / "basic.mp4").recording
    with db.session() as s:
        acquisition.attach_media(s, settings, sid, rec.id, "creator_file")
        assert s.get(Recording, rec.id).acquisition_method == "creator_file"
        assert rights.evaluate_recording(s, rec.id, "analyze").allowed
        rights.supersede(s, rid, scope={**FULL_SCOPE, "acquisition_methods":
                                        ["platform_export"]}, attestation_text=ATTEST)
    with db.session() as s:
        d = rights.evaluate_recording(s, rec.id, "analyze")
        assert not d.allowed and d.reason == rights.METHOD_NOT_PERMITTED


# ---------------------------------------------------------------- finding B: creator authority

def test_source_specific_rights_only_for_the_creators_own_sources(w, db):
    with db.session() as s:
        other = sources.create_creator(s, "Someone Else", "partner")
        ch = sources.add_channel(s, other.id, "twitch", handle="someoneelse")
        their = sources.create_source(s, url="https://www.twitch.tv/videos/555",
                                      creator_channel_id=ch.id, record_public_web=False).id
    with pytest.raises(Exception, match="belong"), db.session() as s:
        rights.create_record(s, creator_id=w.creator, basis="licence", coverage="sources",
                             source_ids=[their], scope=FULL_SCOPE, attestation_text=ATTEST)
    assert not w.decide(their, "analyze").allowed
    with db.session() as s:
        assert s.scalar(select(RightsRecord.id)) is None


# ---------------------------------------------------------------- finding C: badge semantics

def test_badge_distinguishes_active_licence_from_authorized_uses(w, db, client):
    w.licence(scope={**FULL_SCOPE, "uses": ["acquire"]})
    sid = w.source()
    src = client.get(f"/api/v1/sources/{sid}").json()
    assert src["rights_status"] == "active"                     # a licence is in force …
    assert src["authorized_uses"] == ["acquire"]                # … but only for acquiring
    assert src["analyze"]["allowed"] is False
    listed = client.get("/api/v1/sources").json()["items"][0]
    assert listed["rights_status"] == "active" and listed["authorized_uses"] == ["acquire"]


def test_unusable_scope_is_not_shown_as_active(w, db, client):
    with db.session() as s:
        r = rights.create_record(s, creator_id=w.creator, basis="licence",
                                 coverage="creator", scope=FULL_SCOPE, attest=False)
        r.scope_json = '{"v": 1, "uses": ["analyze"], "future": 1}'
        rights.attest_record(s, r, ATTEST)
    sid = w.source()
    src = client.get(f"/api/v1/sources/{sid}").json()
    assert src["rights_status"] == "unusable" and src["authorized_uses"] == []


# ---------------------------------------------------------------- actionable refusals

def test_refusals_carry_an_actionable_next_step(w, client):
    sid = w.source()
    r = client.get(f"/api/v1/sources/{sid}").json()["analyze"]
    assert r["reason"] == "no_rights_record" and "Creators & Rights" in r["action"]
    up = client.post("/api/v1/recordings/import",
                     params={"filename": "x.mp4", "creator_channel_id": w.local}, content=b"x")
    err = up.json()["error"]
    assert up.status_code == 403 and err["details"]["action"]


# ---------------------------------------------------------------- races and validation

def test_concurrent_supersession_has_exactly_one_winner(w, db):
    rid = w.licence()
    results = []

    def edit(i):
        try:
            with db.session() as s:
                rights.supersede(s, rid, scope={**FULL_SCOPE, "notes": str(i)},
                                 attestation_text=ATTEST)
            results.append("ok")
        except Exception as e:  # noqa: BLE001 - the loser's error type is asserted below
            results.append(type(e).__name__)
    ts = [threading.Thread(target=edit, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count("ok") == 1 and results.count("ConflictError") == 3
    with db.session() as s:
        heads = [r for r in s.scalars(select(RightsRecord))
                 if r.superseded_by_id is None and r.revoked_at is None]
        assert len(heads) == 1 and heads[0].version == 2


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=aaaaaaaaaaa",       # two different ids
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ%26v%3Daaaaaaaaaaa",   # encoded smuggling
])
def test_ambiguous_youtube_ids_are_refused(url):
    with pytest.raises(SourceUrlError):
        parse_source_url(url)


def test_repeated_identical_id_is_fine():
    p = parse_source_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=dQw4w9WgXcQ")
    assert p.video_id == "dQw4w9WgXcQ"


@pytest.fixture
def client(db, settings):
    with TestClient(create_app(settings)) as c:
        yield c


