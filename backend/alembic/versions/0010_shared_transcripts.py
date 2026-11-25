"""M4: shared, versioned transcripts (docs/M4_TRANSCRIPTION_SIGNALS_PLAN.md §3).

Adds the TRANSCRIBE job type, transcription configurations, transcripts, their chunks,
raw words and segments, and versioned timing corrections. (Source-time mapping uses the
existing ``recordings.source_offset_ms`` from migration 0004.)
Raw words and segments, and DONE chunks, are made immutable by triggers.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_OLD = "job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN')"
_NEW = "job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN','TRANSCRIBE')"

TRIGGERS = {
    "trg_transcript_words_immutable": """
        CREATE TRIGGER trg_transcript_words_immutable
        BEFORE UPDATE ON transcript_words
        BEGIN SELECT RAISE(ABORT, 'raw transcript words are immutable'); END""",
    "trg_transcript_words_no_delete": """
        CREATE TRIGGER trg_transcript_words_no_delete
        BEFORE DELETE ON transcript_words
        BEGIN SELECT RAISE(ABORT, 'raw transcript words are immutable'); END""",
    "trg_transcript_segments_immutable": """
        CREATE TRIGGER trg_transcript_segments_immutable
        BEFORE UPDATE ON transcript_segments
        BEGIN SELECT RAISE(ABORT, 'raw transcript segments are immutable'); END""",
    "trg_transcript_segments_no_delete": """
        CREATE TRIGGER trg_transcript_segments_no_delete
        BEFORE DELETE ON transcript_segments
        BEGIN SELECT RAISE(ABORT, 'raw transcript segments are immutable'); END""",
    "trg_transcript_chunks_done_immutable": """
        CREATE TRIGGER trg_transcript_chunks_done_immutable
        BEFORE UPDATE ON transcript_chunks
        WHEN OLD.status = 'DONE'
        BEGIN SELECT RAISE(ABORT, 'a transcribed chunk is immutable'); END""",
    "trg_transcript_chunks_done_no_delete": """
        CREATE TRIGGER trg_transcript_chunks_done_no_delete
        BEFORE DELETE ON transcript_chunks
        WHEN OLD.status = 'DONE'
        BEGIN SELECT RAISE(ABORT, 'a transcribed chunk is immutable'); END""",
    "trg_transcripts_complete_immutable": """
        CREATE TRIGGER trg_transcripts_complete_immutable
        BEFORE UPDATE ON transcripts
        WHEN OLD.status = 'COMPLETE'
        BEGIN SELECT RAISE(ABORT, 'a complete transcript is immutable'); END""",
    "trg_transcript_corrections_immutable": """
        CREATE TRIGGER trg_transcript_corrections_immutable
        BEFORE UPDATE OF transcript_id, version, method, params_json ON transcript_corrections
        BEGIN SELECT RAISE(ABORT, 'a timing correction is immutable; add a new version'); END""",
}


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _NEW)

    op.create_table(
        "transcription_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_transcription_configs_fingerprint")),
    )
    op.create_table(
        "transcripts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recording_id", sa.String(36),
                  sa.ForeignKey("recordings.id", name=op.f("fk_transcripts_recording_id_recordings"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("config_id", sa.String(36),
                  sa.ForeignKey("transcription_configs.id",
                                name=op.f("fk_transcripts_config_id_transcription_configs"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("recording_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("coverage_ms", sa.Integer(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(64)),
        sa.Column("job_id", sa.String(36)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("recording_id", "config_id",
                            name=op.f("uq_transcripts_recording_id_config_id")),
        sa.CheckConstraint("status IN ('PENDING','COMPLETE','FAILED')",
                           name=op.f("ck_transcripts_status_valid")),
        sa.CheckConstraint("coverage_ms >= 0", name=op.f("ck_transcripts_coverage_nonneg")),
    )
    op.create_index("ix_transcripts_recording", "transcripts", ["recording_id"])
    op.create_table(
        "transcript_chunks",
        sa.Column("transcript_id", sa.String(36),
                  sa.ForeignKey("transcripts.id",
                                name=op.f("fk_transcript_chunks_transcript_id_transcripts"),
                                ondelete="RESTRICT"), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("core_start_ms", sa.Integer(), nullable=False),
        sa.Column("core_end_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("audio_sha256", sa.String(64)),
        sa.Column("raw_json_gz", sa.LargeBinary()),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint("status IN ('PENDING','DONE')",
                           name=op.f("ck_transcript_chunks_status_valid")),
        sa.CheckConstraint("end_ms > start_ms", name=op.f("ck_transcript_chunks_span_valid")),
    )
    op.create_table(
        "transcript_words",
        sa.Column("transcript_id", sa.String(36),
                  sa.ForeignKey("transcripts.id",
                                name=op.f("fk_transcript_words_transcript_id_transcripts"),
                                ondelete="RESTRICT"), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("prob", sa.Float()),
        sa.Column("chunk_idx", sa.Integer(), nullable=False),
        sa.Column("segment_idx", sa.Integer()),
        sa.CheckConstraint("end_ms >= start_ms", name=op.f("ck_transcript_words_span_valid")),
    )
    op.create_table(
        "transcript_segments",
        sa.Column("transcript_id", sa.String(36),
                  sa.ForeignKey("transcripts.id",
                                name=op.f("fk_transcript_segments_transcript_id_transcripts"),
                                ondelete="RESTRICT"), primary_key=True),
        sa.Column("idx", sa.Integer(), primary_key=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("first_word_idx", sa.Integer(), nullable=False),
        sa.Column("last_word_idx", sa.Integer(), nullable=False),
        sa.CheckConstraint("end_ms >= start_ms",
                           name=op.f("ck_transcript_segments_span_valid")),
    )
    op.create_table(
        "transcript_corrections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("transcript_id", sa.String(36),
                  sa.ForeignKey("transcripts.id",
                                name=op.f("fk_transcript_corrections_transcript_id_transcripts"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("validated", sa.Boolean(), nullable=False),
        sa.Column("validation_ref", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("transcript_id", "version",
                            name=op.f("uq_transcript_corrections_transcript_id_version")),
        sa.CheckConstraint("method IN ('constant_offset')",
                           name=op.f("ck_transcript_corrections_method_valid")),
    )
    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM transcripts")).scalar():
        raise RuntimeError("refusing to downgrade 0010: transcripts exist (raw transcripts "
                           "are immutable history); export or archive them first")
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table in ("transcript_corrections", "transcript_segments", "transcript_words",
                  "transcript_chunks"):
        op.drop_table(table)
    op.drop_index("ix_transcripts_recording", table_name="transcripts")
    op.drop_table("transcripts")
    op.drop_table("transcription_configs")
    op.execute("UPDATE system_events SET related_job_id = NULL WHERE related_job_id IN "
               "(SELECT id FROM jobs WHERE job_type = 'TRANSCRIBE')")
    op.execute("DELETE FROM jobs WHERE job_type = 'TRANSCRIBE'")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _OLD)
