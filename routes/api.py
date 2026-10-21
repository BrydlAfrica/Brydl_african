"""Machine API, v1 (Phase 10): Bearer-key auth, project-scoped.

A client lab integrates against a project it has commissioned: pull
progress, pull approved deliverable rows (the exact client-safe
contract the portal export uses), and push new work in.

Trust model: the raw key lives ONLY in the Authorization header
(Bearer). No cookie, no session, no CSRF surface — a browser cannot
forge a cross-origin request carrying a custom header without a
CORS grant this API never gives. Keys are project-scoped: every
handler re-checks the requested project against the key, and a
foreign or unknown project is the same 404 (no existence oracle).
The key's sha256 is stored (issue-side show-once), the raw key is
never logged (the access log carries paths only), and every mutating
call is audited with counts only.
"""
import json
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    request,
    Response,
)

import kvstore
import quizzes  # noqa: F401  (payload-shape reuse for future kinds)
from client_portal import client_safe_record, progress_summary
from extensions import db
from models import ApiToken, Project, Submission, Task
from sqlalchemy import func, select

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")

KEY_PREFIX = "brydl_"
# Results page size bounds (offset-based pagination like the app).
MAX_PAGE = 500


def _json_error(status: int, code: str, message: str):
    """One uniform JSON error body (never leaks internals)."""
    response = jsonify(
        {"error": {"code": code, "message": message}}
    )
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _bearer_raw_key() -> str:
    """Extract the raw key from the Authorization header, if any."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return ""
    return header[len("Bearer "):].strip()


def _rate_limit_hit(token_row: ApiToken) -> bool:
    """True when this request exceeds the key's minute window.

    Args:
        token_row: The authenticated key row.

    Returns:
        False (allowed) or True (over the limit).
    """
    limit = int(
        current_app.config.get("API_RATE_LIMIT_PER_MINUTE", 60)
    )
    window = 60
    import time as _time

    bucket = int(_time.time()) // window
    name = f"rl:api:{token_row.id}:{bucket}"
    try:
        count = kvstore.bump_window(name, window)
    except Exception:
        # A counter failure must never take the API down.
        return False
    return count > limit


def api_auth(view):
    """Require a valid, unrevoked Bearer key on this endpoint.

    Serves 401 (missing/unknown/revoked key), 429 (rate limited) or
    the view with g.api_token set. Usage accounting is throttled to
    one write per key per minute so a chatty client cannot turn the
    accounting UPDATE into the hot path.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        from datetime import timedelta

        raw = _bearer_raw_key()
        if not raw:
            return _json_error(
                401, "missing_key",
                "Send your project API key as"
                " 'Authorization: Bearer <key>'.",
            )
        from client_portal import hash_token

        token_row = db.session.scalar(
            select(ApiToken).where(ApiToken.key_hash == hash_token(raw))
        )
        if token_row is None or not token_row.is_active():
            return _json_error(
                401, "invalid_key",
                "Unknown, revoked or inactive API key.",
            )
        if _rate_limit_hit(token_row):
            return _json_error(
                429, "rate_limited",
                "Rate limit exceeded"
                f" ({current_app.config.get('API_RATE_LIMIT_PER_MINUTE', 60)}"
                " requests/minute). Retry shortly.",
            )
        g.api_token = token_row
        # Throttled usage accounting (once per key per minute).
        from models import utc_now

        try:
            if (token_row.last_used_at is None
                    or token_row.last_used_at
                    < utc_now() - timedelta(seconds=60)):
                token_row.last_used_at = utc_now()
                token_row.request_count = (
                    (token_row.request_count or 0) + 1
                )
                db.session.commit()
        except Exception:
            db.session.rollback()
        return view(*args, **kwargs)
    return wrapped


def _scoped_project_or_none(project_id: int) -> Project | None:
    """The key's own project, or None (rendered as a uniform 404).

    Args:
        project_id: The project the request addresses.

    Returns:
        The active token's project row when it matches, else None.
    """
    token_row = getattr(g, "api_token", None)
    if token_row is None or token_row.project_id != project_id:
        return None
    return db.session.get(Project, project_id)


