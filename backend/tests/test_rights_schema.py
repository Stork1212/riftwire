"""M2 schema (migration 0004): immutability enforced by the database itself (plan §12.6)
and nothing seeded (R5)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

from app.db.base import utcnow
from app.models import (
    BrandChannel,
    Creator,
    CreatorChannel,
    Game,
    ProvenanceEvent,
    RightsEvidence,
    RightsRecord,
    RightsRecordChannel,
    Source,
)


def _creator(s) -> Creator:
    c = Creator(display_name="Test Creator", relationship="partner")
    s.add(c)
    s.flush()
    return c


def _record(s, creator_id: str, attested: bool) -> RightsRecord:
    r = RightsRecord(creator_id=creator_id, basis="licence", coverage="creator",
                     scope_json='{"v": 1}', scope_sha256="0" * 64, valid_from=utcnow(),
                     attestation_text="I confirm" if attested else None,
                     attested_by="owner" if attested else None,
                     attested_at=utcnow() if attested else None)
    s.add(r)
    s.flush()
    return r


def test_nothing_is_seeded(db):
    with db.session() as s:
        for model in (Game, BrandChannel, Creator, CreatorChannel, Source, RightsRecord,
                      ProvenanceEvent):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_draft_records_are_editable_and_deletable(db):
    with db.session() as s:
        r = _record(s, _creator(s).id, attested=False)
        r.scope_json = '{"v": 1, "uses": ["analyze"]}'
        s.flush()
        s.delete(r)


@pytest.mark.parametrize("column,value", [
    ("scope_json", "'{}'"), ("basis", "'ownership'"), ("coverage", "'sources'"),
    ("valid_until", "'2030-01-01 00:00:00'"), ("attestation_text", "'changed'"),
    ("attested_at", "NULL"), ("version", "2"), ("creator_id", "'x'"),
])
def test_attested_record_content_is_immutable(db, column, value):
    with db.session() as s:
        rid = _record(s, _creator(s).id, attested=True).id
    with pytest.raises(DatabaseError, match="immutable"), db.session() as s:
        s.execute(text(f"UPDATE rights_records SET {column} = {value} WHERE id = :i"),
                  {"i": rid})


def test_attested_record_cannot_be_deleted(db):
    with db.session() as s:
        rid = _record(s, _creator(s).id, attested=True).id
    with pytest.raises(DatabaseError, match="cannot be deleted"), db.session() as s:
        s.execute(text("DELETE FROM rights_records WHERE id = :i"), {"i": rid})


def test_supersession_and_revocation_are_set_once(db):
    with db.session() as s:
        c = _creator(s)
        old, new = _record(s, c.id, attested=True), _record(s, c.id, attested=True)
        draft = _record(s, c.id, attested=False)
        ids = old.id, new.id, draft.id
    old_id, new_id, draft_id = ids
    with db.session() as s:
        s.execute(text("UPDATE rights_records SET superseded_by_id = :n WHERE id = :o"),
                  {"n": new_id, "o": old_id})
    for sql, params in [
        ("UPDATE rights_records SET superseded_by_id = :n WHERE id = :o",
         {"n": draft_id, "o": old_id}),                     # cannot be changed
        ("UPDATE rights_records SET superseded_by_id = NULL WHERE id = :o", {"o": old_id}),
        ("UPDATE rights_records SET superseded_by_id = :n WHERE id = :o",
         {"n": new_id, "o": draft_id}),                     # drafts are never superseded
    ]:
        with pytest.raises(DatabaseError), db.session() as s:
            s.execute(text(sql), params)
    with db.session() as s:
        rec = s.get(RightsRecord, new_id)
        rec.revoked_at, rec.revoked_reason = utcnow(), "creator withdrew permission"
    with pytest.raises(DatabaseError, match="permanent"), db.session() as s:
        s.execute(text("UPDATE rights_records SET revoked_at = NULL WHERE id = :i"),
                  {"i": new_id})


def test_coverage_of_attested_record_is_frozen(db):
    with db.session() as s:
        c = _creator(s)
        ch = CreatorChannel(creator_id=c.id, platform="twitch", handle="somestreamer")
        s.add(ch)
        s.flush()
        draft = _record(s, c.id, attested=False)
        draft.coverage = "channels"
        s.add(RightsRecordChannel(rights_record_id=draft.id, creator_channel_id=ch.id))
        s.flush()
        rid, chid = draft.id, ch.id
    with db.session() as s:                                    # attest the draft
        s.get(RightsRecord, rid).attested_at = utcnow()
    with pytest.raises(DatabaseError, match="frozen"), db.session() as s:
        s.execute(text("DELETE FROM rights_record_channels WHERE rights_record_id = :i"),
                  {"i": rid})
    with db.session() as s:
        ch2 = CreatorChannel(creator_id=s.get(CreatorChannel, chid).creator_id,
                             platform="youtube", handle="other")
        s.add(ch2)
        s.flush()
        ch2_id = ch2.id
    with pytest.raises(DatabaseError, match="frozen"), db.session() as s:
        s.add(RightsRecordChannel(rights_record_id=rid, creator_channel_id=ch2_id))


def test_evidence_and_provenance_are_append_only(db):
    with db.session() as s:
        rid = _record(s, _creator(s).id, attested=True).id
        s.add(RightsEvidence(rights_record_id=rid, kind="note", reference="call notes"))
        s.add(ProvenanceEvent(entity="rights", entity_id=rid, action="attested",
                              actor="owner"))
    for sql in ["UPDATE rights_evidence SET reference = 'x'", "DELETE FROM rights_evidence",
                "UPDATE provenance_events SET action = 'x'", "DELETE FROM provenance_events"]:
        with pytest.raises(DatabaseError, match="append-only"), db.session() as s:
            s.execute(text(sql))


def test_existing_recordings_have_no_source_and_zero_offset(db, settings):
    with db.session() as s:
        cols = {r[1]: r for r in s.execute(text("PRAGMA table_info(recordings)"))}
    assert cols["source_id"][3] == 0                       # nullable: rights not attested
    assert cols["source_offset_ms"][4] == "'0'"            # server default for old rows
