"""Migration 21: workforce verification, dual-workspace, and HR screening subsystem.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-11-22 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Update users table with verification, archive, lineage and activation columns, plus HR role
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("verified_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("verified_by_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("migrated_to_user_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("migrated_from_user_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("activation_token_hash", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("activation_token_expires", sa.DateTime(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_users_verified_by_id_users", "users", ["verified_by_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_users_migrated_to_user_id_users", "users", ["migrated_to_user_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_users_migrated_from_user_id_users", "users", ["migrated_from_user_id"], ["id"]
        )
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            "role IN ('admin', 'project_manager', 'qa', 'auditor', 'hr', 'annotator')",
        )
        batch_op.create_index(
            "ix_users_verified_active", ["is_verified", "is_active"], unique=False
        )
        batch_op.create_index(
            "ix_users_activation_token_hash", ["activation_token_hash"], unique=False
        )

    # 2. Update projects table with allows_provisional flag
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("allows_provisional", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.create_index(
            "ix_projects_allows_provisional", ["allows_provisional"], unique=False
        )

    # 3. Update applicants table with verification_status and constraint
    with op.batch_alter_table("applicants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("verification_status", sa.String(length=30), nullable=False, server_default="provisional")
        )
        batch_op.create_check_constraint(
            "ck_applicants_verification_status",
            "verification_status IN ('provisional', 'pending_hr', 'hr_cleared', 'verified', 'rejected')",
        )
        batch_op.create_index(
            "ix_applicants_verification_status", ["verification_status"], unique=False
        )

    # 4. Create hr_screenings table
    op.create_table(
        "hr_screenings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=False),
        sa.Column("hr_user_id", sa.Integer(), nullable=False),
        sa.Column("interview_date", sa.DateTime(), nullable=False),
        sa.Column("communication_score", sa.Integer(), nullable=False),
        sa.Column("professionalism_score", sa.Integer(), nullable=False),
        sa.Column("availability_confirmed_hours", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "communication_score BETWEEN 1 AND 5",
            name="ck_hr_screening_comm_score",
        ),
        sa.CheckConstraint(
            "professionalism_score BETWEEN 1 AND 5",
            name="ck_hr_screening_prof_score",
        ),
        sa.CheckConstraint(
            "recommendation IN ('recommended', 'needs_practice', 'disqualified')",
            name="ck_hr_screening_rec",
        ),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["hr_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hr_screenings_applicant_id", "hr_screenings", ["applicant_id"], unique=True)
    op.create_index("ix_hr_screenings_hr_user_id", "hr_screenings", ["hr_user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("hr_screenings")
    with op.batch_alter_table("applicants", schema=None) as batch_op:
        batch_op.drop_index("ix_applicants_verification_status")
        batch_op.drop_constraint("ck_applicants_verification_status", type_="check")
        batch_op.drop_column("verification_status")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index("ix_projects_allows_provisional")
        batch_op.drop_column("allows_provisional")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_activation_token_hash")
        batch_op.drop_index("ix_users_verified_active")
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            "role IN ('admin', 'project_manager', 'qa', 'auditor', 'annotator')",
        )
        batch_op.drop_constraint("fk_users_migrated_from_user_id_users", type_="foreignkey")
        batch_op.drop_constraint("fk_users_migrated_to_user_id_users", type_="foreignkey")
        batch_op.drop_constraint("fk_users_verified_by_id_users", type_="foreignkey")
        batch_op.drop_column("activation_token_expires")
        batch_op.drop_column("activation_token_hash")
        batch_op.drop_column("migrated_from_user_id")
        batch_op.drop_column("migrated_to_user_id")
        batch_op.drop_column("verified_by_id")
        batch_op.drop_column("verified_at")
        batch_op.drop_column("is_archived")
        batch_op.drop_column("is_verified")
