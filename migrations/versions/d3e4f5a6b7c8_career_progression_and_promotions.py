"""Migration 22: Career Progression & Specialist Pathways Framework.

Revision ID: d3e4f5a6b7c8
Revises: f2a3b4c5d6e7
Create Date: 2026-11-22 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Update users table with seniority_level, specialist_badge, is_pod_lead, assigned_pod
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("seniority_level", sa.String(length=30), nullable=False, server_default="verified")
        )
        batch_op.add_column(
            sa.Column("specialist_badge", sa.String(length=100), nullable=True)
        )
        batch_op.add_column(
            sa.Column("is_pod_lead", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("assigned_pod", sa.String(length=100), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_users_seniority_level",
            "seniority_level IN ('provisional', 'verified', 'specialist', 'qa_reviewer', 'consensus_arbitrator', 'pod_lead')",
        )
        batch_op.create_index(
            "ix_users_seniority_level", ["seniority_level"], unique=False
        )
        batch_op.create_index(
            "ix_users_pod", ["assigned_pod"], unique=False
        )

    # 2. Create career_promotion_requests table
    op.create_table(
        "career_promotion_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("target_role", sa.String(length=50), nullable=False),
        sa.Column("specialization_area", sa.String(length=100), nullable=False),
        sa.Column("statement_of_interest", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_career_promotion_status",
        ),
        sa.CheckConstraint(
            "target_role IN ('specialist', 'qa_reviewer', 'consensus_arbitrator', 'pod_lead')",
            name="ck_career_promotion_target_role",
        ),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_career_promotion_requests_user_id", "career_promotion_requests", ["user_id"], unique=False)
    op.create_index("ix_career_promotion_requests_status", "career_promotion_requests", ["status"], unique=False)
    op.create_index("ix_career_promotions_user_status", "career_promotion_requests", ["user_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_career_promotions_user_status", table_name="career_promotion_requests")
    op.drop_index("ix_career_promotion_requests_status", table_name="career_promotion_requests")
    op.drop_index("ix_career_promotion_requests_user_id", table_name="career_promotion_requests")
    op.drop_table("career_promotion_requests")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_pod")
        batch_op.drop_index("ix_users_seniority_level")
        batch_op.drop_constraint("ck_users_seniority_level", type_="check")
        batch_op.drop_column("assigned_pod")
        batch_op.drop_column("is_pod_lead")
        batch_op.drop_column("specialist_badge")
        batch_op.drop_column("seniority_level")
