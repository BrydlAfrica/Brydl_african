"""Flask ↔ RedLab integration bridge (Phase 5).

This is the ONLY module in :mod:`redlab` that Flask code is permitted
to import. All other RedLab modules use SQLAlchemy's declarative base
bound to the Flask ``db`` instance but should never be imported
directly from routes/jobs — doing so would couple Flask request
cycles to RedLab implementation details.

This module exposes three call sites:

* :func:`handle_redlab_event` is registered as an event subscriber in
  :func:`register_integration_handlers` (called once at startup from
  :mod:`app`). It receives envelopes emitted by
  :func:`redlab.events.emit_event`
  and performs the Flask-side writes: submission state, breach-count
  cache, QA consensus queue creation.
* :func:`get_session_transcript` returns a chronological message list
  for server-side QA rendering (used by the QA review page in Phase 6).
* :func:`link_session_to_submission` (Phase 6 helper, also used by
  tests) is a small utility that binds ``session.submission_ref`` to
  a submission id so :func:`handle_redlab_event` knows which row to
  update — Phase 5's event handlers gracefully no-op when no
  submission is linked yet.

QA queue creation
-----------------
When a session concludes, we enqueue it for two independent QA
reviewers using the existing ``consensus_groups`` + sibling ``tasks``
machinery the rest of Brydl uses for blind review:

1. A ``ConsensusGroup`` is created (``required_count=2``) and tied to
   the submission's project.
2. Two ``Task`` rows are inserted under that group with status
   ``"pending"`` (unassigned — the QA team self-claims or admins
   assign them), ``task_type`` set to a new ``"redteam_qa"`` marker
   (stored in ``input_data`` envelope so we don't need a schema
   migration for a new task type), and ``expected_output`` set to
   ``None`` (red-team review is subjective, not gold-keyed).
3. The submission's ``redteam_state`` flips to ``"qa_pending"`` so
   the annotator dashboard reflects review-in-progress.

All writes run inside the caller's transaction (the event is
dispatched from the same ``with _flask_ctx()`` block that concludes
the session, so the ``processed_events`` row, submission update,
consensus group, and QA tasks all commit atomically).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


MIN_CONCLUDE_TURNS = 3
QA_REVIEWERS_PER_SESSION = 2


def register_integration_handlers() -> None:
    """Wire event handlers. Called once from ``app.create_app``."""
    from redlab.events import register_handler
    register_handler("breach.flagged", _on_breach_flagged)
    register_handler("session.concluded", _on_session_concluded)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _on_breach_flagged(envelope: dict) -> None:
    """Re-sync the submission's cached breach count."""
    payload = envelope.get("payload") or {}
    session_id = payload.get("session_id")
    if not session_id:
        return
    sub = _lookup_submission_for_session(session_id)
    if sub is None:
        return
    sub.redteam_breach_count = int(payload.get("breach_count") or 0)
    if sub.redteam_breach_count > 0 and sub.redteam_state == "active":
        sub.redteam_state = "flagged"


def _on_session_concluded(envelope: dict) -> None:
    """Flip submission state, cache breach count, enqueue QA tasks."""
    payload = envelope.get("payload") or {}
    session_id = payload.get("session_id")
    if not session_id:
        return
    sub = _lookup_submission_for_session(session_id)
    if sub is None:
        # No submission linked yet (Phase 5-only deployments can
        # conclude sessions before the Phase 6 claim hook wires them
        # up). Nothing to update — no error, just skip.
        return

    breach_count = int(payload.get("breach_count") or 0)
    sub.redteam_breach_count = breach_count
    sub.redteam_state = "qa_pending"

    # Idempotency: if we've already built a QA consensus group for
    # this session, return without creating duplicates. We identify
    # past runs by a marker string inside the QA task's input_data.
    from models import ConsensusGroup, Task  # local: Flask-side models

    marker = _qa_input_marker(session_id)
    existing = (
        Task.query
        .filter(Task.consensus_group_id.isnot(None))
        .filter(Task.input_data.like(f"%{marker}%"))
        .first()
    )
    if existing is not None:
        return

    group = ConsensusGroup(
        project_id=_project_id_for_submission(sub),
        required_count=QA_REVIEWERS_PER_SESSION,
        status="pending",
    )
    from extensions import db
    db.session.add(group)
    db.session.flush()  # assign group.id
    transcript_summary = payload.get("transcript_excerpt") or ""
    input_payload = json.dumps({
        "_redlab_qa": True,
        "session_id": session_id,
        "submission_id": sub.id,
        "breach_count": breach_count,
        "note": payload.get("conclude_note") or "",
        "excerpt": transcript_summary[:500],
    })
    for _i in range(QA_REVIEWERS_PER_SESSION):
        db.session.add(Task(
            project_id=_project_id_for_submission(sub),
            input_data=input_payload,
            status="pending",
            consensus_group_id=group.id,
        ))


