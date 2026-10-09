"""RedLab FastAPI sub-application.

Mounted under ``/redlab`` by :mod:`asgi` (the root ASGI entry point).
Phase 1 scope is minimal:

* A ``GET /healthz`` endpoint that returns 200 with a static JSON body
  (used by Render's HTTP health-check — it MUST succeed even when the
  feature flag is OFF, so the platform keeps serving the Flask app while
  we ship RedLab code dark).
* A JSON error-boundary middleware that converts any unhandled exception
  into a ``{"error": "internal_error", "request_id": ...}`` 500 response
  instead of leaking a traceback or an HTML error page (RedLab is a
  machine-facing API; clients expect JSON at all times).
* A dependency-gated ``/v1/*`` sub-router: when ``REDLAB_ENABLED`` is
  false every sub-route returns 404 ``{"error": "redlab_disabled"}``;
  when true later phases mount their routers (sessions, messages, …)
  onto ``v1_router``.

The sub-app is built lazily via :func:`create_redlab_app` so tests can
construct it repeatedly with different flag states; :data:`app` is the
module-level instance used by ``asgi:app``.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from redlab.auth import (
    RedLabAuthError, _flask_app, get_current_annotator, verify_csrf_header,
)
from redlab.connector import (
    ERR_BAD_RESPONSE, ERR_CLIENT_AUTH, ERR_ENDPOINT_BLOCKED,
    ERR_RATE_LIMITED, ERR_UNREACHABLE, build_history, call_client_model,
)
from redlab.events import emit_event
from redlab.flags import is_redlab_enabled
from redlab.schemas import (
    ConcludeRequest, ErrorResponse, FlagRequest, MessageOut, SessionOut,
    TurnRequest, TurnResponse,
)


def utc_now() -> datetime:
    """UTC timestamp without tzinfo (matches redlab.models.utc_now)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


logger = logging.getLogger(__name__)

# Default body for the disabled-v1 namespace — single source of truth so
# both the explicit 404 router and any future gate can return identical
# JSON.
DISABLED_BODY: dict[str, str] = {"error": "redlab_disabled"}


def _request_id_from(request: Request) -> str:
    """Extract a correlation id for JSON error responses."""
    existing = request.headers.get("x-request-id")
    if existing:
        return existing
    return uuid.uuid4().hex[:12]


async def _json_error_boundary(
    request: Request, call_next: Any,
) -> JSONResponse:
    """Catch-all error middleware returning a JSON 500 on any exception."""
    started = time.perf_counter()
    try:
        response = await call_next(request)
        rid = (response.headers.get("X-Request-ID")
               or _request_id_from(request))
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:  # noqa: BLE001
        rid = _request_id_from(request)
        logger.exception(
            "RedLab unhandled error rid=%s path=%s dur=%dms",
            rid, request.url.path,
            int((time.perf_counter() - started) * 1000),
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "request_id": rid},
            headers={"X-Request-ID": rid},
        )


@contextmanager
def _flask_ctx() -> Iterator[None]:
    """Run the wrapped block inside a Flask app context.

    Re-entrant: if a context is already active the call is a no-op so
    code under this helper works both from FastAPI worker threads and
    from Flask-side test setups.
    """
    from flask import has_app_context
    app = _flask_app()
    ctx = None
    if not has_app_context():
        ctx = app.app_context()
        ctx.push()
    try:
        yield
    finally:
        if ctx is not None:
            ctx.pop()


