"""RedLab SQLAlchemy models (v14 Phase 2).

Four tables live in the Postgres schema ``redlab`` — isolated from the
Flask-owned ``public`` tables so RedLab evolution can never collide
with core Brydl tables. The additive columns on ``public.submissions``
and ``public.projects`` (``redteam_*`` columns) are the *only* schema
changes outside ``redlab.*``; they are created by Migration #18 and
are declared directly on the Submission / Project classes in
:mod:`models`.

All RedLab primary keys are UUID strings (rather than serial ints) so
they are safe to expose in client-side idempotency keys and URLs
without leaking count / enumeration information.

Dialect handling
----------------
SQLite (used for local dev and in-memory tests) has no real schema
support. We detect the dialect at table-configuration time and place
RedLab tables in the default schema on SQLite, but in the ``redlab``
schema on Postgres. The Alembic migration #18 mirrors this dialect
branch so upgrade/downgrade works on both backends.

.. note::

   These models are imported from :mod:`redlab.integration` (Phase 5)
   and the Phase 3-7 routers. Flask code MUST NOT import them directly
   anywhere else — the ``redlab.integration`` module is the sanctioned
   bridge. The import-linter contract enforced in Phase 9 codifies
   this.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from extensions import db


def _redlab_schema() -> Optional[str]:
    """Return the schema name for RedLab tables.

    Uses the same DATABASE_URL sniff that :mod:`config` uses to choose
    the SQLAlchemy dialect: ``"redlab"`` on Postgres, ``None``
    (default schema) on SQLite and anything else. Schema choice is a
    deploy-time configuration, not a runtime decision, so reading the
    env var at import time is deterministic and avoids binding the
    engine during model import.
    """
    import os as _os
    url = _os.environ.get("DATABASE_URL", "").lower().strip()
    # config.py rewrites Render's postgres:// to postgresql://; we
    # accept both.
    if "postgres" in url:
        return "redlab"
    return None  # SQLite (local dev / tests)


# Resolved once at import.
REDLAB_SCHEMA: Optional[str] = _redlab_schema()


def utc_now() -> datetime:
    """UTC timestamp with no tzinfo (matches models.utc_now convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _uuid() -> str:
    """Generate a new hex UUID primary key."""
    return uuid.uuid4().hex


# --- Valid status tuples (single source of truth) -------------------------
TARGET_STATUSES = ("active", "inactive")
SESSION_STATUSES = ("open", "concluded")
MESSAGE_ROLES = ("user", "assistant", "system")


def _fk(target_table: str, col: str = "id", **kw) -> ForeignKey:
    """Build a ForeignKey to a RedLab table, schema-qualified as needed.

    On Postgres this produces ``ForeignKey("redlab.<table>.<col>")``;
    on SQLite it produces ``ForeignKey("<table>.<col>")`` so the
    reference resolves in the default schema.
    """
    if REDLAB_SCHEMA:
        target = f"{REDLAB_SCHEMA}.{target_table}.{col}"
    else:
        target = f"{target_table}.{col}"
    return ForeignKey(target, **kw)


class Target(db.Model):
    """A client's LLM endpoint that annotators red-team against.

    Holds the endpoint URL, the Fernet-encrypted API key, a short
    fingerprint for UI display, and the validated-SSRF timestamp so
    we know the endpoint was screened against the SSRF guard
    (:mod:`redlab.ssrf`, Phase 3) before being enabled.
    """

    __tablename__ = "targets"
    __table_args__ = (
        {"schema": REDLAB_SCHEMA} if REDLAB_SCHEMA else {}
    )

    id = Column(String(32), primary_key=True, default=_uuid)
    project_ref = Column(String(32), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    endpoint = Column(String(1000), nullable=False)
    model_name = Column(String(200), nullable=False)
    api_key_ciphertext = Column(Text, nullable=False)
    key_fingerprint = Column(String(64), nullable=False)
    ssrf_validated_at = Column(DateTime, nullable=True)
    active = Column(Boolean, nullable=False, default=True,
                    server_default=text("1"))
    created_at = Column(DateTime, nullable=False, default=utc_now)

    # target.sessions -> list[Session]; session.target -> Target
    sessions = relationship(
        "Session", back_populates="target", cascade="all, delete-orphan"
    )


class Session(db.Model):
    """One annotator red-team conversation against a Target.

    Linked back to the Flask Submission via ``submission_ref`` (which
    is the string id of the public.submissions row that this chat
    concludes into, set at Phase 5/6). Annotator and project refs are
    stored as strings for the same reason (decoupled from Flask's
    integer keys, at the schema level).
    """

    __tablename__ = "sessions"
    __table_args__ = (
        # submission_ref is UNIQUE (a submission maps to at most one
        # RedLab session and vice-versa). NULL until Phase 6 claim hook.
        UniqueConstraint(
            "submission_ref", name="uq_sessions_submission_ref"
        ),
        CheckConstraint(
            "status IN ('open', 'concluded')",
            name="ck_sessions_status",
        ),
        CheckConstraint(
            "turn_count >= 0", name="ck_sessions_turn_count_nonneg"
        ),
        CheckConstraint(
            "breach_count >= 0", name="ck_sessions_breach_count_nonneg"
        ),
    ) + (({"schema": REDLAB_SCHEMA},) if REDLAB_SCHEMA else ())

    id = Column(String(32), primary_key=True, default=_uuid)
    submission_ref = Column(String(32), nullable=True, unique=False)
    annotator_ref = Column(String(32), nullable=False, index=True)
    project_ref = Column(String(32), nullable=False, index=True)
    target_id = Column(
        String(32),
        _fk("targets"),
        nullable=False,
        index=True,
    )
    status = Column(
        String(20), nullable=False, default="open", server_default="'open'"
    )
    turn_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    breach_count = Column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    conclude_note = Column(Text, nullable=True)
    concluded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)

    target = relationship("Target", back_populates="sessions")
    messages = relationship(
        "Message",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.turn_index",
    )


class Message(db.Model):
    """A single chat turn within a Session."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "turn_index", name="uq_messages_session_turn"
        ),
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_messages_role",
        ),
        CheckConstraint(
            "turn_index >= 0", name="ck_messages_turn_index_nonneg"
        ),
    ) + (({"schema": REDLAB_SCHEMA},) if REDLAB_SCHEMA else ())

    id = Column(String(32), primary_key=True, default=_uuid)
    session_id = Column(
        String(32),
        _fk("sessions", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_index = Column(Integer, nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    # Idempotency key supplied by the client for user turns (so a retry
    # of POST /turn with the same client_turn_id does not double-write).
    # NULL for assistant turns (we write them ourselves).
    client_turn_id = Column(String(64), nullable=True, index=True)
    is_unsafe_flag = Column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    redacted = Column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at = Column(DateTime, nullable=False, default=utc_now)

    session = relationship("Session", back_populates="messages")


class ProcessedEvent(db.Model):
    """Event-dedup table.

    The in-process event dispatcher (:mod:`redlab.events`, Phase 5)
    inserts a row here per processed event_id so that a redelivered
    event (webhook retry, double-send, worker restart) is a no-op.
    event_id is the UUID primary key directly — presence means
    "handled".
    """

    __tablename__ = "processed_events"
    __table_args__ = (
        {"schema": REDLAB_SCHEMA} if REDLAB_SCHEMA else {}
    )

    event_id = Column(String(32), primary_key=True)
    processed_at = Column(DateTime, nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Additive columns on public.submissions / public.projects live directly
# on the Submission / Project classes in models.py (added there in Phase
# 2 so SQLAlchemy metadata includes them for create_all in local dev).
# The Alembic migration #18 is the production authority for adding them.
# ---------------------------------------------------------------------------


def ensure_redlab_schema(conn) -> None:
    """Create the ``redlab`` Postgres schema if it's missing.

    Called by the Phase 2 test before ``create_all()``. The Alembic
    migration #18 is the authority for this in production (which runs
    on Postgres). SQLite has no schema concept — RedLab tables live
    in the default schema there — so this is a no-op on non-Postgres
    backends.
    """
    from sqlalchemy.engine import Engine
    if isinstance(conn, Engine):
        conn = conn.connect()
        close = True
    else:
        close = False
    try:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS redlab"))
            conn.commit()
    finally:
        if close:
            conn.close()