def _project_id_for_submission(sub) -> int:
    """Return the project_id for a submission, walking Task if needed."""
    pid = getattr(sub, "project_id", None)
    if pid is not None:
        return pid
    # Submission.project_id may not exist in this schema version;
    # fall back to the parent task's project.
    if getattr(sub, "task", None) is not None and sub.task is not None:
        return sub.task.project_id
    if getattr(sub, "task_id", None) is not None:
        from models import Task as _T
        tk = _T.query.get(sub.task_id)
        if tk is not None:
            return tk.project_id
    raise ValueError("submission has no project_id and cannot resolve one")


def _qa_input_marker(session_id: str) -> str:
    return f'"session_id": "{session_id}"'


def _lookup_submission_for_session(session_id: str):
    """Return the Submission row linked to ``session_id``, or None.

    The link is the UNIQUE ``submissions.redlab_session_id`` column
    (set by :func:`link_session_to_submission` / Phase 6 claim hook).
    """
    from models import Submission
    return (
        Submission.query
        .filter_by(redlab_session_id=session_id)
        .first()
    )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_session_transcript(session_id: str) -> dict[str, Any]:
    """Return session metadata + ordered messages for QA rendering.

    The returned dict is JSON-serialisable; ``messages`` is ordered
    by ``turn_index`` ascending.
    """
    from redlab.models import Message, Session

    sess = Session.query.get(session_id)
    if sess is None:
        return {"session": None, "messages": []}
    msgs = (
        Message.query
        .filter_by(session_id=session_id)
        .order_by(Message.turn_index.asc())
        .all()
    )
    return {
        "session": {
            "id": sess.id,
            "status": sess.status,
            "annotator_ref": sess.annotator_ref,
            "project_ref": sess.project_ref,
            "target_id": sess.target_id,
            "turn_count": sess.turn_count,
            "breach_count": sess.breach_count,
            "conclude_note": sess.conclude_note,
            "concluded_at": (
                sess.concluded_at.isoformat() if sess.concluded_at else None
            ),
            "created_at": _iso(sess.created_at),
        },
        "messages": [
            {
                "id": m.id,
                "turn_index": m.turn_index,
                "role": m.role,
                "content": m.content,
                "client_turn_id": m.client_turn_id,
                "is_unsafe_flag": bool(m.is_unsafe_flag),
                "created_at": _iso(m.created_at),
            }
            for m in msgs
        ],
    }


def link_session_to_submission(session_id: str, submission_id: int) -> None:
    """Bind ``submissions.redlab_session_id`` → this session.

    Called by Phase 6's claim flow (and by tests) so event handlers
    know which submission to update. Sets the submission to
    ``redteam_state='active'`` as a side-effect. Idempotent: if the
    column is already set to a different session, raises ValueError.
    """
    from extensions import db
    from models import Submission
    from redlab.models import Session

    sess = Session.query.get(session_id)
    if sess is None:
        raise ValueError(f"no redlab session {session_id}")
    sub = Submission.query.get(submission_id)
    if sub is None:
        raise ValueError(f"no submission {submission_id}")
    if sub.redlab_session_id and sub.redlab_session_id != session_id:
        raise ValueError(
            f"submission {submission_id} already linked to "
            f"session {sub.redlab_session_id}"
        )
    # UNIQUE constraint on the column prevents two submissions
    # sharing a session; SQLAlchemy will raise on commit if violated.
    sub.redlab_session_id = session_id
    if sub.redteam_state in (None, "", "none"):
        sub.redteam_state = "active"
    sess.submission_ref = str(submission_id)
    db.session.commit()


__all__ = [
    "MIN_CONCLUDE_TURNS", "QA_REVIEWERS_PER_SESSION",
    "register_integration_handlers", "get_session_transcript",
    "link_session_to_submission",
]
