"""In-process event dispatcher (Phase 5).

RedLab emits domain events over an in-process pub/sub so that Flask
side-effects (QA queue creation, submission state updates,
notifications) can run synchronously inside the same DB transaction
that produced the event, without coupling the FastAPI route layer to
Flask internals.

Why in-process?
---------------
Render free-tier runs a single web process with two gunicorn workers;
an out-of-process bus (Redis streams, Celery, Postgres LISTEN/NOTIFY)
adds ops weight without benefit. Dedup against the
``redlab.processed_events`` table guarantees that an event retried
after a connection blip is a no-op, so handlers can safely commit
side effects.

Contract for handlers
---------------------
Handlers register via :func:`register_handler(event_type, fn)` and
receive the full event envelope ``dict``. They MUST:

* Be idempotent (the same ``event_id`` may be delivered more than
  once across retries).
* Not issue their own commits; the dispatcher runs inside whatever
  Flask / SQLAlchemy session the caller set up and the caller is
  responsible for committing. The dispatcher itself records
  ``event_id`` in ``processed_events`` as part of that unit.
* Not raise — an unhandled exception in a handler is logged and
  swallowed so one bad subscriber cannot break the originating
  request (fail-open for the annotator-facing path).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)

# event_type -> list[handler]
_HANDLERS: dict[str, list[Callable[[dict], None]]] = {}


def register_handler(event_type: str, fn: Callable[[dict], None]) -> None:
    """Register ``fn`` to be called on every emit of ``event_type``.

    Safe to call multiple times (e.g. from module imports during
    tests) — duplicate registrations for the same (event_type, fn)
    are de-duplicated by identity so handlers don't fire twice.
    """
    lst = _HANDLERS.setdefault(event_type, [])
    if fn not in lst:
        lst.append(fn)


def clear_handlers() -> None:
    """Unregister every handler (test helper)."""
    _HANDLERS.clear()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit_event(event_type: str, payload: dict) -> dict:
    """Build an envelope, dedup, dispatch to subscribers, return it.

    The caller MUST be inside a Flask / SQLAlchemy app context and
    SHOULD commit after emit so the ``processed_events`` row lands
    atomically with the originating state change. If the event_id
    is already in ``processed_events`` this is a no-op (returns the
    already-dispatched envelope silently — never raises).

    Returns the event envelope ``{"event_id", "event_type",
    "payload", "created_at"}``.
    """
    envelope = {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "payload": dict(payload or {}),
        "created_at": _utc_now_iso(),
    }
    # Deferred import so importing redlab.events doesn't pull in the
    # Flask/DB stack (used by unit tests that only want the dispatcher).
    try:
        from extensions import db
        from redlab.models import ProcessedEvent
        from flask import has_app_context
    except Exception:  # noqa: BLE001 - no DB available
        db = None
        ProcessedEvent = None
        has_app_context = None

    already = False
    if db is not None and ProcessedEvent is not None and (
        has_app_context is None or has_app_context()
    ):
        existing = db.session.get(ProcessedEvent, envelope["event_id"])
        if existing is not None:
            already = True
        else:
            db.session.add(ProcessedEvent(event_id=envelope["event_id"]))

    if already:
        logger.debug("redlab event %s already processed (dedup)",
                     envelope["event_id"])
        return envelope

    for h in list(_HANDLERS.get(event_type, ())):
        try:
            h(envelope)
        except Exception:  # noqa: BLE001 - defensive: one bad subscriber
            logger.exception(
                "RedLab event handler %r failed for event %s",
                getattr(h, "__name__", repr(h)), envelope["event_id"],
            )
    return envelope


__all__ = ["emit_event", "register_handler", "clear_handlers"]
