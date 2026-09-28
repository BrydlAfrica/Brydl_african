"""Encrypt bank numbers: widen the column, add last4 for display.

bank_account_number now stores Fernet ciphertext (see
security_crypto.py); bank_account_last4 keeps the masked Team-page
display without touching the decryption key. Rows written before this
migration hold legacy plaintext and keep working — reads fall back
gracefully and the next save re-encrypts. No data rewrite here on
purpose: the key lives in the app environment, not in the migration.

Revision ID: e91c73a40b57
Revises: c3f81b92ad04
Create Date: 2026-09-18 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e91c73a40b57"
down_revision = "c3f81b92ad04"
branch_labels = None
depends_on = None


def upgrade():
    """Widen the ciphertext column and add the display helper."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "bank_account_number",
            existing_type=sa.String(length=20),
            type_=sa.String(length=512),
            existing_nullable=True,
        )
        batch_op.add_column(
            sa.Column("bank_account_last4", sa.String(length=4),
                      nullable=True)
        )


def downgrade():
    """Drop the helper; shrink the number column (plaintext era)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("bank_account_last4")
        batch_op.alter_column(
            "bank_account_number",
            existing_type=sa.String(length=512),
            type_=sa.String(length=20),
            existing_nullable=True,
        )
