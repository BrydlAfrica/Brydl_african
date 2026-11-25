"""Migration 25: Add contributor_consents table for durable legal consent tracking.

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-11-24 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contributor_consents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("document_hash", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column(
            "consent_method",
            sa.String(length=32),
            server_default="web_form",
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "document_type", "version", name="uq_contributor_consents_user_doc_ver"
        ),
    )
    with op.batch_alter_table("contributor_consents", schema=None) as batch_op:
        batch_op.create_index(
            "ix_contributor_consents_user_id",
            ["user_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("contributor_consents", schema=None) as batch_op:
        batch_op.drop_index("ix_contributor_consents_user_id")
    op.drop_table("contributor_consents")
