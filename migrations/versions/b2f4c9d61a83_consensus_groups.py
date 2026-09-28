"""Consensus engine: groups table, sibling links, project depth.

ConsensusGroups carry blind multi-annotation rounds for RLHF work:
projects.required_annotations selects the depth (1 = off), sibling
tasks link via tasks.consensus_group_id, and the group's status tracks
pending / agreed / conflict / resolved (arbitration is binding and
frozen once resolved).

Revision ID: b2f4c9d61a83
Revises: e91c73a40b57
Create Date: 2026-09-20 UTC
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2f4c9d61a83"
down_revision = "e91c73a40b57"
branch_labels = None
depends_on = None


def upgrade():
    """Create consensus_groups, link tasks, add project depth."""
    op.create_table(
        "consensus_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("required_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_choice", sa.String(length=100),
                  nullable=True),
        sa.Column("decided_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'agreed', 'conflict', 'resolved')",
            name="ck_consensus_status",
        ),
    )
    op.create_index(
        "ix_consensus_groups_project_id",
        "consensus_groups",
        ["project_id"],
    )
    op.create_index(
        "ix_consensus_groups_status", "consensus_groups", ["status"]
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("consensus_group_id", sa.Integer(),
                      nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_tasks_consensus_group",
            "consensus_groups",
            ["consensus_group_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_tasks_consensus_group_id", ["consensus_group_id"]
        )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("required_annotations", sa.Integer(),
                      nullable=False, server_default="1")
        )
        batch_op.create_check_constraint(
            "ck_projects_consensus_depth",
            "required_annotations IN (1, 2, 3)",
        )


def downgrade():
    """Drop the consensus schema (groups first, then columns)."""
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint("ck_projects_consensus_depth",
                                 type_="check")
        batch_op.drop_column("required_annotations")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_tasks_consensus_group_id")
        batch_op.drop_constraint("fk_tasks_consensus_group",
                                 type_="foreignkey")
        batch_op.drop_column("consensus_group_id")
    op.drop_index("ix_consensus_groups_status",
                  table_name="consensus_groups")
    op.drop_index("ix_consensus_groups_project_id",
                  table_name="consensus_groups")
    op.drop_table("consensus_groups")
