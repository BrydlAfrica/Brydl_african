"""Index users.reset_token_hash.

Password-reset links look users up by hashed-token equality; this
keeps that lookup indexed per the scalability standard (every filtered
column carries an index). Non-unique: NULL (no reset in flight) is the
common case and NULLs never collide in a non-unique index.

Revision ID: f7353e97b8cc
Revises: 041200d1ec79
Create Date: 2026-09-12 UTC
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "f7353e97b8cc"
down_revision = "041200d1ec79"
branch_labels = None
depends_on = None


def upgrade():
    """Create the reset-token lookup index."""
    op.create_index(
        "ix_users_reset_token_hash",
        "users",
        ["reset_token_hash"],
        unique=False,
    )


def downgrade():
    """Drop the reset-token lookup index."""
    op.drop_index("ix_users_reset_token_hash", table_name="users")
