"""Client portal blueprint (Phase 7): scoped client-facing delivery.

Anonymous-by-design surface, gated per-link: an opaque 32-byte token
in the URL is the credential (only its sha256 is stored), access is
audited, the raw token is redacted from REQ access logs, and the
portal is marked noindex. Clients see progress + quality aggregates
and can download their APPROVED work as client-safe JSONL/CSV —
annotator identities, gold answers and reviewer notes never leave
the platform through this surface.
"""

import io
import csv as csv_module

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, select

import client_portal as portal
from audit import log_event
from extensions import db
from models import ClientAccessToken, Submission, Task, utc_now

client_bp = Blueprint("client", __name__)

PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=()"
)


def _load_token(raw: str):
    """Resolve a raw token to (token_row, project) or abort with 404.

    A 404 (not a login page, not a 403) is deliberate: the token IS
    the credential and its existence is not advertised.

    Args:
        raw: The raw token from the URL.

    Returns:
        The (ClientAccessToken, Project) pair for a live link.
    """
    if not raw:
        abort(404)
    row = db.session.scalar(
        select(ClientAccessToken).where(
            ClientAccessToken.token_hash == portal.hash_token(raw)
        )
    )
    if (
        row is None
        or not row.is_active
        or portal.link_expired(row.expires_at, utc_now())
    ):
        abort(404)
    return row


@client_bp.after_request
def _client_noindex(response):
    """Keep portal pages out of search indexes (belt: also X-Robots)."""
    if request.path.startswith("/client/"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@client_bp.route("/client/<token>")
def dashboard(token: str):
    """The client's project dashboard: progress, quality, activity.

    Args:
        token: The raw access token from the link.

    Returns:
        The portal page, or 404 for unknown/deactivated/expired links.
    """
    row = _load_token(token)
    project = row.project

    total_tasks = db.session.scalar(
        select(func.count(Task.id)).where(Task.project_id == project.id)
    ) or 0
    status_counts = dict(
        db.session.execute(
            select(Submission.status, func.count(Submission.id))
            .join(Task, Submission.task_id == Task.id)
            .where(Task.project_id == project.id)
            .group_by(Submission.status)
        ).all()
    )
    approved = db.session.scalar(
        select(func.count(Submission.id))
        .join(Task, Submission.task_id == Task.id)
        .where(
            Task.project_id == project.id,
            Submission.status == "approved",
        )
    ) or 0
    avg_score = db.session.scalar(
        select(func.avg(Submission.qa_score))
        .join(Task, Submission.task_id == Task.id)
        .where(
            Task.project_id == project.id,
            Submission.status == "approved",
            Submission.qa_score.is_not(None),
        )
    )
    summary = portal.progress_summary(
        status_counts, approved, total_tasks
    )
    summary["avg_qa_score"] = (
        round(float(avg_score), 1) if avg_score is not None else None
    )

    row.last_used_at = utc_now()
    row.last_used_ip = (request.remote_addr or "-")[:64]
    try:
        log_event(
            "client_portal.access",
            actor_name=f"client:{row.label}",
            entity_type="project",
            entity_id=project.id,
            detail={"token_id": row.id},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Client portal access bookkeeping failed for token %s.",
            row.id,
        )
    # The raw token is the credential: it authenticates this request
    # but is never rendered back into the page — the export links are
    # prebuilt server-side instead.
    return render_template(
        "client_portal.html",
        project=project,
        summary=summary,
        label=row.label,
        export_jsonl_url=url_for("client.export_jsonl", token=token),
        export_csv_url=url_for("client.export_csv", token=token),
    )


def _stream_delivery(token: str, as_csv: bool) -> Response:
    """Build the approved-work delivery file (shared by both formats).

    Args:
        token: The raw access token from the link.
        as_csv: True renders CSV, False renders JSONL.

    Returns:
        The attachment Response, or 404 for a dead link.
    """
    row = _load_token(token)
    project = row.project
    rows = db.session.execute(
        select(Submission, Task)
        .join(Task, Submission.task_id == Task.id)
        .where(
            Task.project_id == project.id,
            Submission.status == "approved",
        )
        .order_by(Submission.id)
    ).all()
    records = [
        portal.client_safe_record(
            task.id,
            _parsed(task.input_data),
            _parsed(submission.output_data),
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
    log_event(
        "client_portal.export",
        actor_name=f"client:{row.label}",
        entity_type="project",
        entity_id=project.id,
        detail={"format": "csv" if as_csv else "jsonl",
                "rows": len(records), "token_id": row.id},
        commit=True,
    )
    stamp = utc_now().strftime("%Y%m%d")
    if as_csv:
        buffer = io.StringIO()
        writer = csv_module.writer(buffer)
        writer.writerow(
            ["task_id", "input", "output", "qa_score",
             "submitted_at", "reviewed_at"]
        )
        for record in records:
            writer.writerow([
                record["task_id"],
                _as_text(record["input"]),
                _as_text(record["output"]),
                record["qa_score"] if record["qa_score"] is not None
                else "",
                record["submitted_at"] or "",
                record["reviewed_at"] or "",
            ])
        body = buffer.getvalue().encode("utf-8-sig")
        mimetype = "text/csv"
        ext = "csv"
    else:
        body = portal.to_jsonl(records).encode("utf-8")
        mimetype = "application/x-ndjson"
        ext = "jsonl"
    payload = Response(body, mimetype=mimetype)
    payload.headers["Content-Disposition"] = (
        f'attachment; filename="brydl-{project.id}-approved-{stamp}.{ext}"'
    )
    return payload


def _parsed(raw: str):
    """Parse stored JSON when possible, else pass the raw text."""
    import json

    try:
        return json.loads(raw or "")
    except ValueError:
        return raw


def _as_text(value) -> str:
    """Flatten a stored document to CSV-safe text (never a formula)."""
    if isinstance(value, (dict, list)):
        import json

        value = json.dumps(value, ensure_ascii=False)
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


@client_bp.route("/client/<token>/export.jsonl")
def export_jsonl(token: str):
    """Download approved work as client-safe JSONL."""
    return _stream_delivery(token, as_csv=False)


@client_bp.route("/client/<token>/export.csv")
def export_csv(token: str):
    """Download approved work as client-safe CSV."""
    return _stream_delivery(token, as_csv=True)
