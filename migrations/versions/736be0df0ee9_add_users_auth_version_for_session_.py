"""Add users.auth_version for credential-change session revocation.

The counter is baked into the Flask-Login session id
(User.get_id -> "id.version"); bumping it on any password
change/reset instantly invalidates every existing session and
remember-me cookie for the account. Legacy cookies (bare id, no
suffix) are treated as version 0.

Revision ID: 736be0df0ee9
Revises: f7353e97b8cc
Create Date: 2026-09-13 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "736be0df0ee9"
down_revision = "f7353e97b8cc"
branch_labels = None
depends_on = None


def upgrade():
    """Add the auth-version counter (existing accounts start at 0)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auth_version",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade():
    """Drop the auth-version counter (all sessions re-authenticate)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("auth_version")
