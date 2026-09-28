"""Media workspaces: media_assets for image/audio task content.

Phase 8. Bytes are stored in the row (LargeBinary) — $0 storage,
included in database backups, survives redeploys. Unique
(project, sha256) makes re-uploads dedupe; kind/content_type carry
the upload-time allowlist decision so serving never trusts the
browser again.

Revision ID: b8e40c92f7d1
Revises: a9b3d7c15e40
Create Date: 2026-09-25 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8e40c92f7d1"
down_revision = "a9b3d7c15e40"
branch_labels = None
depends_on = None


def upgrade():
    """Create media_assets (bytes in-row, dedup per project)."""
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('image', 'audio')",
                           name="ck_media_kind"),
        sa.CheckConstraint("byte_size > 0", name="ck_media_size"),
    )
    op.create_index(
        "ix_media_assets_project_id", "media_assets", ["project_id"]
    )
    op.create_index(
        "ix_media_assets_project_sha", "media_assets",
        ["project_id", "sha256"], unique=True,
    )


def downgrade():
    """Drop media assets (task documents keep their metadata)."""
    op.drop_table("media_assets")
