"""Rubric dimensions on projects (RLHF Phase 5).

Adds projects.rubric_dimensions (newline-separated dimension names,
empty = rubric off). Preference tasks in a project with dimensions
show per-response range sliders and store the scores in the output
document's "rubric" key.

Revision ID: d7f2b9e46a10
Revises: c4e8a1f37d52
Create Date: 2026-09-25 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d7f2b9e46a10"
down_revision = "c4e8a1f37d52"
branch_labels = None
depends_on = None


def upgrade():
    """Add projects.rubric_dimensions (nullable Text, NULL = off)."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("rubric_dimensions", sa.Text(), nullable=True)
        )


def downgrade():
    """Drop the rubric dimension list (scores live in output docs)."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("rubric_dimensions")
