"""SFT builder: sft_examples table for gold-rewrite training pairs.

RLHF Phase 4. Each row is one supervised-fine-tuning pair harvested
from finalized work (consensus ruling, approved submission or seed
gold) with an immutable source_completion audit anchor, a written
edit note on every human rewrite, and a draft -> ready -> exported
lifecycle (exported is terminal and frozen).

Revision ID: c4e8a1f37d52
Revises: b2f4c9d61a83
Create Date: 2026-09-24 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e8a1f37d52"
down_revision = "b2f4c9d61a83"
branch_labels = None
depends_on = None


def upgrade():
    """Create sft_examples (SFT training pairs with rewrite audit)."""
    op.create_table(
        "sft_examples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("source_completion", sa.Text(), nullable=False),
        sa.Column("completion", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("edit_note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'exported')",
            name="ck_sft_status",
        ),
        sa.CheckConstraint(
            "source IN ('consensus', 'approved', 'gold', 'manual')",
            name="ck_sft_source",
        ),
    )
    op.create_index(
        "ix_sft_examples_task_id", "sft_examples", ["task_id"]
    )
    op.create_index(
        "ix_sft_examples_project_id", "sft_examples", ["project_id"]
    )
    op.create_index(
        "ix_sft_examples_status", "sft_examples", ["status"]
    )


def downgrade():
    """Drop the SFT builder table (training pairs are reproducible)."""
    op.drop_table("sft_examples")
