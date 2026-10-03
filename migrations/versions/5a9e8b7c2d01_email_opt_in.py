"""Add per-user opt-in for notification email mirror.

Phase 13-H: annotators and PMs can opt into having in-app notifications
(assignment, rejection, payout, approval, etc.) mirrored to their
registered email. Existing users default to False so nothing starts
mailing without an explicit choice; reset/password mail still goes out
regardless (transactional, not marketing).

Revision ID: 5a9e8b7c2d01
Revises: f1a94c3b7e20
Create Date: 2026-10-03 UTC
"""

import sqlalchemy as sa
from alembic import op


revision = "5a9e8b7c2d01"
down_revision = "b2f3e4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    """Add the email_notifications_opt_in column (default False)."""
    op.add_column(
        "users",
        sa.Column(
            "email_notifications_opt_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    """Drop the column."""
    op.drop_column("users", "email_notifications_opt_in")
