"""Migration 24: Add specialization_track, constraint and indexes to applicants.

Revision ID: a7b8c9d0e1f2
Revises: e4f5a6b7c8d9
Create Date: 2026-11-22 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("applicants", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "specialization_track",
                sa.String(length=50),
                nullable=False,
                server_default="translation",
            )
        )
        batch_op.create_check_constraint(
            "ck_applicants_specialization_track",
            "specialization_track IN ('red_teaming', 'translation', 'rlhf_preference', 'audio_speech', 'general_labeling')",
        )
        batch_op.create_index(
            "ix_applicants_specialization_track",
            ["specialization_track"],
            unique=False,
        )
        batch_op.create_index(
            "ix_applicants_track_stage",
            ["specialization_track", "stage"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("applicants", schema=None) as batch_op:
        batch_op.drop_index("ix_applicants_track_stage")
        batch_op.drop_index("ix_applicants_specialization_track")
        batch_op.drop_constraint("ck_applicants_specialization_track", type_="check")
        batch_op.drop_column("specialization_track")
