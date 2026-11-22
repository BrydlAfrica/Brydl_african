"""Migration 23: Quality Standards, Multi-Dimensional Rubrics & Defect Taxonomy.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-11-22 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add quality_threshold to projects
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "quality_threshold",
                sa.Float(),
                nullable=False,
                server_default=sa.text("90.0"),
            )
        )
        batch_op.create_check_constraint(
            "ck_projects_quality_threshold",
            "quality_threshold >= 50.0 AND quality_threshold <= 100.0",
        )

    # 2. Add qa_rubric_data, error_tags, appeal columns to submissions
    with op.batch_alter_table("submissions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("qa_rubric_data", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("error_tags", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "appeal_status",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'none'"),
            )
        )
        batch_op.add_column(
            sa.Column("appeal_notes", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("appeal_reviewed_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("appeal_reviewed_by_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_submissions_appeal_reviewer",
            "users",
            ["appeal_reviewed_by_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_submissions_appeal_status",
            "appeal_status IN ('none', 'pending', 'granted', 'denied')",
        )
        batch_op.create_index(
            "ix_submissions_appeal_status",
            ["appeal_status"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("submissions", schema=None) as batch_op:
        batch_op.drop_index("ix_submissions_appeal_status")
        batch_op.drop_constraint("ck_submissions_appeal_status", type_="check")
        batch_op.drop_constraint("fk_submissions_appeal_reviewer", type_="foreignkey")
        batch_op.drop_column("appeal_reviewed_by_id")
        batch_op.drop_column("appeal_reviewed_at")
        batch_op.drop_column("appeal_notes")
        batch_op.drop_column("appeal_status")
        batch_op.drop_column("error_tags")
        batch_op.drop_column("qa_rubric_data")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("ck_projects_quality_threshold", type_="check")
        batch_op.drop_column("quality_threshold")
