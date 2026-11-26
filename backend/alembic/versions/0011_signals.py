"""M4: audio and visual signal runs and keyframes (docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md
§3, §5, §6).

Adds the AUDIO_SIGNALS and VISUAL_SIGNALS job types, ``signal_runs`` (one per recording,
kind and configuration; compressed measured series plus measured/inferred events) and
``keyframes``. A complete run and its keyframes are immutable (triggers).
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_OLD = "job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN','TRANSCRIBE')"
_NEW = ("job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN','TRANSCRIBE',"
        "'AUDIO_SIGNALS','VISUAL_SIGNALS')")

TRIGGERS = {
    "trg_signal_runs_complete_immutable": """
        CREATE TRIGGER trg_signal_runs_complete_immutable
        BEFORE UPDATE ON signal_runs
        WHEN OLD.status = 'COMPLETE'
        BEGIN SELECT RAISE(ABORT, 'a complete signal run is immutable'); END""",
    "trg_signal_runs_complete_no_delete": """
        CREATE TRIGGER trg_signal_runs_complete_no_delete
        BEFORE DELETE ON signal_runs
        WHEN OLD.status = 'COMPLETE'
        BEGIN SELECT RAISE(ABORT, 'a complete signal run is immutable'); END""",
    "trg_keyframes_immutable": """
        CREATE TRIGGER trg_keyframes_immutable
        BEFORE UPDATE ON keyframes
        BEGIN SELECT RAISE(ABORT, 'keyframes are immutable'); END""",
    "trg_keyframes_no_delete": """
        CREATE TRIGGER trg_keyframes_no_delete
        BEFORE DELETE ON keyframes
        BEGIN SELECT RAISE(ABORT, 'keyframes are immutable'); END""",
}


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _NEW)
    op.create_table(
        "signal_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recording_id", sa.String(36),
                  sa.ForeignKey("recordings.id", name=op.f("fk_signal_runs_recording_id_recordings"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("recording_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("series_gz", sa.LargeBinary()),
        sa.Column("events_json", sa.Text()),
        sa.Column("job_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("recording_id", "kind", "config_fingerprint",
                            name=op.f("uq_signal_runs_recording_id_kind_config_fingerprint")),
        sa.CheckConstraint("kind IN ('audio','visual')", name=op.f("ck_signal_runs_kind_valid")),
        sa.CheckConstraint("status IN ('PENDING','COMPLETE')",
                           name=op.f("ck_signal_runs_status_valid")),
    )
    op.create_index("ix_signal_runs_recording", "signal_runs", ["recording_id"])
    op.create_table(
        "keyframes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("signal_run_id", sa.String(36),
                  sa.ForeignKey("signal_runs.id", name=op.f("fk_keyframes_signal_run_id_signal_runs"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("pts_ms", sa.Integer(), nullable=False),
        sa.Column("scene_idx", sa.Integer(), nullable=False),
        sa.Column("stored_filename", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("signal_run_id", "idx", name=op.f("uq_keyframes_signal_run_id_idx")),
        sa.UniqueConstraint("stored_filename", name=op.f("uq_keyframes_stored_filename")),
    )
    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM signal_runs")).scalar():
        raise RuntimeError("refusing to downgrade 0011: signal runs exist (complete runs are "
                           "immutable analysis history); export or archive them first")
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("keyframes")
    op.drop_index("ix_signal_runs_recording", table_name="signal_runs")
    op.drop_table("signal_runs")
    op.execute("UPDATE system_events SET related_job_id = NULL WHERE related_job_id IN "
               "(SELECT id FROM jobs WHERE job_type IN ('AUDIO_SIGNALS','VISUAL_SIGNALS'))")
    op.execute("DELETE FROM jobs WHERE job_type IN ('AUDIO_SIGNALS','VISUAL_SIGNALS')")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _OLD)
