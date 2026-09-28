"""qualification quizzes (Phase 9)

Revision ID: e2f6a9c4d8b1
Revises: b8e40c92f7d1
Create Date: 2026-09-26 18:20:00 UTC

Phase 9: per-project qualification quizzes. One quiz per project
(unique project_id); attempts record every graded try plus
admin-granted exemptions. Questions live as a canonical JSON payload
(quizzes.py owns parsing/grading); no correct-answer data is exposed
through any read surface.
"""
from alembic import op
import sqlalchemy as sa

revision = "e2f6a9c4d8b1"
down_revision = "b8e40c92f7d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create qualification_quizzes + quiz_attempts (chain 16)."""
    op.create_table(
        "qualification_quizzes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("questions_json", sa.Text(), nullable=False),
        sa.Column("pass_score", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"]
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_quiz_status",
        ),
        sa.CheckConstraint(
            "pass_score BETWEEN 1 AND 100", name="ck_quiz_score"
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20",
            name="ck_quiz_max_attempts",
        ),
    )
    op.create_index(
        "ix_qualification_quizzes_status",
        "qualification_quizzes",
        ["status"],
    )
    op.create_index(
        "ix_qualification_quizzes_project_status",
        "qualification_quizzes",
        ["project_id", "status"],
    )
    op.create_table(
        "quiz_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("quiz_id", sa.Integer(), nullable=False),
        sa.Column("annotator_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("correct_count", sa.Integer(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("score_percent", sa.Integer(), nullable=True),
        sa.Column("answers_json", sa.Text(), nullable=True),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["quiz_id"], ["qualification_quizzes.id"]
        ),
        sa.ForeignKeyConstraint(["annotator_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'exempt')",
            name="ck_quiz_attempt_status",
        ),
        sa.CheckConstraint(
            "score_percent IS NULL OR score_percent BETWEEN 0 AND 100",
            name="ck_quiz_attempt_score",
        ),
    )
    op.create_index(
        "ix_quiz_attempts_quiz_id", "quiz_attempts", ["quiz_id"]
    )
    op.create_index(
        "ix_quiz_attempts_annotator_id",
        "quiz_attempts",
        ["annotator_id"],
    )
    op.create_index(
        "ix_quiz_attempts_created_at", "quiz_attempts", ["created_at"]
    )
    op.create_index(
        "ix_quiz_attempts_annotator_quiz",
        "quiz_attempts",
        ["annotator_id", "quiz_id", "status"],
    )


def downgrade() -> None:
    """Drop quiz_attempts + qualification_quizzes."""
    op.drop_index(
        "ix_quiz_attempts_annotator_quiz", table_name="quiz_attempts"
    )
    op.drop_index("ix_quiz_attempts_created_at", table_name="quiz_attempts")
    op.drop_index(
        "ix_quiz_attempts_annotator_id", table_name="quiz_attempts"
    )
    op.drop_index("ix_quiz_attempts_quiz_id", table_name="quiz_attempts")
    op.drop_table("quiz_attempts")
    op.drop_index(
        "ix_qualification_quizzes_project_status",
        table_name="qualification_quizzes",
    )
    op.drop_index(
        "ix_qualification_quizzes_status",
        table_name="qualification_quizzes",
    )
    op.drop_table("qualification_quizzes")
