"""storage locations, reservations, migrations (M1)

Adds the storage layout (authoritative location per kind), persistent per-volume disk
reservations shared by the API and worker, and the resumable migration journal.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24 00:39:52.376125
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('disk_reservations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('volume_uuid', sa.String(length=36), nullable=False),
    sa.Column('location_kind', sa.String(length=16), nullable=False),
    sa.Column('holder', sa.String(length=128), nullable=False),
    sa.Column('bytes', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('bytes >= 0', name=op.f('ck_disk_reservations_bytes_nonneg')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_disk_reservations'))
    )
    with op.batch_alter_table('disk_reservations', schema=None) as batch_op:
        batch_op.create_index('ix_disk_reservations_volume', ['volume_uuid', 'expires_at'], unique=False)

    op.create_table('storage_locations',
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('path', sa.String(length=1024), nullable=False),
    sa.Column('location_uuid', sa.String(length=36), nullable=False),
    sa.Column('volume_uuid', sa.String(length=36), nullable=False),
    sa.Column('hw_volume_uuid', sa.String(length=64), nullable=True),
    sa.Column('fs_type', sa.String(length=32), nullable=True),
    sa.Column('floor_bytes', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("kind IN ('recordings','temp','models','exports','assets')", name=op.f('ck_storage_locations_kind_valid')),
    sa.CheckConstraint('floor_bytes >= 0', name=op.f('ck_storage_locations_floor_nonneg')),
    sa.PrimaryKeyConstraint('kind', name=op.f('pk_storage_locations'))
    )
    op.create_table('storage_migrations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('source_path', sa.String(length=1024), nullable=False),
    sa.Column('dest_path', sa.String(length=1024), nullable=False),
    sa.Column('dest_location_uuid', sa.String(length=36), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('total_files', sa.Integer(), nullable=False),
    sa.Column('total_bytes', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('COPYING','SWITCHED','ABORTED')", name=op.f('ck_storage_migrations_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_storage_migrations'))
    )
    op.create_table('storage_migration_items',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('migration_id', sa.String(length=36), nullable=False),
    sa.Column('rel_path', sa.String(length=1024), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.CheckConstraint("state IN ('PENDING','VERIFIED')", name=op.f('ck_storage_migration_items_state_valid')),
    sa.ForeignKeyConstraint(['migration_id'], ['storage_migrations.id'], name=op.f('fk_storage_migration_items_migration_id_storage_migrations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_storage_migration_items')),
    sa.UniqueConstraint('migration_id', 'rel_path', name='uq_migration_item_path')
    )


def downgrade() -> None:
    op.drop_table('storage_migration_items')
    op.drop_table('storage_migrations')
    op.drop_table('storage_locations')
    with op.batch_alter_table('disk_reservations', schema=None) as batch_op:
        batch_op.drop_index('ix_disk_reservations_volume')

    op.drop_table('disk_reservations')