def create_redlab_app() -> FastAPI:
    """Construct the RedLab FastAPI sub-app."""
    app = FastAPI(
        title="Brydl RedLab",
        version="14.0.0",
        description=(
            "Dynamic Red-Teaming as a Service — African-language "
            "adversarial evaluation of LLMs via Brydl's annotator pool."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.middleware("http")(_json_error_boundary)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "redlab",
            "version": "14.0.0",
            "redlab_enabled": is_redlab_enabled(),
        }

    # /v1 namespace — gated.
    v1_router = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    v1_router.middleware("http")(_json_error_boundary)

    @v1_router.exception_handler(RedLabAuthError)
    async def _auth_error_handler(request: Request, exc: RedLabAuthError):
        body = dict(exc.detail) if isinstance(exc.detail, dict) else {
            "error": "auth_error", "detail": str(exc.detail),
        }
        body.setdefault("request_id", _request_id_from(request))
        return JSONResponse(status_code=exc.status_code, content=body)

    @v1_router.exception_handler(HTTPException)
    async def _http_error_handler(request: Request, exc: HTTPException):
        rid = _request_id_from(request)
        detail = exc.detail
        if isinstance(detail, dict):
            body = dict(detail)
        else:
            body = {"error": "http_error", "detail": str(detail)}
        body.setdefault("request_id", rid)
        return JSONResponse(status_code=exc.status_code, content=body)

    _CONNECTOR_ERR_STATUS = {
        ERR_ENDPOINT_BLOCKED: (502, "endpoint_blocked"),
        ERR_CLIENT_AUTH:     (502, "client_auth_failed"),
        ERR_RATE_LIMITED:    (503, "client_rate_limited"),
        ERR_UNREACHABLE:     (502, "client_unreachable"),
        ERR_BAD_RESPONSE:    (502, "client_bad_response"),
    }

    MAX_TURNS = 40

    # ---- Turn endpoint -----------------------------------------------
    turn_router = APIRouter()

    @turn_router.post(
        "/sessions/{sid}/turn",
        response_model=TurnResponse,
        responses={400: {"model": ErrorResponse},
                   401: {"model": ErrorResponse},
                   403: {"model": ErrorResponse},
                   404: {"model": ErrorResponse},
                   502: {"model": ErrorResponse},
                   503: {"model": ErrorResponse}},
    )
    async def post_turn(
        sid: str,
        payload: TurnRequest,
        request: Request,
        current_user=Depends(get_current_annotator),
        _csrf_ok: None = Depends(verify_csrf_header),
    ):
        """Submit one user turn and (atomically) receive the assistant reply.

        Semantics:
          * Session must be open and owned by the authenticated annotator.
          * Caps: ≤40 turns per session, ≤4000 chars per message,
            ≤64 chars for client_turn_id (Pydantic enforces the rest).
          * Idempotent on ``client_turn_id``: retries return the cached
            pair without re-calling the client model.
          * On connector failure, writes ZERO rows and returns 5xx.
          * On success, writes user + assistant messages in one commit
            and bumps session.turn_count.
        """
        # Phase 1: read + verify session inside a Flask app context,
        # then release the DB transaction before the await. All DB
        # access is sync (SQLAlchemy 1.x style), so we do it straight
        # through — FastAPI dispatches to a worker thread when the
        # route calls sync code, and `_flask_ctx()` guarantees the
        # Flask app context is present.
        with _flask_ctx():
            from extensions import db
            from redlab.models import Message, Session

            session = db.session.get(Session, sid)
            if session is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "session_not_found",
                            "detail": "no session with that id"},
                )
            if str(session.annotator_ref or "") != str(current_user.id):
                db.session.close()
                raise HTTPException(
                    status_code=403,
                    detail={"error": "session_not_yours",
                            "detail": "session belongs to another user"},
                )
            if session.status != "open":
                db.session.close()
                raise HTTPException(
                    status_code=400,
                    detail={"error": "session_concluded",
                            "detail": "session is already concluded"},
                )

            # Idempotency early-out: if this client_turn_id was already
            # processed, return the cached pair. User messages live at
            # turn_index = 2*pair_idx and assistants at 2*pair_idx+1.
            cached_user = (
                db.session.query(Message)
                .filter(Message.session_id == sid,
                        Message.role == "user",
                        Message.client_turn_id == payload.client_turn_id)
                .first()
            )
            if cached_user is not None:
                cached_assistant = (
                    db.session.query(Message)
                    .filter(Message.session_id == sid,
                            Message.role == "assistant",
                            Message.turn_index == cached_user.turn_index + 1)
                    .first()
                )
                sess_snapshot = SessionOut.model_validate(session)
                user_snapshot = MessageOut.model_validate(cached_user)
                ast_snapshot = MessageOut.model_validate(
                    cached_assistant if cached_assistant is not None
                    else cached_user
                )
                db.session.close()
                return TurnResponse(
                    user_message=user_snapshot,
                    assistant_message=ast_snapshot,
                    session=sess_snapshot,
                )

            if session.turn_count >= MAX_TURNS:
                db.session.close()
                raise HTTPException(
                    status_code=400,
                    detail={"error": "turn_cap_reached",
                            "detail": f"session limited to {MAX_TURNS} turns"},
                )

            # Build outbound history + grab the Target FK while the
            # session is still bound.
            target = session.target
            history = build_history(list(session.messages))
            history.append({"role": "user", "content": payload.content})
            next_turn_index = session.turn_count
            db.session.commit()
            db.session.close()

        # --- Outbound call (async, no DB tx held) -------------------
        ok, content_or_err, err_code = await call_client_model(target, history)

        # --- Atomic write -------------------------------------------
        with _flask_ctx():
            from extensions import db
            from redlab.models import Message, Session

            if not ok:
                db.session.rollback()
                status_code, err_json_code = _CONNECTOR_ERR_STATUS.get(
                    err_code, (502, "client_unreachable"),
                )
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "error": err_json_code,
                        "detail": content_or_err or "client call failed",
                        "request_id": _request_id_from(request),
                    },
                )

            sess = db.session.get(Session, sid)
            if sess is None:  # pragma: no cover
                db.session.rollback()
                raise HTTPException(status_code=404, detail={
                    "error": "session_not_found",
                })
            if sess.status != "open":
                db.session.rollback()
                raise HTTPException(status_code=400, detail={
                    "error": "session_concluded",
                })
            # Re-check idempotency under the lock (a concurrent retry
            # may have committed while we awaited call_client_model).
            raced = (
                db.session.query(Message)
                .filter(Message.session_id == sid,
                        Message.client_turn_id == payload.client_turn_id)
                .first()
            )
            if raced is not None:
                raced_assistant = (
                    db.session.query(Message)
                    .filter(Message.session_id == sid,
                            Message.role == "assistant",
                            Message.turn_index == raced.turn_index + 1)
                    .first()
                )
                db.session.commit()
                return TurnResponse(
                    user_message=MessageOut.model_validate(raced),
                    assistant_message=MessageOut.model_validate(
                        raced_assistant if raced_assistant is not None
                        else raced
                    ),
                    session=SessionOut.model_validate(sess),
                )
            if sess.turn_count != next_turn_index:
                # Turn count advanced while we were awaiting — another
                # concurrent turn won the race; treat as cap / order
                # error and refuse to double-write.
                db.session.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={"error": "concurrent_turn",
                            "detail": "another turn committed concurrently; "
                                      "retry with a new client_turn_id"},
                )

            # turn_index is a monotonically-increasing per-message index:
            # pair 0 → user at 0, assistant at 1; pair 1 → user at 2,
            # assistant at 3; etc. session.turn_count is the count of
            # completed PAIRS, so pair N starts at message index 2*N.
            base_idx = next_turn_index * 2
            user_msg = Message(
                session_id=sess.id,
                turn_index=base_idx,
                role="user",
                content=payload.content,
                client_turn_id=payload.client_turn_id,
            )
            assistant_msg = Message(
                session_id=sess.id,
                turn_index=base_idx + 1,
                role="assistant",
                content=content_or_err,
                client_turn_id=None,
            )
            db.session.add(user_msg)
            db.session.add(assistant_msg)
            sess.turn_count = next_turn_index + 1
            try:
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                raise
            db.session.refresh(user_msg)
            db.session.refresh(assistant_msg)
            fresh_session = db.session.get(Session, sess.id)
            db.session.close()
            return TurnResponse(
                user_message=MessageOut.model_validate(user_msg),
                assistant_message=MessageOut.model_validate(assistant_msg),
                session=SessionOut.model_validate(fresh_session
                                                  if fresh_session else sess),
            )

    # ---- Message flag endpoint ---------------------------------------
    msg_router = APIRouter()

    @msg_router.post(
        "/messages/{mid}/flag",
        responses={400: {"model": ErrorResponse},
                   401: {"model": ErrorResponse},
                   403: {"model": ErrorResponse},
                   404: {"model": ErrorResponse}},
    )
    async def post_flag(
        mid: str,
        payload: FlagRequest,
        request: Request,
        current_user=Depends(get_current_annotator),
        _csrf_ok: None = Depends(verify_csrf_header),
    ):
        """Toggle ``is_unsafe_flag`` on an assistant message.

        Recomputes ``session.breach_count`` (count of assistant rows in
        the session with ``is_unsafe_flag = TRUE``) and emits a
        ``breach.flagged`` event so the Flask-side cache on the
        submission is updated.
        """
        with _flask_ctx():
            from extensions import db
            from redlab.models import Message, Session

            msg = db.session.get(Message, mid)
            if msg is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "message_not_found",
                            "detail": "no message with that id"},
                )
            sess = db.session.get(Session, msg.session_id)
            if sess is None:  # pragma: no cover - FK integrity
                db.session.rollback()
                raise HTTPException(
                    status_code=404,
                    detail={"error": "session_not_found"},
                )
            if str(sess.annotator_ref or "") != str(current_user.id):
                db.session.close()
                raise HTTPException(
                    status_code=403,
                    detail={"error": "session_not_yours"},
                )
            if msg.role != "assistant":
                db.session.close()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "flag_assistant_only",
                        "detail": "only assistant messages may be flagged",
                    },
                )
            if sess.status != "open":
                db.session.close()
                raise HTTPException(
                    status_code=400,
                    detail={"error": "session_concluded",
                            "detail": "session is already concluded"},
                )

            msg.is_unsafe_flag = bool(payload.is_unsafe)
            # Recompute breach count across this session.
            breach_count = (
                db.session.query(Message)
                .filter(Message.session_id == sess.id,
                        Message.role == "assistant",
                        Message.is_unsafe_flag.is_(True))
                .count()
            )
            # Include the just-toggled row (it's already flushed by
            # the SET, but count() above may not reflect unflushed
            # changes on every backend — adjust manually to be safe).
            currently_set = bool(msg.is_unsafe_flag)
            # Re-run count against the session to pick the new row up.
            db.session.flush()
            breach_count = (
                db.session.query(Message)
                .filter(Message.session_id == sess.id,
                        Message.role == "assistant",
                        Message.is_unsafe_flag.is_(True))
                .count()
            )
            sess.breach_count = breach_count
            db.session.commit()

            emit_event("breach.flagged", {
                "session_id": sess.id,
                "message_id": msg.id,
                "is_unsafe": currently_set,
                "breach_count": breach_count,
                "annotator_id": current_user.id,
            })
            db.session.commit()

            db.session.refresh(msg)
            fresh_session = db.session.get(Session, sess.id)
            db.session.close()
            sess_for_out = fresh_session if fresh_session else sess
            return {
                "message": MessageOut.model_validate(msg).model_dump(),
                "session": SessionOut.model_validate(
                    sess_for_out,
                ).model_dump(),
            }

    # ---- Session conclude endpoint -----------------------------------
    session_router = APIRouter()

    @session_router.post(
        "/sessions/{sid}/conclude",
        responses={400: {"model": ErrorResponse},
                   401: {"model": ErrorResponse},
                   403: {"model": ErrorResponse},
                   404: {"model": ErrorResponse}},
    )
    async def post_conclude(
        sid: str,
        payload: ConcludeRequest,
        request: Request,
        current_user=Depends(get_current_annotator),
        _csrf_ok: None = Depends(verify_csrf_header),
    ):
        """Conclude a session. Minimum 3 turns; idempotent."""
        from redlab.integration import MIN_CONCLUDE_TURNS
        with _flask_ctx():
            from extensions import db
            from redlab.models import Message, Session

            sess = db.session.get(Session, sid)
            if sess is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "session_not_found"},
                )
            if str(sess.annotator_ref or "") != str(current_user.id):
                db.session.close()
                raise HTTPException(
                    status_code=403,
                    detail={"error": "session_not_yours"},
                )
            if sess.status == "concluded":
                # Idempotent: return current snapshot.
                db.session.close()
                return {
                    "session": SessionOut.model_validate(sess).model_dump(),
                }
            if sess.turn_count < MIN_CONCLUDE_TURNS:
                db.session.close()
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "min_turns_required",
                        "detail": (
                            f"at least {MIN_CONCLUDE_TURNS} turns required"
                        ),
                    },
                )

            note = (payload.note or "").strip() or None
            sess.status = "concluded"
            sess.concluded_at = utc_now()
            sess.conclude_note = note

            # Build a short transcript excerpt to put in the event
            # payload (QA queue record) — last assistant + user pair.
            last_msgs = (
                db.session.query(Message)
                .filter(Message.session_id == sess.id)
                .order_by(Message.turn_index.desc())
                .limit(2)
                .all()
            )
            excerpt_parts = []
            for m in reversed(last_msgs):
                excerpt_parts.append(f"{m.role}: {m.content[:200]}")
            excerpt = "\n".join(excerpt_parts)

            db.session.flush()
            emit_event("session.concluded", {
                "session_id": sess.id,
                "breach_count": sess.breach_count,
                "turn_count": sess.turn_count,
                "conclude_note": note,
                "transcript_excerpt": excerpt,
                "annotator_id": current_user.id,
            })
            db.session.commit()

            fresh = db.session.get(Session, sess.id)
            db.session.close()
            sess_for_out = fresh if fresh else sess
            return {
                "session": SessionOut.model_validate(sess_for_out).model_dump()
            }

    def _mount_v1_routes() -> None:
        if not is_redlab_enabled():
            return
        v1_router.include_router(turn_router)
        v1_router.include_router(msg_router)
        v1_router.include_router(session_router)

    _mount_v1_routes()

    @v1_router.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def v1_gate(full_path: str) -> JSONResponse:
        if is_redlab_enabled():
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "path": full_path},
            )
        return JSONResponse(status_code=404, content=DISABLED_BODY)

    app.mount("/v1", v1_router)
    return app


# Module-level singleton. Imported by asgi:app at startup.
app: FastAPI = create_redlab_app()
