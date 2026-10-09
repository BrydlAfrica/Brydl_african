"""Migration 19: submissions.status accepts 'in_progress' for RedLab chats.

Phase 6 (dynamic red-team annotator UI) creates a Submission row the
moment an annotator opens a red-team task, so the chat can carry a
submission id through the Flask integration bridge before the chat
concludes. That row must exist (for FK link + submission_ref binding)
but must NOT appear in the QA review queue — the queue filters on
``Submission.status == 'submitted'`` with ``submitted_at NOT NULL``,
so a new ``'in_progress'`` status (with ``submitted_at = NULL``)
keeps those rows out of QA until the annotator concludes their
session (at which point :func:`redlab.integration._mark_submitted`
flips them to ``'submitted'`` and stamps ``submitted_at``).

Dialect: uses batch_alter_table so the CHECK constraint is recreated
portably on both SQLite and Postgres.

Revision ID: b3c4d5e6f701
Revises: a1b2c3d4e5f6
Create Date: 2026-10-09 UTC
"""
from alembic import op
import sqlalchemy as sa


revision = "b3c4d5e6f701"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


# Keep the old/new status sets in lockstep with models.SUBMISSION_STATUSES.
_OLD_STATUSES = ("submitted", "approved", "rejected", "revision")
_NEW_STATUSES = _OLD_STATUSES + ("in_progress",)


def _status_check(statuses: tuple[str, ...]) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"status IN {statuses!r}", name="ck_submissions_status",
    )


def upgrade() -> None:
    # 1) widen ck_submissions_status to include 'in_progress'.
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint("ck_submissions_status", type_="check")
    with op.batch_alter_table("submissions") as batch:
        batch.create_check_constraint(
            "ck_submissions_status",
            condition=(
                "status IN ('submitted','approved','rejected',"
                "'revision','in_progress')"
            ),
        )
    # 2) widen ck_submissions_review_integrity to exempt 'in_progress'
    #    (live red-team chats have no qa_score yet).
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint(
            "ck_submissions_review_integrity", type_="check"
        )
    with op.batch_alter_table("submissions") as batch:
        batch.create_check_constraint(
            "ck_submissions_review_integrity",
            condition=(
                "status IN ('submitted','revision','in_progress')"
                " OR (reviewed_at IS NOT NULL AND qa_score IS NOT NULL)"
            ),
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM submissions WHERE status = 'in_progress'"
    )
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint("ck_submissions_status", type_="check")
    with op.batch_alter_table("submissions") as batch:
        batch.create_check_constraint(
            "ck_submissions_status",
            condition=(
                "status IN ('submitted','approved','rejected','revision')"
            ),
        )
    with op.batch_alter_table("submissions") as batch:
        batch.drop_constraint(
            "ck_submissions_review_integrity", type_="check"
        )
    with op.batch_alter_table("submissions") as batch:
        batch.create_check_constraint(
            "ck_submissions_review_integrity",
            condition=(
                "status IN ('submitted','revision')"
                " OR (reviewed_at IS NOT NULL AND qa_score IS NOT NULL)"
            ),
        )
