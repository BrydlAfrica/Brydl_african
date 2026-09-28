"""Tier 1: task claims, project deadlines, notifications.

- tasks.claimed_at: UTC timestamp when an annotator SELF-claimed the
  task (admin assignment leaves it NULL) — feeds the daily claim cap.
- projects.deadline: optional due date shown to admins/annotators.
- notifications: the in-app bell feed (user, kind, payload JSON,
  read_at) with the unread-badge index.

Revision ID: c3f81b92ad04
Revises: aadccd5f13e2
Create Date: 2026-09-17 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3f81b92ad04"
down_revision = "aadccd5f13e2"
branch_labels = None
depends_on = None


def upgrade():
    """Add the Tier 1 schema (all nullable except notification rows)."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("deadline", sa.Date(), nullable=True)
        )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("claimed_at", sa.DateTime(), nullable=True)
        )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "length(kind) > 0", name="ck_notifications_kind"
        ),
    )
    op.create_index(
        "ix_notifications_user_id", "notifications", ["user_id"]
    )
    op.create_index(
        "ix_notifications_created_at", "notifications", ["created_at"]
    )
    op.create_index(
        "ix_notifications_user_read",
        "notifications",
        ["user_id", "read_at"],
    )


def downgrade():
    """Drop the Tier 1 schema."""
    op.drop_index(
        "ix_notifications_user_read", table_name="notifications"
    )
    op.drop_index(
        "ix_notifications_created_at", table_name="notifications"
    )
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("claimed_at")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("deadline")
