"""M5/M6: AI highlight requests, AI runs and candidates (docs/M5_M6_AI_HIGHLIGHTS_PLAN.md).

Adds the AI_HIGHLIGHTS job type and three tables. Finished AI runs are immutable and every
run records cost 0 (check constraint): paid providers do not exist in this release.
Candidates are immutable except their review status.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_OLD = ("job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN','TRANSCRIBE',"
        "'AUDIO_SIGNALS','VISUAL_SIGNALS')")
_NEW = ("job_type IN ('INGEST','METADATA','THUMBNAIL','ACQUIRE','HIGHLIGHT_SCAN','TRANSCRIBE',"
        "'AUDIO_SIGNALS','VISUAL_SIGNALS','AI_HIGHLIGHTS')")

TRIGGERS = {
    "trg_ai_runs_immutable": """
        CREATE TRIGGER trg_ai_runs_immutable BEFORE UPDATE ON ai_runs
        BEGIN SELECT RAISE(ABORT, 'AI runs are immutable'); END""",
    "trg_ai_runs_no_delete": """
        CREATE TRIGGER trg_ai_runs_no_delete BEFORE DELETE ON ai_runs
        BEGIN SELECT RAISE(ABORT, 'AI runs are immutable'); END""",
    "trg_highlight_candidates_immutable": """
        CREATE TRIGGER trg_highlight_candidates_immutable
        BEFORE UPDATE OF id, created_at, request_id, ai_run_id, status, rank, reject_reason, window_id,
            start_ms, end_ms, start_id, end_id, category, title, hook, what_happens,
            why_entertaining, scores_json, editorial_score, confidence, evidence_json,
            uncertainty, transcript_id, evidence_fingerprint ON highlight_candidates
        BEGIN SELECT RAISE(ABORT, 'highlight candidates are immutable (except review status)');
        END""",
    "trg_highlight_candidates_no_delete": """
        CREATE TRIGGER trg_highlight_candidates_no_delete BEFORE DELETE ON highlight_candidates
        BEGIN SELECT RAISE(ABORT, 'highlight candidates are immutable'); END""",
}


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _NEW)
    op.create_table(
        "highlight_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recording_id", sa.String(36),
                  sa.ForeignKey("recordings.id",
                                name=op.f("fk_highlight_requests_recording_id_recordings"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("game", sa.String(64)),
        sa.Column("channel", sa.String(64)),
        sa.Column("prompt_id", sa.String(64), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("plan_json", sa.Text()),
        sa.Column("summary_json", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("job_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint("status IN ('PENDING','RUNNING','COMPLETE','FAILED','SUPERSEDED')",
                           name=op.f("ck_highlight_requests_status_valid")),
        sa.CheckConstraint("target_count BETWEEN 1 AND 20",
                           name=op.f("ck_highlight_requests_target_count_range")),
    )
    op.create_index("ix_highlight_requests_recording", "highlight_requests", ["recording_id"])
    op.create_table(
        "ai_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36),
                  sa.ForeignKey("highlight_requests.id",
                                name=op.f("fk_ai_runs_request_id_highlight_requests"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("batch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("model_digest", sa.String(128)),
        sa.Column("identity_json", sa.Text(), nullable=False),
        sa.Column("prompt_id", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_sha256", sa.String(64), nullable=False),
        sa.Column("scoring_json", sa.Text(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("record_json", sa.Text(), nullable=False),
        sa.Column("raw_last", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("wall_s", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('accepted','failed_validation','provider_error')",
                           name=op.f("ck_ai_runs_status_valid")),
        sa.CheckConstraint("cost_usd = 0", name=op.f("ck_ai_runs_cost_zero")),
    )
    op.create_index("ix_ai_runs_request", "ai_runs", ["request_id"])
    op.create_table(
        "highlight_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36),
                  sa.ForeignKey("highlight_requests.id",
                                name=op.f("fk_highlight_candidates_request_id_highlight_requests"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("ai_run_id", sa.String(36),
                  sa.ForeignKey("ai_runs.id", name=op.f("fk_highlight_candidates_ai_run_id_ai_runs"),
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rank", sa.Integer()),
        sa.Column("reject_reason", sa.Text()),
        sa.Column("window_id", sa.String(32), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("start_id", sa.String(96), nullable=False),
        sa.Column("end_id", sa.String(96), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("hook", sa.String(160), nullable=False),
        sa.Column("what_happens", sa.Text(), nullable=False),
        sa.Column("why_entertaining", sa.Text(), nullable=False),
        sa.Column("scores_json", sa.Text(), nullable=False),
        sa.Column("editorial_score", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("uncertainty", sa.Text(), nullable=False),
        sa.Column("transcript_id", sa.String(36)),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('selected','rejected')",
                           name=op.f("ck_highlight_candidates_status_valid")),
        sa.CheckConstraint("review_status IN ('pending','approved','rejected')",
                           name=op.f("ck_highlight_candidates_review_status_valid")),
        sa.CheckConstraint("end_ms > start_ms", name=op.f("ck_highlight_candidates_span_valid")),
        sa.CheckConstraint("editorial_score BETWEEN 0 AND 100",
                           name=op.f("ck_highlight_candidates_score_range")),
    )
    op.create_index("ix_highlight_candidates_request", "highlight_candidates", ["request_id"])
    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM ai_runs")).scalar():
        raise RuntimeError("refusing to downgrade 0012: AI runs exist (immutable audit "
                           "history); export or archive them first")
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_highlight_candidates_request", table_name="highlight_candidates")
    op.drop_table("highlight_candidates")
    op.drop_index("ix_ai_runs_request", table_name="ai_runs")
    op.drop_table("ai_runs")
    op.drop_index("ix_highlight_requests_recording", table_name="highlight_requests")
    op.drop_table("highlight_requests")
    op.execute("UPDATE system_events SET related_job_id = NULL WHERE related_job_id IN "
               "(SELECT id FROM jobs WHERE job_type = 'AI_HIGHLIGHTS')")
    op.execute("DELETE FROM jobs WHERE job_type = 'AI_HIGHLIGHTS'")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_constraint(op.f("ck_jobs_job_type_valid"), type_="check")
        batch.create_check_constraint(op.f("ck_jobs_job_type_valid"), _OLD)
