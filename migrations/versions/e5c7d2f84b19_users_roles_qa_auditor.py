"""Widen user roles: add qa and auditor staff roles.

RLHF Phase 6. QA reviewers work the submission-review queue; auditors
own consensus arbitration and the audit trail. Admins keep full
control. The role CHECK constraint is recreated with the widened
vocabulary; existing rows (admin/annotator) satisfy it unchanged.

Revision ID: e5c7d2f84b19
Revises: d7f2b9e46a10
Create Date: 2026-09-25 UTC
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e5c7d2f84b19"
down_revision = "d7f2b9e46a10"
branch_labels = None
depends_on = None


def upgrade():
    """Recreate ck_users_role with admin/qa/auditor/annotator."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            "role IN ('admin', 'qa', 'auditor', 'annotator')",
        )


def downgrade():
    """Restore the two-role constraint (fails if qa/auditor exist)."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            "role IN ('admin', 'annotator')",
        )
