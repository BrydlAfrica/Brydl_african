"""Add annotator payout account type and contact phone.

- users.bank_account_type: "savings" | "current" (NULL until the
  annotator saves payout details; cleared together with them).
- users.phone: optional contact number for payment questions. Both
  columns are nullable, so existing PostgreSQL rows need no
  server_default.

Revision ID: aadccd5f13e2
Revises: 736be0df0ee9
Create Date: 2026-09-13 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "aadccd5f13e2"
down_revision = "736be0df0ee9"
branch_labels = None
depends_on = None


def upgrade():
    """Add the two nullable annotator-supplied columns."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("bank_account_type", sa.String(length=10),
                      nullable=True)
        )
        batch_op.add_column(
            sa.Column("phone", sa.String(length=24), nullable=True)
        )


def downgrade():
    """Drop the two columns (annotators re-enter the details)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("phone")
        batch_op.drop_column("bank_account_type")
