"""api_tokens + background_jobs (Phase 10)

Revision ID: c7d21b8a45e9
Revises: e2f6a9c4d8b1
Create Date: 2026-09-26 19:05:00 UTC

Phase 10: the machine API tier (project-scoped Bearer keys, hash-only
storage) and the DB-backed background job queue (durable rows, an
in-process runner, race-safe claiming). No secrets ever enter
background_jobs payloads.
"""
from alembic import op
import sqlalchemy as sa

revision = "c7d21b8a45e9"
down_revision = "e2f6a9c4d8b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create api_tokens + background_jobs, backfill Payment.created_at
    (chain 17)."""
    op.add_column(
        "payments",
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "UPDATE payments SET created_at = COALESCE(paid_at, period_end)"
    )
    with op.batch_alter_table("payments") as batch_op:
        batch_op.alter_column("created_at", nullable=False)
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_tokens_project_id", "api_tokens", ["project_id"]
    )
    op.create_index(
        "ix_api_tokens_key_hash",
        "api_tokens",
        ["key_hash"],
        unique=True,
    )
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20",
            name="ck_jobs_max_attempts",
        ),
    )
    op.create_index(
        "ix_background_jobs_status", "background_jobs", ["status"]
    )
    op.create_index(
        "ix_background_jobs_status_run_after",
        "background_jobs",
        ["status", "run_after"],
    )


def downgrade() -> None:
    """Drop background_jobs + api_tokens, drop Payment.created_at."""
    op.drop_column("payments", "created_at")
    op.drop_index(
        "ix_background_jobs_status_run_after",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_status", table_name="background_jobs"
    )
    op.drop_table("background_jobs")
    op.drop_index("ix_api_tokens_key_hash", table_name="api_tokens")
    op.drop_index("ix_api_tokens_project_id", table_name="api_tokens")
    op.drop_table("api_tokens")
