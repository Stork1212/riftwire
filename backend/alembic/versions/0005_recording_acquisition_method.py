"""recordings.acquisition_method: how linked media was actually obtained (M2 review)

Rights checks for analysis, editing and publishing use the recording's actual acquisition
method, not only the source's declared one.

Backfill (independent audit of 63a2e5e): the method actually used for already-linked media
comes from its attachment provenance. An explicit owner confirmation takes precedence
over inconclusive attachment provenance. When the relevant history is missing, unknown
or ambiguous the column stays NULL, and rights-gated uses of that recording stay refused
(``acquisition_method_unknown``) until the owner records how the footage was obtained.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24 17:10:00
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

# Frozen copy of AcquisitionMethod values at this revision (migrations never import app code).
_METHODS = frozenset({"local_recording", "creator_file", "platform_export", "direct_link",
                      "platform_api", "watch_folder", "url_fetch"})


_ATTACHED = "attached_to_source"
_CONFIRMED = "acquisition_method_recorded"


def actual_method(events: list[tuple[str, str]], source_id: str) -> str | None:
    """Restore a confirmed method for this source, or use unambiguous attachment history."""
    methods: dict[str, set[str | None]] = {_ATTACHED: set(), _CONFIRMED: set()}
    for action, raw in events:
        try:
            details = json.loads(raw)
        except (TypeError, ValueError):
            methods[action].add(None)
            continue
        if not isinstance(details, dict) or details.get("source_id") != source_id:
            continue
        method = details.get("method")
        methods[action].add(method if isinstance(method, str) and method in _METHODS else None)
    found = methods[_CONFIRMED] or methods[_ATTACHED]
    return next(iter(found)) if len(found) == 1 else None


def upgrade() -> None:
    # Plain ADD COLUMN (not batch): recordings has no triggers, and the M2 trigger tables
    # are untouched.
    op.add_column('recordings', sa.Column('acquisition_method', sa.String(length=32),
                                          nullable=True))
    bind = op.get_bind()
    linked = bind.execute(sa.text(
        "SELECT id, source_id FROM recordings WHERE source_id IS NOT NULL")).fetchall()
    for rec_id, source_id in linked:
        events = [(action, details) for action, details in bind.execute(sa.text(
            "SELECT action, details_json FROM provenance_events WHERE entity = 'recording' "
            "AND entity_id = :i AND action IN (:attached, :confirmed)"),
            {"i": rec_id, "attached": _ATTACHED, "confirmed": _CONFIRMED})]
        method = actual_method(events, source_id)
        if method is not None:
            bind.execute(sa.text("UPDATE recordings SET acquisition_method = :m WHERE id = :i"),
                         {"m": method, "i": rec_id})


def downgrade() -> None:
    with op.batch_alter_table('recordings') as batch:
        batch.drop_column('acquisition_method')
