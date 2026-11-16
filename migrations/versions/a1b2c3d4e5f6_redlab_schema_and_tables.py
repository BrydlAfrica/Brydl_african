"""Migration 18: RedLab schema, four new tables, redteam columns.

Creates the isolated Postgres schema ``redlab`` and four tables
(``targets``, ``sessions``, ``messages``, ``processed_events``) for
the v14 Dynamic Red-Teaming as a Service product line. Adds three
``redteam_*`` columns to ``public.submissions`` and three to
``public.projects`` for linking, state tracking, and Decimal-pricing
of red-team work.

Dialect: on Postgres the four tables live in schema ``redlab``; on
SQLite (local dev) they live in the default schema because SQLite has
no real schema support. The downgrade is fully reversible in both
cases.

Revision ID: a1b2c3d4e5f6
Revises: 5a9e8b7c2d01
Create Date: 2026-10-08 UTC
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "5a9e8b7c2d01"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _schema() -> str | None:
    return "redlab" if _dialect() == "postgresql" else None


def _has_table(name: str) -> bool:
    """Check whether a RedLab table exists (schema-aware)."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    sch = _schema()
    return name in insp.get_table_names(schema=sch)


def _has_column(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    return any(c["name"] == name for c in insp.get_columns(table))


# Shared column definitions so upgrade/downgrade/constraints are
# defined in one place.
def _targets_fk(col: str = "id") -> str:
    return f"redlab.targets.{col}" if _schema() else f"targets.{col}"


def _sessions_fk(col: str = "id") -> str:
    return f"redlab.sessions.{col}" if _schema() else f"sessions.{col}"


def upgrade():
    """Create redlab schema, four tables, and additive columns."""
    dialect = _dialect()
    schema = _schema()
    tab_kw = {"schema": schema} if schema else {}

    # 1. Create schema on Postgres.
    if dialect == "postgresql":
        op.execute("CREATE SCHEMA IF NOT EXISTS redlab")

    # 2. redlab.targets
    if not _has_table("targets"):
        op.create_table(
            "targets",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("project_ref", sa.String(32),
                      nullable=False, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("endpoint", sa.String(1000), nullable=False),
            sa.Column("model_name", sa.String(200), nullable=False),
            sa.Column("api_key_ciphertext", sa.Text, nullable=False),
            sa.Column("key_fingerprint", sa.String(64), nullable=False),
            sa.Column("ssrf_validated_at", sa.DateTime, nullable=True),
            sa.Column(
                "active",
                sa.Boolean,
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("created_at", sa.DateTime, nullable=False),
            **tab_kw,
        )

    # 3. redlab.sessions
    if not _has_table("sessions"):
        op.create_table(
            "sessions",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("submission_ref", sa.String(32), nullable=True),
            sa.Column("annotator_ref", sa.String(
                32), nullable=False, index=True),
            sa.Column("project_ref", sa.String(32),
                      nullable=False, index=True),
            sa.Column(
                "target_id",
                sa.String(32),
                sa.ForeignKey(_targets_fk()),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="'open'",
            ),
            sa.Column(
                "turn_count",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "breach_count",
                sa.Integer,
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("conclude_note", sa.Text, nullable=True),
            sa.Column("concluded_at", sa.DateTime, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.CheckConstraint(
                "status IN ('open', 'concluded')",
                name="ck_sessions_status",
            ),
            sa.CheckConstraint(
                "turn_count >= 0", name="ck_sessions_turn_count_nonneg"
            ),
            sa.CheckConstraint(
                "breach_count >= 0", name="ck_sessions_breach_count_nonneg"
            ),
            sa.UniqueConstraint(
                "submission_ref", name="uq_sessions_submission_ref"
            ),
            **tab_kw,
        )

    # 4. redlab.messages
    if not _has_table("messages"):
        op.create_table(
            "messages",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "session_id",
                sa.String(32),
                sa.ForeignKey(_sessions_fk(), ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "turn_index", sa.Integer, nullable=False, index=True
            ),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("client_turn_id", sa.String(
                64), nullable=True, index=True),
            sa.Column(
                "is_unsafe_flag",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "redacted",
                sa.Boolean,
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.UniqueConstraint(
                "session_id", "turn_index", name="uq_messages_session_turn"
            ),
            sa.CheckConstraint(
                "role IN ('user', 'assistant', 'system')",
                name="ck_messages_role",
            ),
            sa.CheckConstraint(
                "turn_index >= 0", name="ck_messages_turn_index_nonneg"
            ),
            **tab_kw,
        )

    # 5. redlab.processed_events
    if not _has_table("processed_events"):
        op.create_table(
            "processed_events",
            sa.Column("event_id", sa.String(32), primary_key=True),
            sa.Column("processed_at", sa.DateTime, nullable=False),
            **tab_kw,
        )

    # 6+7. Additive columns on public.submissions + public.projects.
    #
    # We use op.batch_alter_table for portability (SQLite cannot
    # ALTER TABLE to add constraints; batch_alter_table transparently
    # uses copy-rebuild on SQLite and plain ALTER on Postgres).
    if not _has_column("submissions", "redlab_session_id"):
        with op.batch_alter_table("submissions") as batch:
            batch.add_column(sa.Column(
                "redlab_session_id", sa.String(32), nullable=True,
            ))
            batch.create_unique_constraint(
                "uq_submissions_redlab_session_id", ["redlab_session_id"],
            )
    if not _has_column("submissions", "redteam_state"):
        with op.batch_alter_table("submissions") as batch:
            batch.add_column(sa.Column(
                "redteam_state", sa.String(32), nullable=False,
                server_default="'none'",
            ))
            batch.create_index(
                "ix_submissions_redteam_state",
                ["redteam_state"], unique=False,
            )
    if not _has_column("submissions", "redteam_breach_count"):
        with op.batch_alter_table("submissions") as batch:
            batch.add_column(sa.Column(
                "redteam_breach_count", sa.Integer, nullable=False,
                server_default=sa.text("0"),
            ))
    if not _has_column("projects", "redteam_base_pay"):
        with op.batch_alter_table("projects") as batch:
            batch.add_column(sa.Column(
                "redteam_base_pay", sa.Numeric(10, 2), nullable=False,
                server_default=sa.text("0.00"),
            ))
    if not _has_column("projects", "redteam_breach_bounty"):
        with op.batch_alter_table("projects") as batch:
            batch.add_column(sa.Column(
                "redteam_breach_bounty", sa.Numeric(10, 2), nullable=False,
                server_default=sa.text("0.00"),
            ))
    if not _has_column("projects", "redteam_session_quota"):
        with op.batch_alter_table("projects") as batch:
            batch.add_column(sa.Column(
                "redteam_session_quota", sa.Integer, nullable=False,
                server_default=sa.text("0"),
            ))


def downgrade():
    """Reverse: drop additive columns + the four redlab tables + schema."""
    dialect = _dialect()
    schema = _schema()
    tab_kw = {"schema": schema} if schema else {}

    # Drop tables in reverse order to respect FKs.
    for name in ("processed_events", "messages", "sessions", "targets"):
        if _has_table(name):
            op.drop_table(name, **tab_kw)

    # Additive columns — drop if present (batch mode for SQLite compat).
    if _has_column("submissions", "redteam_breach_count"):
        with op.batch_alter_table("submissions") as batch:
            batch.drop_column("redteam_breach_count")
    if _has_column("submissions", "redteam_state"):
        with op.batch_alter_table("submissions") as batch:
            batch.drop_index("ix_submissions_redteam_state")
            batch.drop_column("redteam_state")
    if _has_column("submissions", "redlab_session_id"):
        with op.batch_alter_table("submissions") as batch:
            batch.drop_constraint(
                "uq_submissions_redlab_session_id", type_="unique",
            )
            batch.drop_column("redlab_session_id")
    for col in ("redteam_session_quota", "redteam_breach_bounty",
                "redteam_base_pay"):
        if _has_column("projects", col):
            with op.batch_alter_table("projects") as batch:
                batch.drop_column(col)

    if dialect == "postgresql":
        op.execute("DROP SCHEMA IF EXISTS redlab CASCADE")
