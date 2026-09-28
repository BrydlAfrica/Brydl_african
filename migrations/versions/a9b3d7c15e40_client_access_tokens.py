"""Client portal: client_access_tokens for scoped client delivery.

Phase 7. One row per issued client link: the sha256 of an opaque
32-byte token, scoped to a project, instantly revocable. Clients
follow progress and download approved client-safe deliverables
without an account; annotator identities and gold answers never
leave the platform through it.

Revision ID: a9b3d7c15e40
Revises: f1a94c3b7e20
Create Date: 2026-09-25 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a9b3d7c15e40"
down_revision = "f1a94c3b7e20"
branch_labels = None
depends_on = None


def upgrade():
    """Create client_access_tokens (hash-only token storage)."""
    op.create_table(
        "client_access_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_ip", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_client_access_tokens_project_id", "client_access_tokens",
        ["project_id"],
    )
    op.create_index(
        "ix_client_access_tokens_token_hash", "client_access_tokens",
        ["token_hash"], unique=True,
    )


def downgrade():
    """Drop the client portal table (links simply stop resolving)."""
    op.drop_table("client_access_tokens")
