"""Add annotator payout-account (bank) details.

bank_name / bank_account_number / bank_account_name are nullable as a
unit and filled by the annotator on their profile; admins see them
masked on the Team page and in full inside bank-batch payout CSVs.

Revision ID: 041200d1ec79
Revises: 4af7971b8512
Create Date: 2026-09-12 14:53:31.797684

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '041200d1ec79'
down_revision = '4af7971b8512'
branch_labels = None
depends_on = None


def upgrade():
    """Add the three nullable payout-account columns to users."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("bank_name", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "bank_account_number",
                sa.String(length=20),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "bank_account_name",
                sa.String(length=120),
                nullable=True,
            )
        )


def downgrade():
    """Drop the payout-account columns (data is lost)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("bank_account_name")
        batch_op.drop_column("bank_account_number")
        batch_op.drop_column("bank_name")
