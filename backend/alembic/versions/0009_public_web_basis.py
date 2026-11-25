"""public_web rights basis (a classification; authorizes nothing since 2026-11-25)

Also adds the creator relationship ``unaffiliated`` (no relationship: a creator whose public
footage was saved by link), so such creators are not recorded as partners or licensors.

Schema only. When written (``b13b91f``), the application recorded a ``public_web`` row
automatically for each saved link, authorizing fetch and analysis. Owner decision
2026-11-25 superseded that: public availability is not permission. The application writes
no new ``public_web`` rows, and ``rights.evaluate`` refuses every use under existing ones
(``creator_permission_needed``). The basis stays in the schema so that this immutable
history remains valid.

Rebuilds ``rights_records`` (SQLite batch). Every trigger that mentions the table is dropped
first and recreated afterwards from the frozen copies below, inside the same migration
transaction (alembic/env.py): the batch rebuild would otherwise drop the four triggers on
the table, and renaming the rebuilt table under triggers that reference it is not relied
on. Foreign keys are off while migrating and checked before commit.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24 23:50:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

_REL_OLD = "relationship IN ('self','partner','licensor')"
_REL_NEW = "relationship IN ('self','partner','licensor','unaffiliated')"
_BASES_OLD = "basis IN ('ownership','creator_permission','licence')"
_BASES_NEW = "basis IN ('ownership','creator_permission','licence','public_web')"

# Frozen copies (0004 and 0006) of every trigger whose definition mentions rights_records.
_CONTENT = ("id, lineage_id, version, creator_id, basis, coverage, scope_json, scope_sha256, "
            "valid_from, valid_until, attestation_text, attested_by, attested_at, created_at")
_WHEN_UPD = """
        WHEN (SELECT attested_at FROM rights_records WHERE id = OLD.rights_record_id)
             IS NOT NULL
          OR (SELECT attested_at FROM rights_records WHERE id = NEW.rights_record_id)
             IS NOT NULL"""
TRIGGERS: dict[str, str] = {
    "trg_rights_attested_immutable": f"""
        CREATE TRIGGER trg_rights_attested_immutable
        BEFORE UPDATE OF {_CONTENT} ON rights_records
        WHEN OLD.attested_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'attested rights records are immutable'); END""",
    "trg_rights_superseded_once": """
        CREATE TRIGGER trg_rights_superseded_once
        BEFORE UPDATE OF superseded_by_id ON rights_records
        WHEN OLD.superseded_by_id IS NOT NULL
             OR OLD.attested_at IS NULL
             OR NEW.superseded_by_id IS NULL
        BEGIN SELECT RAISE(ABORT, 'supersession is set once, on attested records only'); END""",
    "trg_rights_revoked_once": """
        CREATE TRIGGER trg_rights_revoked_once
        BEFORE UPDATE OF revoked_at, revoked_reason ON rights_records
        WHEN OLD.revoked_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'revocation is permanent'); END""",
    "trg_rights_attested_no_delete": """
        CREATE TRIGGER trg_rights_attested_no_delete
        BEFORE DELETE ON rights_records
        WHEN OLD.attested_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'attested rights records cannot be deleted'); END""",
    "trg_rights_channels_frozen_ins": """
        CREATE TRIGGER trg_rights_channels_frozen_ins
        BEFORE INSERT ON rights_record_channels
        WHEN (SELECT attested_at FROM rights_records WHERE id = NEW.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_channels_frozen_del": """
        CREATE TRIGGER trg_rights_channels_frozen_del
        BEFORE DELETE ON rights_record_channels
        WHEN (SELECT attested_at FROM rights_records WHERE id = OLD.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_channels_frozen_upd": f"""
        CREATE TRIGGER trg_rights_channels_frozen_upd
        BEFORE UPDATE ON rights_record_channels{_WHEN_UPD}
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_sources_frozen_ins": """
        CREATE TRIGGER trg_rights_sources_frozen_ins
        BEFORE INSERT ON rights_source_links
        WHEN (SELECT attested_at FROM rights_records WHERE id = NEW.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_sources_frozen_del": """
        CREATE TRIGGER trg_rights_sources_frozen_del
        BEFORE DELETE ON rights_source_links
        WHEN (SELECT attested_at FROM rights_records WHERE id = OLD.rights_record_id)
             IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_sources_frozen_upd": f"""
        CREATE TRIGGER trg_rights_sources_frozen_upd
        BEFORE UPDATE ON rights_source_links{_WHEN_UPD}
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
}


def _drop_triggers() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _create_triggers() -> None:
    for sql in TRIGGERS.values():
        op.execute(sql)


def upgrade() -> None:
    _drop_triggers()
    with op.batch_alter_table('rights_records', schema=None, recreate='always') as b:
        b.drop_constraint(b.f('ck_rights_records_basis_valid'), type_='check')
        b.create_check_constraint(b.f('ck_rights_records_basis_valid'), _BASES_NEW)
        b.create_check_constraint(b.f('ck_rights_records_public_web_per_source'),
                                  "basis != 'public_web' OR coverage = 'sources'")
    _create_triggers()
    with op.batch_alter_table('creators', schema=None) as b:
        b.drop_constraint(b.f('ck_creators_relationship_valid'), type_='check')
        b.create_check_constraint(b.f('ck_creators_relationship_valid'), _REL_NEW)


def downgrade() -> None:
    # Attested records are immutable and their history is permanent: public_web records
    # cannot be deleted or re-based to fit the older schema, so the downgrade refuses.
    bind = op.get_bind()
    n = bind.execute(sa.text(
        "SELECT COUNT(*) FROM rights_records WHERE basis = 'public_web'")).scalar()
    if n:
        raise RuntimeError(f"{n} public_web rights record(s) exist; this downgrade would "
                           "have to delete or rewrite immutable rights history. Nothing "
                           "was changed.")
    m = bind.execute(sa.text(
        "SELECT COUNT(*) FROM creators WHERE relationship = 'unaffiliated'")).scalar()
    if m:
        raise RuntimeError(f"{m} unaffiliated creator(s) exist; set their relationship "
                           "before downgrading. Nothing was changed.")
    with op.batch_alter_table('creators', schema=None) as b:
        b.drop_constraint(b.f('ck_creators_relationship_valid'), type_='check')
        b.create_check_constraint(b.f('ck_creators_relationship_valid'), _REL_OLD)
    _drop_triggers()
    with op.batch_alter_table('rights_records', schema=None, recreate='always') as b:
        b.drop_constraint(b.f('ck_rights_records_public_web_per_source'), type_='check')
        b.drop_constraint(b.f('ck_rights_records_basis_valid'), type_='check')
        b.create_check_constraint(b.f('ck_rights_records_basis_valid'), _BASES_OLD)
    _create_triggers()
