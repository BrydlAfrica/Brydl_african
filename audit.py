"""Observability layer: structured logging and the database audit trail.

Every significant action in Brydl leaves TWO coordinated records:

1. A stdout log line (Render's log viewer picks these up) carrying a
   per-request correlation id. ``REQ ...`` access lines, ``AUDIT ...``
   action lines and exception tracebacks all share the same
   ``[request_id]``, so one id reconstructs the full story of a request:
   who called it, what it changed, what failed and how long it took.

2. An immutable ``audit_logs`` table row — queryable by admins at
   /admin/audit — recording WHO did WHAT to WHICH entity, WHEN, from
   WHICH IP, with a small JSON detail payload. Rows are append-only:
   nothing in the app ever updates or deletes them.

Design rules:
- ``log_event()`` must never break the request it witnesses. The stdout
  line is written FIRST (the trail survives a total database failure);
  the database insert is wrapped defensively and, when called inside the
  actor's transaction (``commit=False``), deliberately does NOT roll
  back on failure — the caller's own commit/rollback remains in charge.
- ``commit=True`` is for standalone audit writes on paths that change
  nothing else (failed sign-ins, logouts): the event is committed in its
  own transaction.
"""

import contextvars
import json
import logging
import os
import sys
import uuid

from flask import current_app, has_request_context, request

from extensions import db
from models import AuditLog

# Correlation id for the request currently being served in this context
# ("-" outside a request, e.g. during CLI commands).
_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def get_request_id() -> str:
    """Return the current request's correlation id.

    Returns:
        The 12-hex-character id, or "-" when no request is in flight.
    """
    return _REQUEST_ID.get()


def new_request_id() -> "contextvars.Token[str]":
    """Set a fresh correlation id for the current request context.

    Returns:
        The contextvars token needed to restore the previous value in
        teardown_request (context reuse safety between requests).
    """
    return _REQUEST_ID.set(uuid.uuid4().hex[:12])


def reset_request_id(token: "contextvars.Token[str]") -> None:
    """Restore the previous correlation id (request teardown).

    Args:
        token: The token returned by new_request_id().
    """
    _REQUEST_ID.reset(token)


class _RequestIdFilter(logging.Filter):
    """Inject the current request id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Attach record.request_id if the formatter needs it.

        Args:
            record: The log record being emitted.

        Returns:
            Always True (the record is emitted).
        """
        if not hasattr(record, "request_id"):
            record.request_id = _REQUEST_ID.get()
        return True


def setup_logging(app) -> None:
    """Configure the app logger with a correlated, single-line format.

    Idempotent: create_app() may run several times in one process (the
    test suites do exactly that), so handlers are replaced, never
    stacked. Output goes to stdout — Render's log drain captures it.

    Args:
        app: The Flask application whose logger to configure.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: "
            "%(message)s"
        )
    )
    handler.addFilter(_RequestIdFilter())

    logger = app.logger
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False

    # SQLAlchemy statement echoes would drown the signal at INFO; the
    # ORM's errors still surface through their own logger.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def log_event(
    action: str,
    *,
    actor=None,
    actor_name: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    detail: dict | None = None,
    commit: bool = False,
) -> None:
    """Record one auditable action: stdout line + audit_logs row.

    Safe to call anywhere: with an app context (normal request handling)
    and without one (CLI commands). Never raises.

    Args:
        action: Dotted action name, e.g. "review.verdict", "task.upload".
        actor: The acting User instance, or None for anonymous/system.
        actor_name: Overrides the stored actor label (e.g. the email an
            anonymous caller typed on the login form).
        entity_type: Optional target kind ("task", "payment", ...).
        entity_id: Optional target primary key.
        detail: Optional small JSON-serialisable payload.
        commit: True for standalone events (failed logins, logouts) that
            have no other transaction to join. Leave False when called
            inside the caller's transaction so the audit row commits —
            or rolls back — atomically WITH the action it describes.
    """
    # --- 1. The stdout line always happens ------------------------------
    ip = ""
    user_agent = ""
    if has_request_context():
        ip = request.remote_addr or ""
        user_agent = (request.headers.get("User-Agent") or "")[:200]
    actor_label = actor_name or (
        getattr(actor, "name", None) or "anonymous"
    )
    try:
        current_app.logger.info(
            "AUDIT %s actor=%s entity=%s#%s ip=%s detail=%s",
            action,
            actor_label,
            entity_type or "-",
            entity_id if entity_id is not None else "-",
            ip or "-",
            json.dumps(detail, ensure_ascii=False, default=str)
            if detail
            else "-",
        )
    except Exception:
        pass  # even logging must never take the request down

    # --- 2. The database row, best-effort --------------------------------
    try:
        entry = AuditLog(
            user_id=getattr(actor, "id", None),
            actor_name=actor_label[:120] or "system",
            action=action[:100],
            entity_type=entity_type[:50] if entity_type else None,
            entity_id=entity_id,
            detail=(
                json.dumps(detail, ensure_ascii=False, default=str)[:2000]
                if detail
                else None
            ),
            ip_address=ip or None,
            user_agent=user_agent or None,
        )
        db.session.add(entry)
        if commit:
            db.session.commit()
    except Exception:
        if commit:
            db.session.rollback()
        # Inside a caller's transaction we deliberately do NOT roll back:
        # the caller's exception path stays in charge of its transaction.
        try:
            current_app.logger.exception(
                "Audit row write failed for action %s.", action
            )
        except Exception:
            pass
