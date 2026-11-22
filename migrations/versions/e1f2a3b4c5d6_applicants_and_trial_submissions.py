"""Migration 20: applicants and applicant_trial_submissions tables for 9-stage onboarding.

Revision ID: e1f2a3b4c5d6
Revises: b3c4d5e6f701
Create Date: 2026-11-22 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "b3c4d5e6f701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "applicants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("specialization_track", sa.String(length=50), nullable=False),
        sa.Column("primary_language", sa.String(length=50), nullable=False),
        sa.Column("dialect", sa.String(length=100), nullable=False),
        sa.Column("secondary_languages", sa.Text(), nullable=True),
        sa.Column("competencies", sa.Text(), nullable=True),
        sa.Column("domain_expertise", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=False),
        sa.Column("state_province", sa.String(length=100), nullable=True),
        sa.Column("phone_whatsapp", sa.String(length=50), nullable=True),
        sa.Column("device_type", sa.String(length=50), nullable=True),
        sa.Column("weekly_hours", sa.Integer(), nullable=True),
        sa.Column("guidelines_quiz_score", sa.Integer(), nullable=False),
        sa.Column("guidelines_passed", sa.Boolean(), nullable=False),
        sa.Column("guidelines_passed_at", sa.DateTime(), nullable=True),
        sa.Column("test_started_at", sa.DateTime(), nullable=True),
        sa.Column("test_submitted_at", sa.DateTime(), nullable=True),
        sa.Column("test_score_auto", sa.Integer(), nullable=False),
        sa.Column("test_score_reviewer", sa.Integer(), nullable=True),
        sa.Column("test_score_composite", sa.Integer(), nullable=False),
        sa.Column("test_answers_json", sa.Text(), nullable=True),
        sa.Column("test_passed", sa.Boolean(), nullable=False),
        sa.Column("assigned_pod", sa.String(length=100), nullable=True),
        sa.Column("pod_capacity_fit", sa.Boolean(), nullable=False),
        sa.Column("admin_decision", sa.String(length=20), nullable=False),
        sa.Column("admin_decision_by", sa.Integer(), nullable=True),
        sa.Column("admin_decision_at", sa.DateTime(), nullable=True),
        sa.Column("admin_notes", sa.Text(), nullable=True),
        sa.Column("agreement_signed_at", sa.DateTime(), nullable=True),
        sa.Column("agreement_signature_name", sa.String(length=120), nullable=True),
        sa.Column("agreement_signature_hash", sa.String(length=128), nullable=True),
        sa.Column("trial_tasks_assigned", sa.Integer(), nullable=False),
        sa.Column("trial_tasks_completed", sa.Integer(), nullable=False),
        sa.Column("trial_score_average", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("trial_passed", sa.Boolean(), nullable=False),
        sa.Column("trial_completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "admin_decision IN ('pending', 'approved', 'waitlisted', 'declined')",
            name="ck_applicants_admin_decision",
        ),
        sa.CheckConstraint(
            "specialization_track IN ('red_teaming', 'translation', 'rlhf_preference', 'audio_speech', 'general_labeling')",
            name="ck_applicants_specialization_track",
        ),
        sa.CheckConstraint(
            "stage IN ('apply', 'guidelines', 'test', 'scoring', 'fit_to_need', 'decision', 'agreement', 'trial', 'active', 'waitlisted', 'declined')",
            name="ck_applicants_stage",
        ),
        sa.ForeignKeyConstraint(["admin_decision_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_applicants_admin_decision", "applicants", ["admin_decision"], unique=False)
    op.create_index("ix_applicants_specialization_track", "applicants", ["specialization_track"], unique=False)
    op.create_index("ix_applicants_stage", "applicants", ["stage"], unique=False)
    op.create_index("ix_applicants_stage_language", "applicants", ["stage", "primary_language"], unique=False)
    op.create_index("ix_applicants_track_stage", "applicants", ["specialization_track", "stage"], unique=False)
    op.create_index("ix_applicants_user_id", "applicants", ["user_id"], unique=True)

    op.create_table(
        "applicant_trial_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=False),
        sa.Column("task_index", sa.Integer(), nullable=False),
        sa.Column("task_prompt", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.Column("reviewer_1_id", sa.Integer(), nullable=True),
        sa.Column("reviewer_1_score", sa.Integer(), nullable=True),
        sa.Column("reviewer_1_notes", sa.Text(), nullable=True),
        sa.Column("reviewer_1_graded_at", sa.DateTime(), nullable=True),
        sa.Column("reviewer_2_id", sa.Integer(), nullable=True),
        sa.Column("reviewer_2_score", sa.Integer(), nullable=True),
        sa.Column("reviewer_2_notes", sa.Text(), nullable=True),
        sa.Column("reviewer_2_graded_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "status IN ('submitted', 'reviewed', 'passed', 'failed')",
            name="ck_applicant_trial_status",
        ),
        sa.ForeignKeyConstraint(["applicant_id"], ["applicants.id"]),
        sa.ForeignKeyConstraint(["reviewer_1_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewer_2_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_applicant_trial_submissions_applicant_id", "applicant_trial_submissions", ["applicant_id"], unique=False)
    op.create_index("ix_trial_sub_applicant_task", "applicant_trial_submissions", ["applicant_id", "task_index"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_trial_sub_applicant_task", table_name="applicant_trial_submissions")
    op.drop_index("ix_applicant_trial_submissions_applicant_id", table_name="applicant_trial_submissions")
    op.drop_table("applicant_trial_submissions")
    op.drop_index("ix_applicants_user_id", table_name="applicants")
    op.drop_index("ix_applicants_track_stage", table_name="applicants")
    op.drop_index("ix_applicants_stage_language", table_name="applicants")
    op.drop_index("ix_applicants_stage", table_name="applicants")
    op.drop_index("ix_applicants_specialization_track", table_name="applicants")
    op.drop_index("ix_applicants_admin_decision", table_name="applicants")
    op.drop_table("applicants")