@api_bp.get("/ping")
def ping():
    """Uptime probe (unauthenticated, no information disclosed)."""
    return jsonify({"status": "ok", "service": "brydl-api", "v": 1})


@api_bp.get("/me")
@api_auth
def whoami():
    """Key identity: label, project binding, effective rate limit."""
    token_row = g.api_token
    project = db.session.get(Project, token_row.project_id)
    return jsonify({
        "key": token_row.key_prefix + "…",
        "label": token_row.label,
        "project": {
            "id": project.id,
            "name": project.name,
            "task_type": project.task_type,
            "status": project.status,
        },
        "rate_limit_per_minute": current_app.config.get(
            "API_RATE_LIMIT_PER_MINUTE", 60
        ),
    })


@api_bp.get("/projects/<int:project_id>")
@api_auth
def project_status(project_id: int):
    """Progress summary for the key's own project (portal math)."""
    project = _scoped_project_or_none(project_id)
    if project is None:
        return _json_error(404, "not_found", "No such project.")
    total_tasks = db.session.scalar(
        select(func.count(Task.id)).where(
            Task.project_id == project.id)
    ) or 0
    status_rows = db.session.execute(
        select(Task.status, func.count(Task.id))
        .where(Task.project_id == project.id)
        .group_by(Task.status)
    ).all()
    counts = {status: count for status, count in status_rows}
    approved = counts.get("approved", 0)
    return jsonify({
        "project": {
            "id": project.id,
            "name": project.name,
            "task_type": project.task_type,
            "status": project.status,
            "deadline": project.deadline.isoformat()
            if project.deadline else None,
        },
        "progress": progress_summary(
            counts, approved, total_tasks
        ),
        "status_counts": counts,
    })


