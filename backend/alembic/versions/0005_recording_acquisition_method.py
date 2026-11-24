"""recordings.acquisition_method: how linked media was actually obtained (M2 review)

Rights checks for analysis, editing and publishing use the recording's actual acquisition
method, not only the source's declared one.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24 17:10:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN (not batch): recordings has no triggers, and the M2 trigger tables
    # are untouched.
    op.add_column('recordings', sa.Column('acquisition_method', sa.String(length=32),
                                          nullable=True))
    # Media linked before this column existed: the source's declared method is the best
    # available record (it is what the link was checked against).
    op.execute("UPDATE recordings SET acquisition_method = (SELECT acquisition_method FROM "
               "sources WHERE sources.id = recordings.source_id) WHERE source_id IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table('recordings') as batch:
        batch.drop_column('acquisition_method')
