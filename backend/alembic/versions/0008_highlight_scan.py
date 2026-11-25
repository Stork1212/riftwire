"""Persist completed highlight scans in the recording job queue."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_OLD = "job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE')"
_NEW = "job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN')"


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _NEW)
        batch.add_column(sa.Column("result_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE system_events SET related_job_id = NULL WHERE related_job_id IN "
               "(SELECT id FROM jobs WHERE job_type = 'HIGHLIGHT_SCAN')")
    op.execute("DELETE FROM jobs WHERE job_type = 'HIGHLIGHT_SCAN'")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("result_json")
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _OLD)
