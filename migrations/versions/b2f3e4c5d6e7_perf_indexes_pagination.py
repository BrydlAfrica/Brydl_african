"""b2f3e4c5d6e7 — Performance indexes for Phase 11+ tables.

Adds composite indexes on feedback_threads, project_assignments and
media_assets to keep admin/PM list views fast under load. No data
migration; purely additive.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2f3e4c5d6e7"
down_revision = "a1f2e3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("feedback_threads") as batch_op:
        batch_op.create_index(
            "ix_feedback_threads_kind_created", ["kind", "created_at"], unique=False,
        )
        batch_op.create_index(
            "ix_feedback_threads_author_created", ["author_id", "created_at"], unique=False,
        )
    with op.batch_alter_table("project_assignments") as batch_op:
        batch_op.create_index(
            "ix_assignments_user_role", ["user_id", "role_in_project"], unique=False,
        )
        batch_op.create_index(
            "ix_assignments_project_role", ["project_id", "role_in_project"], unique=False,
        )
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.create_index(
            "ix_media_project_kind", ["project_id", "kind"], unique=False,
        )
        # Permit document / other kinds (PM media library uploads).
        batch_op.drop_constraint("ck_media_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_media_kind",
            sa.text("kind IN ('image','audio','document','other')"),
        )


def downgrade() -> None:
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.drop_index("ix_media_project_kind")
        batch_op.drop_constraint("ck_media_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_media_kind", sa.text("kind IN ('image','audio')")
        )
    with op.batch_alter_table("feedback_threads") as batch_op:
        batch_op.drop_index("ix_feedback_threads_kind_created")
        batch_op.drop_index("ix_feedback_threads_author_created")
    with op.batch_alter_table("project_assignments") as batch_op:
        batch_op.drop_index("ix_assignments_user_role")
        batch_op.drop_index("ix_assignments_project_role")
