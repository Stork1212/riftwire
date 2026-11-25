"""ACQUIRE jobs: fetching a saved source URL is a durable queued job

An ACQUIRE job belongs to a source (``jobs.source_id``); its recording does not exist when
it is queued, so ``jobs.recording_id`` becomes nullable and is set when the recording is
created. One ACQUIRE job per source (``uq_jobs_source_type``), mirroring one job per
(recording, type). Every other job type still requires a recording (``ck_jobs_subject``).

Rebuilds ``jobs`` (SQLite batch). Existing rows and their ``system_events`` references are
preserved: migrations run with foreign keys off and a ``foreign_key_check`` before commit
(alembic/env.py, M2 audit finding 1).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24 21:10:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

_TYPES_OLD = "job_type IN ('INGEST','METADATA','THUMBNAIL')"
_TYPES_NEW = "job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE')"
_SUBJECT = ("(job_type = 'ACQUIRE' AND source_id IS NOT NULL) OR "
            "(job_type != 'ACQUIRE' AND source_id IS NULL AND recording_id IS NOT NULL)")


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_id', sa.String(length=36), nullable=True))
        batch_op.alter_column('recording_id', existing_type=sa.String(length=36),
                              nullable=True)
        batch_op.create_foreign_key(batch_op.f('fk_jobs_source_id_sources'), 'sources',
                                    ['source_id'], ['id'])
        batch_op.create_unique_constraint('uq_jobs_source_type', ['source_id', 'job_type'])
        batch_op.drop_constraint(batch_op.f('ck_jobs_job_type_valid'), type_='check')
        batch_op.create_check_constraint(batch_op.f('ck_jobs_job_type_valid'), _TYPES_NEW)
        batch_op.create_check_constraint(batch_op.f('ck_jobs_subject'), _SUBJECT)


def downgrade() -> None:
    # ACQUIRE jobs cannot exist before this revision. Their recordings, if any, stay; their
    # events keep their text but lose the job reference (what ON DELETE SET NULL would do;
    # foreign keys are off while migrating).
    op.execute("UPDATE system_events SET related_job_id = NULL WHERE related_job_id IN "
               "(SELECT id FROM jobs WHERE job_type = 'ACQUIRE')")
    op.execute("DELETE FROM jobs WHERE job_type = 'ACQUIRE'")
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('ck_jobs_subject'), type_='check')
        batch_op.drop_constraint(batch_op.f('ck_jobs_job_type_valid'), type_='check')
        batch_op.create_check_constraint(batch_op.f('ck_jobs_job_type_valid'), _TYPES_OLD)
        batch_op.drop_constraint('uq_jobs_source_type', type_='unique')
        batch_op.drop_constraint(batch_op.f('fk_jobs_source_id_sources'), type_='foreignkey')
        batch_op.alter_column('recording_id', existing_type=sa.String(length=36),
                              nullable=False)
        batch_op.drop_column('source_id')
