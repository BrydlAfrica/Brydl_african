"""project manager role, site settings, feedback/messages (chain 18)

Revision ID: a1f2e3b4c5d6
Revises: c7d21b8a45e9
Create Date: 2026-10-02 09:30:00 UTC

Adds:
- project_manager user role (users.role enum extended)
- projects.proposed_pay_per_task, approval_note, approved_by, approved_at
- projects.status extended with draft/pending_approval/rejected; default changed to 'draft'
- project_assignments table (PM <-> project assignments, unique per pair)
- site_settings table (admin-editable operational knobs)
- feedback_threads + feedback_replies (Option C: feedback + broadcasts)
- users.theme default already exists.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text as sa_text  # noqa: F401 (reserved for future seed SQL)

revision = "a1f2e3b4c5d6"
down_revision = "c7d21b8a45e9"
branch_labels = None
depends_on = None


def _recreate_users_with_new_role() -> None:
    """Swap the users.role CHECK constraint to allow project_manager.

    SQLite does not support ALTER TABLE DROP CONSTRAINT; batch_alter_table
    creates a fresh table with the desired schema and copies rows.
    """
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            sa.text(
                "role IN ('admin','project_manager','qa','auditor','annotator')"
            ),
        )


def _recreate_projects_for_new_statuses() -> None:
    """Widen projects.status allowed set, relax pay_per_task NOT NULL, and
    add PM-sign-off columns.

    SQLite's batch_alter_table rewrites the table; we must declare
    every column we want to carry across (existing_type/existing_nullable
    for un-changed columns) or batch_alter_table drops their nullability.
    """
    with op.batch_alter_table("projects") as batch_op:
        # New PM-sign-off columns first.
        batch_op.add_column(
            sa.Column(
                "proposed_pay_per_task",
                sa.Numeric(precision=10, scale=2),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("approval_note", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("approved_by", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("approved_at", sa.DateTime(), nullable=True)
        )
        # pay_per_task becomes nullable for draft proposals (was NOT NULL).
        batch_op.alter_column(
            "pay_per_task",
            existing_type=sa.Numeric(precision=10, scale=2),
            existing_nullable=False,
            nullable=True,
        )
        # Recreate the status CHECK with the widened allowed set.
        batch_op.drop_constraint("ck_projects_status", type_="check")
        batch_op.create_check_constraint(
            "ck_projects_status",
            sa.text(
                "status IN ('draft','pending_approval','active','paused','completed','rejected')"
            ),
        )
        # Replace the strict pay_per_task > 0 constraint with the
        # nullable-tolerant version.
        batch_op.drop_constraint("ck_projects_pay_per_task", type_="check")
        batch_op.create_check_constraint(
            "ck_projects_pay_per_task",
            sa.text("pay_per_task IS NULL OR pay_per_task > 0"),
        )
        batch_op.create_check_constraint(
            "ck_projects_proposed_pay",
            sa.text("proposed_pay_per_task IS NULL OR proposed_pay_per_task > 0"),
        )
        batch_op.create_foreign_key(
            "fk_projects_approved_by_users",
            "users",
            ["approved_by"],
            ["id"],
        )


def upgrade() -> None:
    """Apply chain 18."""
    # 1. Widen users.role enum.
    _recreate_users_with_new_role()
    # 2. Projects: new columns + widened status/pay constraints.
    _recreate_projects_for_new_statuses()
    # 3. project_assignments
    op.create_table(
        "project_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), nullable=False, index=True),
        sa.Column(
            "role_in_project",
            sa.String(length=20),
            nullable=False,
            server_default="project_manager",
        ),
        sa.Column("assigned_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_assignments_project"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_assignments_user"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by"], ["users.id"], name="fk_assignments_assigner"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_assignments_project_user"
        ),
    )
    # 4. site_settings
    op.create_table(
        "site_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    # Seed editable settings rows with sensible defaults (admin can
    # change all of these later through /admin/settings). The USD->NGN
    # rate is the one you explicitly asked to be editable. Use raw SQL
    # for the seed so there is no bulk_insert type-coercion mismatch
    # across SQLite/PostgreSQL backends.
    seed_rows = [
        ("usd_to_ngn_rate", "1600", "USD -> Naira display rate (Decimal)."),
        ("site_name", "Brydl Dashboard", "Brand name shown in the top bar."),
        ("support_email", "support@brydl.ng",
         "Address staff see for urgent contact."),
        ("pagination_per_page", "20", "Default rows per list page."),
        ("upload_batch_limit", "500",
         "Max rows accepted in a single CSV upload."),
        ("claim_daily_cap", "20",
         "Max self-claim tasks per annotator per UTC day."),
        ("task_max_rejections", "3",
         "Rejections before a task is routed for revision."),
        ("preference_min_justification", "40",
         "Min chars for preference-judgement justifications."),
        ("api_rate_limit_per_minute", "60",
         "Per-API-key requests per minute."),
    ]
    conn = op.get_bind()
    for key, value, desc in seed_rows:
        conn.execute(
            sa.text(
                "INSERT INTO site_settings (key, value, description, updated_at)"
                " VALUES (:k, :v, :d, CURRENT_TIMESTAMP)"
            ),
            {"k": key, "v": value, "d": desc},
        )
    # 5. feedback_threads + replies
    op.create_table(
        "feedback_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind", sa.String(length=20), nullable=False, server_default="feedback"
        ),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="new"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_feedback_author"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('feedback','broadcast')",
            name="ck_feedback_kind",
        ),
        sa.CheckConstraint(
            "status IN ('new','reviewed','resolved')",
            name="ck_feedback_status",
        ),
    )
    op.create_index(
        "ix_feedback_threads_author_id",
        "feedback_threads",
        ["author_id"],
    )
    op.create_index(
        "ix_feedback_threads_created_at",
        "feedback_threads",
        ["created_at"],
    )
    op.create_index(
        "ix_feedback_threads_status",
        "feedback_threads",
        ["status"],
    )
    op.create_table(
        "feedback_replies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["feedback_threads.id"],
            name="fk_reply_thread",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name="fk_reply_author"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feedback_replies_thread_id",
        "feedback_replies",
        ["thread_id"],
    )
    op.create_index(
        "ix_feedback_replies_created_at",
        "feedback_replies",
        ["created_at"],
    )
    # Notification kind widening for feedback events: notifications.kind
    # has no CHECK constraint against a closed list (only 'length > 0'),
    # so no DDL is needed — feedback.broadcast / feedback.reply /
    # project.approved / project.rejected will just appear as strings.


def downgrade() -> None:
    """Revert chain 18; drops new tables + columns and rewinds role enum."""
    op.drop_table("feedback_replies")
    op.drop_table("feedback_threads")
    op.drop_table("site_settings")
    op.drop_table("project_assignments")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("ck_projects_proposed_pay", type_="check")
        batch_op.drop_constraint("ck_projects_pay_per_task", type_="check")
        batch_op.create_check_constraint(
            "ck_projects_pay_per_task", sa.text("pay_per_task > 0")
        )
        batch_op.drop_constraint("ck_projects_status", type_="check")
        batch_op.create_check_constraint(
            "ck_projects_status",
            sa.text("status IN ('active','paused','completed')"),
        )
        batch_op.drop_constraint("fk_projects_approved_by_users", type_="foreignkey")
        batch_op.drop_column("approved_at")
        batch_op.drop_column("approved_by")
        batch_op.drop_column("approval_note")
        batch_op.drop_column("proposed_pay_per_task")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role",
            sa.text("role IN ('admin','qa','auditor','annotator')"),
        )