@api_bp.get("/projects/<int:project_id>/results")
@api_auth
def project_results(project_id: int):
    """Approved, client-safe rows (portal-export contract, paginated).

    Query: ?limit= (1..500, default 100) &offset= (>=0). The row
    shape is exactly client_safe_record — no annotator identity, no
    gold markers, no reviewer notes.
    """
    project = _scoped_project_or_none(project_id)
    if project is None:
        return _json_error(404, "not_found", "No such project.")
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1),
                    MAX_PAGE)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        return _json_error(
            400, "bad_request", "limit/offset must be integers."
        )
    # The exact portal-delivery query + contract (Submission.status
    # governs; no consensus fields, no identity, no gold).
    rows = db.session.execute(
        select(Submission, Task)
        .join(Task, Submission.task_id == Task.id)
        .where(
            Task.project_id == project.id,
            Submission.status == "approved",
        )
        .order_by(Submission.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()
    records = [
        client_safe_record(
            task.id,
            _loads(task.input_data),
            _loads(submission.output_data),
            submission.qa_score,
            None,
            None,
            submission.submitted_at.isoformat()
            if submission.submitted_at else None,
            submission.reviewed_at.isoformat()
            if submission.reviewed_at else None,
        )
        for submission, task in rows
    ]
    return jsonify({
        "project_id": project.id,
        "offset": offset,
        "limit": limit,
        "count": len(records),
        "rows": records,
    })


def _loads(raw):
    """Parse a stored JSON column, degrading to the raw string."""
    try:
        return json.loads(raw or "")
    except ValueError:
        return raw or ""


class ApiSubmitError(Exception):
    """A validated request-level failure (message is client-safe)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@api_bp.post("/projects/<int:project_id>/tasks")
@api_auth
def submit_tasks(project_id: int):
    """Push a batch of tasks into the key's own project.

    Body: {"tasks": [{"input_data": str, "expected_output"?: str}]},
    at most UPLOAD_BATCH_LIMIT rows, each input 1..10000 chars.
    Only ACTIVE projects accept work. Audited (counts only).
    """
    from audit import log_event
    from config import UPLOAD_BATCH_LIMIT

    project = _scoped_project_or_none(project_id)
    if project is None:
        return _json_error(404, "not_found", "No such project.")
    if project.status != "active":
        return _json_error(
            403, "project_paused",
            "This project is not accepting tasks right now.",
        )
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(
            body.get("tasks"), list):
        raise ApiSubmitError(
            'Send JSON {"tasks": [{"input_data": ...}]}', 400
        )
    rows = body["tasks"]
    if not rows:
        raise ApiSubmitError("tasks must not be empty.", 400)
    if len(rows) > UPLOAD_BATCH_LIMIT:
        raise ApiSubmitError(
            f"Batch too large ({len(rows)}); send at most"
            f" {UPLOAD_BATCH_LIMIT} tasks per request.", 413
        )
    prepared: list[dict] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ApiSubmitError(
                f"tasks[{index}] must be an object.", 400
            )
        text = str(row.get("input_data") or "").strip()
        if not text:
            raise ApiSubmitError(
                f"tasks[{index}].input_data is required.", 400
            )
        if len(text) > 10_000:
            raise ApiSubmitError(
                f"tasks[{index}].input_data exceeds 10000"
                " characters.", 400
            )
        expected = row.get("expected_output")
        expected = (
            str(expected).strip()[:10_000] if expected else None
        )
        prepared.append({"input_data": text,
                         "expected_output": expected})
    try:
        created = [
            Task(project_id=project.id,
                 input_data=row["input_data"],
                 expected_output=row["expected_output"])
            for row in prepared
        ]
        db.session.add_all(created)
        db.session.flush()
        count = len(created)
        log_event(
            "api.tasks.submit",
            actor_name=f"api-key:{g.api_token.key_prefix}",
            entity_type="project",
            entity_id=project.id,
            detail={"created": count},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "API task submit failed for project %s.", project.id
        )
        return _json_error(
            500, "server_error",
            "Could not store the batch. Retry later.",
        )
    response = jsonify({"created": count, "project_id": project.id})
    response.status_code = 201
    return response


@api_bp.get("/projects/<int:project_id>/export")
@api_auth
def export_project_results(project_id: int):
    """Approved deliverable exports (JSONL / OpenAI / ShareGPT / CSV).

    Formats:
      - format=openai (or task_type=dynamic_red_team&format=openai):
        JSONL where each line is:
        {"messages": [{"role": "user"|"assistant", "content": "..."}],
         "metadata": {"submission_id": "...", "breach_turns": [...],
                      "model": "...", "qa_reviewers": [...],
                      "exported_at": "..."}}
      - format=sharegpt:
        JSONL where each line is:
        {"conversations": [{"from": "human"|"gpt", "value": "..."}]}
      - format=jsonl / format=csv: standard portal export format.

    Unapproved and rejected sessions are strictly excluded.
    """
    project = _scoped_project_or_none(project_id)
    if project is None:
        return _json_error(404, "not_found", "No such project.")

    export_format = request.args.get("format", "").lower()
    task_type_arg = request.args.get("task_type", "").lower()
    if not export_format:
        if (project.task_type == "dynamic_red_team"
                or task_type_arg == "dynamic_red_team"):
            export_format = "openai"
        else:
            export_format = "jsonl"

    from models import utc_now
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")

    # Fetch only approved submissions for this project
    subs = (
        db.session.query(Submission, Task)
        .join(Task, Submission.task_id == Task.id)
        .where(
            Task.project_id == project.id,
            Submission.status == "approved",
        )
        .order_by(Submission.id.asc())
        .all()
    )

    if export_format in ("openai", "sharegpt"):
        import hashlib
        import hmac
        import secrets
        from redlab.integration import get_session_export_data
        from models import AuditLog

        export_salt = secrets.token_bytes(16)
        lines = []
        for submission, task in subs:
            if not submission.redlab_session_id:
                continue
            data = get_session_export_data(submission.redlab_session_id)
            if not data or not data.get("messages"):
                continue
            msgs = data["messages"]
            model_name = data.get("model", "unknown")

            if export_format == "openai":
                # Opaque anonymized QA reviewer identifiers:
                # Deduplicate reviewer user_ids and map to per-export salted HMAC tokens
                reviewer_rows = db.session.execute(
                    select(AuditLog.user_id)
                    .where(
                        AuditLog.action == "review.verdict",
                        AuditLog.entity_type == "submission",
                        AuditLog.entity_id == submission.id,
                    )
                ).all()
                unique_uids: list[int] = []
                for r in reviewer_rows:
                    if r[0] is not None and r[0] not in unique_uids:
                        unique_uids.append(r[0])

                if unique_uids:
                    reviewers = [
                        f"reviewer-{hmac.new(export_salt, str(uid).encode('utf-8'), hashlib.sha256).hexdigest()[:8]}"
                        for uid in unique_uids
                    ]
                else:
                    reviewers = [
                        f"reviewer-{hmac.new(export_salt, b'default_qa', hashlib.sha256).hexdigest()[:8]}"
                    ]

                breach_turns = [
                    m["turn_index"] for m in msgs
                    if m["role"] == "assistant" and m["is_unsafe_flag"]
                ]
                line_obj = {
                    "messages": [
                        {"role": m["role"], "content": m["content"] or ""}
                        for m in msgs
                    ],
                    "metadata": {
                        "submission_id": str(submission.id),
                        "breach_turns": breach_turns,
                        "model": model_name,
                        "qa_reviewers": reviewers,
                        "exported_at": utc_now().isoformat(),
                    },
                }
                lines.append(json.dumps(line_obj, ensure_ascii=False))
            elif export_format == "sharegpt":
                line_obj = {
                    "conversations": [
                        {
                            "from": "human" if m["role"] == "user" else "gpt",
                            "value": m["content"] or "",
                        }
                        for m in msgs
                    ]
                }
                lines.append(json.dumps(line_obj, ensure_ascii=False))

        body = "\n".join(lines) + ("\n" if lines else "")
        resp = Response(
            body.encode("utf-8"),
            mimetype="application/x-ndjson",
        )
        fname = f"brydl-project{project.id}-{export_format}-{stamp}.jsonl"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp

    elif export_format == "csv":
        import io
        import csv as csv_module
        from client_portal import _as_text
        buffer = io.StringIO()
        writer = csv_module.writer(buffer)
        writer.writerow(
            ["task_id", "input", "output", "qa_score",
             "submitted_at", "reviewed_at"]
        )
        for submission, task in subs:
            writer.writerow([
                task.id,
                _as_text(_loads(task.input_data)),
                _as_text(_loads(submission.output_data)),
                submission.qa_score if submission.qa_score is not None else "",
                (submission.submitted_at.isoformat()
                 if submission.submitted_at else ""),
                (submission.reviewed_at.isoformat()
                 if submission.reviewed_at else ""),
            ])
        resp = Response(
            buffer.getvalue().encode("utf-8-sig"),
            mimetype="text/csv",
        )
        fname = f"brydl-project{project.id}-approved-{stamp}.csv"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp
    else:  # standard jsonl
        import client_portal as portal
        records = [
            portal.client_safe_record(
                task.id,
                _loads(task.input_data),
                _loads(submission.output_data),
                submission.qa_score,
                None,
                None,
                (submission.submitted_at.isoformat()
                 if submission.submitted_at else None),
                (submission.reviewed_at.isoformat()
                 if submission.reviewed_at else None),
            )
            for submission, task in subs
        ]
        body = portal.to_jsonl(records)
        resp = Response(
            body.encode("utf-8"),
            mimetype="application/x-ndjson",
        )
        fname = f"brydl-project{project.id}-approved-{stamp}.jsonl"
        resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return resp


@api_bp.errorhandler(ApiSubmitError)
def _handle_submit_error(exc):
    """Render validated request failures as uniform JSON errors."""
    return _json_error(exc.status, "bad_request", str(exc))
