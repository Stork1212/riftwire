"""migration journal records the destination drive's OS volume UUID (M1 review)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24 14:40:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('storage_migrations') as batch:
        batch.add_column(sa.Column('dest_hw_volume_uuid', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('storage_migrations') as batch:
        batch.drop_column('dest_hw_volume_uuid')
