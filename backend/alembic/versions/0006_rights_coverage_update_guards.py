"""freeze attested coverage against UPDATE (independent audit of 63a2e5e, finding 3)

0004 froze the coverage links of an attested rights record against INSERT and DELETE, but
an UPDATE could still re-point a link: move it onto an attested record (adding coverage),
off one (removing coverage), or change which channel or source an attested link names.
These triggers refuse any UPDATE of a link row when either the old or the new parent record
is attested. Links between draft records stay editable.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24 19:40:00
"""
from __future__ import annotations

from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

_WHEN = """
        WHEN (SELECT attested_at FROM rights_records WHERE id = OLD.rights_record_id)
             IS NOT NULL
          OR (SELECT attested_at FROM rights_records WHERE id = NEW.rights_record_id)
             IS NOT NULL"""

TRIGGERS: dict[str, str] = {
    "trg_rights_channels_frozen_upd": f"""
        CREATE TRIGGER trg_rights_channels_frozen_upd
        BEFORE UPDATE ON rights_record_channels{_WHEN}
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
    "trg_rights_sources_frozen_upd": f"""
        CREATE TRIGGER trg_rights_sources_frozen_upd
        BEFORE UPDATE ON rights_source_links{_WHEN}
        BEGIN SELECT RAISE(ABORT, 'coverage of an attested record is frozen'); END""",
}


def upgrade() -> None:
    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
