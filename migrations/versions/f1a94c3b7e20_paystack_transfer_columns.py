"""Paystack Transfers: recipient codes on users, references on payments.

Tier 2 payouts. users.paystack_recipient_code caches the Paystack
transfer recipient (one per annotator, created on first Paystack
payout from their stored bank details). payments.paystack_reference
carries the idempotency reference of the initiated transfer so
webhooks can settle the payment row.

Revision ID: f1a94c3b7e20
Revises: e5c7d2f84b19
Create Date: 2026-09-25 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1a94c3b7e20"
down_revision = "e5c7d2f84b19"
branch_labels = None
depends_on = None


def upgrade():
    """Add the Paystack columns (nullable — the CSV flow stays valid)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("paystack_recipient_code", sa.String(length=100),
                      nullable=True)
        )
    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("paystack_reference", sa.String(length=100),
                      nullable=True)
        )
    op.create_index(
        "ix_payments_paystack_reference", "payments",
        ["paystack_reference"],
    )


def downgrade():
    """Drop the Paystack columns (pending payouts keep CSV fallback)."""
    op.drop_index("ix_payments_paystack_reference", table_name="payments")
    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.drop_column("paystack_reference")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("paystack_recipient_code")
