"""initial schema

Revision ID: 0001
Revises: (none)
Create Date: 2026-11-23 17:54:37.648157
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table('recordings',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('stored_filename', sa.String(length=64), nullable=False),
    sa.Column('file_path', sa.String(length=512), nullable=False),
    sa.Column('staging_filename', sa.String(length=128), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('file_size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('container_format', sa.String(length=64), nullable=True),
    sa.Column('duration_ms', sa.BigInteger(), nullable=True),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('fps_num', sa.Integer(), nullable=True),
    sa.Column('fps_den', sa.Integer(), nullable=True),
    sa.Column('is_variable_frame_rate', sa.Boolean(), nullable=True),
    sa.Column('video_codec', sa.String(length=64), nullable=True),
    sa.Column('audio_codec', sa.String(length=64), nullable=True),
    sa.Column('has_audio', sa.Boolean(), nullable=True),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('thumbnail_status', sa.String(length=16), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('STAGED','PROCESSING','READY','FAILED')", name=op.f('ck_recordings_status_valid')),
    sa.CheckConstraint("thumbnail_status IN ('PENDING','READY','FAILED')", name=op.f('ck_recordings_thumbnail_status_valid')),
    sa.CheckConstraint('file_size_bytes >= 0', name=op.f('ck_recordings_size_nonneg')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recordings')),
    sa.UniqueConstraint('sha256', name=op.f('uq_recordings_sha256')),
    sa.UniqueConstraint('stored_filename', name=op.f('uq_recordings_stored_filename'))
    )
    with op.batch_alter_table('recordings', schema=None) as batch_op:
        batch_op.create_index('ix_recordings_imported_at', ['imported_at'], unique=False)

    op.create_table('workers',
    sa.Column('id', sa.String(length=128), nullable=False),
    sa.Column('hostname', sa.String(length=255), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=False),
    sa.Column('concurrency', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('last_heartbeat_at', sa.DateTime(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('current_job_id', sa.String(length=36), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workers'))
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recording_id', sa.String(length=36), nullable=False),
    sa.Column('job_type', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('progress_percent', sa.Integer(), nullable=False),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(), nullable=True),
    sa.Column('claimed_by', sa.String(length=128), nullable=True),
    sa.Column('lease_token', sa.String(length=64), nullable=True),
    sa.Column('process_pid', sa.Integer(), nullable=True),
    sa.Column('process_started_at', sa.String(length=64), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.CheckConstraint("job_type IN ('INGEST','METADATA','THUMBNAIL')", name=op.f('ck_jobs_job_type_valid')),
    sa.CheckConstraint("status IN ('PENDING','RUNNING','COMPLETED','FAILED','RETRY_WAIT','CANCELLED')", name=op.f('ck_jobs_status_valid')),
    sa.CheckConstraint('attempt_count >= 0', name=op.f('ck_jobs_attempts_nonneg')),
    sa.CheckConstraint('max_attempts >= 1', name=op.f('ck_jobs_max_attempts_pos')),
    sa.CheckConstraint('progress_percent BETWEEN 0 AND 100', name=op.f('ck_jobs_progress_range')),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], name=op.f('fk_jobs_recording_id_recordings'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('recording_id', 'job_type', name='uq_jobs_recording_type')
    )
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.create_index('ix_jobs_claimable', ['status', 'next_attempt_at'], unique=False)
        batch_op.create_index('ix_jobs_created_at', ['created_at'], unique=False)

    op.create_table('system_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('timestamp', sa.DateTime(), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('component', sa.String(length=64), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('related_job_id', sa.String(length=36), nullable=True),
    sa.Column('related_recording_id', sa.String(length=36), nullable=True),
    sa.CheckConstraint("severity IN ('DEBUG','INFO','WARNING','ERROR','CRITICAL')", name=op.f('ck_system_events_severity_valid')),
    sa.ForeignKeyConstraint(['related_job_id'], ['jobs.id'], name=op.f('fk_system_events_related_job_id_jobs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['related_recording_id'], ['recordings.id'], name=op.f('fk_system_events_related_recording_id_recordings'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_system_events'))
    )
    with op.batch_alter_table('system_events', schema=None) as batch_op:
        batch_op.create_index('ix_system_events_timestamp', ['timestamp'], unique=False)




def downgrade() -> None:

    with op.batch_alter_table('system_events', schema=None) as batch_op:
        batch_op.drop_index('ix_system_events_timestamp')

    op.drop_table('system_events')
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_jobs_created_at')
        batch_op.drop_index('ix_jobs_claimable')

    op.drop_table('jobs')
    op.drop_table('workers')
    with op.batch_alter_table('recordings', schema=None) as batch_op:
        batch_op.drop_index('ix_recordings_imported_at')

    op.drop_table('recordings')

