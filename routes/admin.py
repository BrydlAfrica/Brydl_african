"""Admin blueprint: dashboard, projects, tasks, QA review, payments.

Shipped: admin dashboard (/admin/), project management (/admin/projects
CRUD + pause toggle), task management (/admin/tasks upload, filtered
list, bulk assignment, unassignment) and QA review (/admin/review queue,
detail verdict, bulk verdicts). Payments are delivered by their own
upcoming feature in this blueprint.

Access control: @admin_required (from routes/__init__.py, shared with the
annotator blueprint) wraps Flask-Login's @login_required — anonymous
visitors are redirected to sign-in; signed-in annotators get 403
Forbidden. No route here may skip it.

QA review integrity: only submissions in 'submitted' state can be
reviewed, and every verdict is a conditional UPDATE guarded on that
state — two reviewers hitting the same submission cannot both score it.
Approving sets the task to 'approved'; rejecting sets it to 'rejected'
and either leaves it with the annotator for revision (default) or
returns the task to the unowned pending pool for reassignment.

All database access goes through the SQLAlchemy ORM — no raw SQL. Every
write is wrapped in try/except with a rollback + flash on failure.
"""

import csv
import hashlib
import io
import json
import secrets
import zipfile

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

from flask_wtf import FlaskForm
from flask_wtf.csrf import ValidationError, validate_csrf
from sqlalchemy import case, delete as sa_delete, exists, func, select, update
from sqlalchemy.orm import joinedload
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    FileField,
    IntegerField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    AnyOf,
    DataRequired,
    Email,
    Length,
    NumberRange,
    Optional,
)

from audit import log_event
from notifications import notify
from config import (
    EXPORT_YIELD_PER,
    MAX_QA_NOTES_CHARS,
    MAX_TEXTAREA_CHARS,
    PAGINATION_PER_PAGE,
    IMPORT_MAX_BYTES,
    IMPORT_MAX_ROWS,
    MEDIA_MAX_BYTES,
    SFT_EXPORT_MAX_ROWS,
    SFT_MAX_TEXT_CHARS,
    UPLOAD_BATCH_LIMIT,
    USD_TO_NGN_RATE,
    MAX_RUBRIC_DIMENSIONS,
    MAX_RUBRIC_DIM_NAME,
)
import paystack as paystack_gateway
import client_portal as portal_tokens
import api_keys
import quizzes as quiz_engine
from models import ClientAccessToken, MediaAsset
from models import (
    ApiToken,

    FeedbackThread,
    ProjectAssignment,
    QualificationQuiz,
    QuizAttempt,
    SiteSetting,
)

from consensus import refresh_group
from rendering import content_view
from rubric import parse_dimensions, rubric_scores_valid
from sft import (
    extract_prompt,
    export_jsonl,
    harvest_completion,
)
from extensions import db
from models import (
    ConsensusGroup,

    AuditLog,
    SFT_STATUSES,
    TASK_STATUSES,
    TASK_TYPES,
    Payment,
    Project,
    SftExample,
    Submission,
    Task,

    User,
    utc_now,
)
from models import money
from routes import (
    admin_required,
    auditor_required,
    qa_required,
    role_required,
    usd_to_ngn,
)

admin_bp = Blueprint("admin", __name__)

# Tuning values live in config.py (env-tunable, single source of
# truth); the aliases below keep the route code readable.
UPLOAD_BATCH_LIMIT = UPLOAD_BATCH_LIMIT
# File-import caps (module mirrors, same pattern as above).
IMPORT_MAX_ROWS_MIRROR = IMPORT_MAX_ROWS
IMPORT_MAX_BYTES_MIRROR = IMPORT_MAX_BYTES
# One cap for every single-content textarea (see config.py).
MAX_INPUT_CHARS = MAX_TEXTAREA_CHARS
TASKS_PER_PAGE = PAGINATION_PER_PAGE
REVIEWS_PER_PAGE = PAGINATION_PER_PAGE
SFT_PER_PAGE = PAGINATION_PER_PAGE
MAX_QA_NOTES_CHARS = MAX_QA_NOTES_CHARS
MAX_RUBRIC_DIMS = MAX_RUBRIC_DIMENSIONS
MAX_RUBRIC_NAME = MAX_RUBRIC_DIM_NAME


class ProjectForm(FlaskForm):
    """Create/edit form for projects, validated server-side.

    task_type choices come from models.TASK_TYPES — the single source of
    truth shared with the rest of the codebase. pay_per_task must be
    strictly positive (matching the ck_projects_pay_per_task constraint).
    """

    name = StringField(
        "Project name",
        validators=[
            DataRequired(message="Project name is required."),
            Length(max=200, message="Name must be 200 characters or less."),
        ],
    )
    client_name = StringField(
        "Client name",
        validators=[
            DataRequired(message="Client name is required."),
            Length(
                max=200, message="Client name must be 200 characters or less."
            ),
        ],
    )
    task_type = SelectField(
        "Task type",
        choices=[
            (value, value.replace("_", " ").title()) for value in TASK_TYPES
        ],
        validators=[
            DataRequired(message="Choose a task type."),
        ],
    )
    pay_per_task = DecimalField(
        "Pay per task (USD)",
        places=2,
        validators=[
            DataRequired(message="Pay per task is required."),
            NumberRange(
                min=Decimal("0.01"),
                max=Decimal("10000"),
                message="Pay per task must be greater than 0.",
            ),
        ],
    )
    instructions = TextAreaField(
        "Annotator instructions",
        validators=[
            DataRequired(message="Instructions are required."),
            Length(
                max=MAX_TEXTAREA_CHARS,
                message=(
                    f"Instructions must be {MAX_TEXTAREA_CHARS:,}"
                    " characters or less."
                ),
            ),
        ],
    )
    rubric_dimensions = TextAreaField(
        "Rubric dimensions (optional, one per line)",
        validators=[
            Optional(),
            Length(
                max=MAX_TEXTAREA_CHARS,
                message=(
                    f"Rubric dimensions must be"
                    f" {MAX_TEXTAREA_CHARS} characters or less."
                ),
            ),
        ],
        description=(
            "Preference projects only: annotators rate BOTH responses"
            " on each dimension with 1-5 sliders."
        ),
    )
    required_annotations = SelectField(
        "Annotations per task (consensus)",
        choices=[
            (1, "1 — single annotation"),
            (2, "2 — blind consensus (recommended for RLHF)"),
            (3, "3 — blind consensus with strong arbitration"),
        ],
        coerce=int,
        validators=[DataRequired(message="Choose the consensus depth.")],
    )
    deadline = DateField(
        "Deadline (optional, shown to annotators)",
        validators=[Optional()],
    )
    submit = SubmitField("Save project")


class TaskUploadForm(FlaskForm):
    """Bulk task upload form (project + pasted lines).

    project choices are set per-request from the active project list in
    the view, so tampered or paused project ids fail choice validation.
    Line-level problems are reported by _parse_task_lines() in the view
    so errors can carry line numbers.
    """

    project = SelectField(
        "Project",
        coerce=int,
        validators=[DataRequired(message="Choose a project.")],
    )
    tasks_data = TextAreaField(
        "Tasks (one per line)",
        validators=[
            DataRequired(message="Paste at least one task."),
            Length(
                max=200000,
                message="Pasted data is too large; split it into"
                        " smaller batches.",
            ),
        ],
    )
    submit = SubmitField("Upload tasks")


class AssignForm(FlaskForm):
    """Annotator picker for the bulk-assign bar (choices set per view)."""

    annotator_id = SelectField(
        "Assign to",
        coerce=int,
        validators=[DataRequired(message="Choose an annotator.")],
    )
    submit = SubmitField("Assign selected")


class ReviewForm(FlaskForm):
    """Single QA verdict on one submission.

    ``action`` is submitted through the Approve/Reject buttons
    (name="action"), not a rendered dropdown; the SelectField exists to
    validate the value server-side. The score is required because the
    database refuses a final verdict without one.
    """

    action = SelectField(
        "Decision",
        choices=[("approve", "Approve"), ("reject", "Reject")],
        validators=[DataRequired(message="Approve or reject first.")],
    )
    qa_score = IntegerField(
        "QA score (0-100)",
        validators=[
            DataRequired(message="Set a QA score."),
            NumberRange(
                min=0, max=100, message="Score must be between 0 and 100."
            ),
        ],
    )
    qa_notes = TextAreaField(
        "QA notes for the annotator",
        validators=[
            Optional(),
            Length(
                max=MAX_QA_NOTES_CHARS,
                message=(f"Notes must be {MAX_QA_NOTES_CHARS} characters"
                         " or less."),
            ),
        ],
    )
    # Only meaningful when action=reject: keeps the task with the
    # annotator (revision loop) instead of stripping ownership.
    return_for_revision = BooleanField(
        "Leave the task with the annotator for revision"
    )


class BulkReviewForm(FlaskForm):
    """Bulk approve/reject for selected submissions.

    One shared score is applied to every selected submission (the DB
    requires a score per verdict); notes are intentionally left empty —
    individual feedback belongs on the detail page.
    """

    action = SelectField(
        "Decision",
        choices=[("approve", "Approve"), ("reject", "Reject")],
        validators=[DataRequired(message="Approve or reject first.")],
    )
    qa_score = IntegerField(
        "Score to apply",
        validators=[
            DataRequired(message="Set the score to apply."),
            NumberRange(
                min=0, max=100, message="Score must be between 0 and 100."
            ),
        ],
    )
    return_for_revision = BooleanField(
        "Leave rejected tasks with their annotators for revision"
    )


def _project_progress() -> dict[int, tuple[int, int]]:
    """Return {project_id: (total_tasks, approved_tasks)} in one query.

    A single grouped round-trip for the whole project list instead of two
    queries per project (no N+1).

    Returns:
        Mapping of project id to its (total, approved) task counts.
    """
    rows = db.session.execute(
        select(
            Task.project_id,
            func.count(Task.id),
            func.coalesce(
                func.sum(case((Task.status == "approved", 1), else_=0)), 0
            ),
        ).group_by(Task.project_id)
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def _parse_task_lines(
    text: str,
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Parse pasted upload data into task tuples with line-level errors.

    Format: one task per line. A line may optionally carry a gold answer
    as ``input<TAB>expected_output`` (split on the FIRST tab, so the
    expected output may itself contain tabs). Blank lines are skipped.
    A line whose text before the tab is empty is an error — trimming
    happens after the tab split so ``\\tanswer`` is caught, not silently
    accepted as a normal task.

    Args:
        text: Raw textarea contents.

    Returns:
        A tuple of (valid tasks as (input_data, expected_output|None),
        human-readable error messages with line numbers). Callers must
        treat the batch atomically: if errors is non-empty, nothing is
        inserted.
    """
    parsed: list[tuple[str, str | None]] = []
    errors: list[str] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        # Only line endings are removed here. Space/tab trimming happens
        # AFTER the tab split, so a leading-tab line ("\tanswer") is still
        # detected as an empty input instead of silently becoming text.
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        if "\t" in line:
            raw_input, _, raw_expected = line.partition("\t")
            inp = raw_input.strip()
            expected = raw_expected.strip() or None
            if not inp:
                errors.append(
                    f"Line {line_no}: task text is empty before the tab"
                    " separator."
                )
                continue
        else:
            inp = line.strip()
            expected = None
        preference = _preference_document(inp)
        gold_error = ""
        if preference is not None:
            # Preference lines must carry an a/b gold when present, and
            # are stored as canonical compact JSON.
            expected, gold_error = _preference_gold(expected)
            inp = json.dumps(preference, ensure_ascii=False)
        if len(inp) > MAX_INPUT_CHARS:
            errors.append(
                f"Line {line_no}: task text exceeds {MAX_INPUT_CHARS}"
                " characters."
            )
        elif expected and len(expected) > MAX_INPUT_CHARS:
            errors.append(
                f"Line {line_no}: expected output exceeds"
                f" {MAX_INPUT_CHARS} characters."
            )
        elif gold_error:
            errors.append(f"Line {line_no}: {gold_error}")
        else:
            parsed.append((inp, expected))
    return parsed, errors


class TaskImportForm(FlaskForm):
    """Device file import form (project + CSV file).

    The file is parsed in memory and NEVER written to disk; every row
    is validated for preview and re-validated from scratch at the
    confirm step, so a tampered preview post cannot smuggle bad rows
    into the database.
    """

    project = SelectField(
        "Project",
        coerce=int,
        validators=[DataRequired(message="Choose a project.")],
    )
    csv_file = FileField(
        "CSV file",
        validators=[
            DataRequired(message="Choose a .csv file to import."),
        ],
    )
    preview = SubmitField("Preview import")


@admin_bp.route("/", endpoint="dashboard")
@role_required("admin", "qa", "auditor")
def dashboard():
    """Render the admin dashboard: pipeline stats, review queue, payout.

    Admins get the management dashboard (pipeline + payout stats); QA
    and auditor users are redirected straight to their queues — the
    dashboard's financial statistics are admin-only.

    Statistics are non-critical by design: if the database misbehaves,
    the page still renders with zeros and an error flash instead of a
    500 — an admin must always be able to reach navigation and logout.

    Returns:
        The rendered admin dashboard template (admins) or a redirect
        to the caller's queue (qa / auditor).
    """
    if current_user.role == "qa":
        return redirect(url_for("admin.review"))
    if current_user.role == "auditor":
        return redirect(url_for("admin.consensus_conflicts"))
    stats = {
        "total_projects": 0,
        "total_tasks": 0,
        "pending_tasks": 0,
        "in_progress_tasks": 0,
        "submitted_tasks": 0,
        "approved_tasks": 0,
        "rejected_tasks": 0,
        "active_annotators": 0,
        "active_pms": 0,
        "pending_payout": money(0),
        "pending_payments_count": 0,
        "open_approvals": 0,
        "unread_feedback": 0,
        "submissions_7d": 0,
        "usd_to_ngn": None,
        "redteam_sessions_used": 0,
        "redteam_breaches": 0,
        "redteam_session_quota": 0,
    }
    review_queue = []
    recent_activity = []
    flagged = []  # projects/items needing attention

    try:
        # --- Task pipeline: one GROUP BY round-trip for all statuses ----
        counts = dict(
            db.session.execute(
                select(Task.status, func.count(Task.id)).group_by(
                    Task.status
                )
            ).all()
        )
        stats["total_tasks"] = sum(counts.values())
        stats["pending_tasks"] = counts.get("pending", 0)
        stats["in_progress_tasks"] = counts.get("in_progress", 0)
        stats["submitted_tasks"] = counts.get("submitted", 0)
        stats["approved_tasks"] = counts.get("approved", 0)
        stats["rejected_tasks"] = counts.get("rejected", 0)

        # --- Workforce and projects --------------------------------------
        stats["active_annotators"] = (
            db.session.scalar(
                select(func.count(User.id)).where(
                    User.role == "annotator", User.is_active.is_(True)
                )
            )
            or 0
        )
        stats["active_pms"] = (
            db.session.scalar(
                select(func.count(User.id)).where(
                    User.role == "project_manager", User.is_active.is_(True)
                )
            )
            or 0
        )
        stats["total_projects"] = (
            db.session.scalar(select(func.count(Project.id))) or 0
        )
        stats["open_approvals"] = (
            db.session.scalar(
                select(func.count(Project.id)).where(
                    Project.status == "pending_approval"
                )
            )
            or 0
        )
        stats["unread_feedback"] = (
            db.session.scalar(
                select(func.count(FeedbackThread.id)).where(
                    FeedbackThread.status.in_(("new", "reviewed")),
                    FeedbackThread.kind == "feedback",
                )
            )
            or 0
        )
        from datetime import timedelta as _td
        seven_days_ago = utc_now() - _td(days=7)
        stats["submissions_7d"] = (
            db.session.scalar(
                select(func.count(Submission.id)).where(
                    Submission.submitted_at >= seven_days_ago
                )
            )
            or 0
        )

        # --- Pending payout ----------------------------------------------
        per_sub = case(
            (Project.task_type == "dynamic_red_team",
             func.coalesce(Project.redteam_base_pay, 0)
             + func.coalesce(Submission.redteam_breach_count, 0)
             * func.coalesce(Project.redteam_breach_bounty, 0)),
            else_=func.coalesce(Project.pay_per_task, 0),
        )
        approved_value = money(
            db.session.scalar(
                select(func.coalesce(func.sum(per_sub), 0))
                .select_from(Submission)
                .join(Submission.task)
                .join(Task.project)
                .where(Submission.status == "approved")
            )
            or 0
        )
        paid_out = money(
            db.session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == "paid"
                )
            )
            or 0
        )
        stats["pending_payout"] = max(
            money(0), money(approved_value - paid_out)
        )
        stats["pending_payments_count"] = (
            db.session.scalar(
                select(func.count(Payment.id)).where(
                    Payment.status == "pending"
                )
            )
            or 0
        )

        # --- USD-NGN rate (display-only) ---------------------------------
        rate_row = db.session.get(SiteSetting, "usd_to_ngn_rate")
        if rate_row and rate_row.value:
            try:
                stats["usd_to_ngn"] = Decimal(str(rate_row.value))
            except (TypeError, ValueError, InvalidOperation):
                stats["usd_to_ngn"] = None

        # --- Red-team sessions, breaches & quota (Phase 8) ---------------
        try:
            from redlab.integration import get_redteam_quota_stats
            u, b, q = get_redteam_quota_stats()
            stats["redteam_sessions_used"] = u
            stats["redteam_breaches"] = b
            stats["redteam_session_quota"] = q
        except Exception:
            stats["redteam_sessions_used"] = 0
            stats["redteam_breaches"] = 0
            stats["redteam_session_quota"] = 0

        # --- Review queue -----------------------------------------------
        rows = db.session.execute(
            select(
                Submission.id,
                Submission.status,
                Submission.submitted_at,
                Task.id.label("task_id"),
                Project.name.label("project_name"),
                User.name.label("annotator_name"),
            )
            .join(Submission.task)
            .join(Task.project)
            .join(Submission.annotator)
            .where(Submission.status == "submitted")
            .order_by(Submission.submitted_at.desc(), Submission.id.desc())
            .limit(5)
        ).all()
        review_queue = [
            {
                "submission_id": row.id,
                "status": row.status,
                "task_id": row.task_id,
                "annotator": row.annotator_name,
                "project": row.project_name,
                "submitted_at": row.submitted_at,
            }
            for row in rows
        ]

        # --- Recent activity (audit log) ---------------------------------
        recent = db.session.execute(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(15)
        ).scalars().all()
        recent_activity = [
            {
                "action": r.action,
                "actor": r.actor_name or "system",
                "entity_type": r.entity_type,
                "created_at": r.created_at,
                "detail": (r.detail or {}) if isinstance(r.detail, dict) else {},
            }
            for r in recent
        ]

        # --- Flagged items (action needed) -------------------------------
        def _plural(n, singular, plural):
            return singular if n == 1 else plural
        n = stats["open_approvals"]
        if n:
            flagged.append({
                "label": f"{n} project {_plural(n, 'approval', 'approvals')} awaiting sign-off",
                "url": url_for("admin.approvals"),
                "variant": "warning",
            })
        n = stats["submitted_tasks"]
        if n:
            flagged.append({
                "label": f"{n} {_plural(n, 'submission', 'submissions')} awaiting QA",
                "url": url_for("admin.review"),
                "variant": "primary",
            })
        n = stats["pending_payments_count"]
        if n:
            flagged.append({
                "label": f"{n} pending payment {_plural(n, 'batch', 'batches')}",
                "url": url_for("admin.payments"),
                "variant": "info",
            })
        n = stats["unread_feedback"]
        if n:
            flagged.append({
                "label": f"{n} unread feedback {_plural(n, 'message', 'messages')}",
                "url": url_for("staff.feedback_inbox"),
                "variant": "secondary",
            })
        flagged = flagged[:6]
    except Exception:
        current_app.logger.exception("Admin dashboard statistics failed.")
        flash("Dashboard statistics could not be loaded.", "danger")

    return render_template(
        "admin/dashboard.html",
        stats=stats, review_queue=review_queue,
        recent_activity=recent_activity, flagged=flagged,
    )


@admin_bp.route("/dashboard")
@role_required("admin", "qa", "auditor")
def dashboard_alias():
    """Redirect the legacy /admin/dashboard path to /admin/.

    The canonical dashboard URL is /admin/ (matching the blueprint
    prefix); this alias keeps early bookmarks and links working.

    Returns:
        A redirect response to the canonical dashboard URL.
    """
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/projects")
@admin_required
def projects():
    """List every project with task counts and completion percentage.

    Progress figures come from one grouped query (see
    _project_progress), so the page costs two queries regardless of how
    many projects exist.

    Returns:
        The rendered project list template.
    """
    rows = []
    pagination = None
    try:
        progress = _project_progress()
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        pagination = db.paginate(
            select(Project).order_by(
                Project.created_at.desc(), Project.id.desc()
            ),
            page=page,
            per_page=PAGINATION_PER_PAGE,
            error_out=False,
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for("admin.projects", page=pagination.pages)
            )
        for project in pagination.items:
            total, approved = progress.get(project.id, (0, 0))
            completion = (
                round((approved / total) * 100.0, 1) if total else 0.0
            )
            rows.append(
                {
                    "project": project,
                    "total_tasks": total,
                    "completion": completion,
                }
            )
    except Exception:
        current_app.logger.exception("Project listing failed.")
        flash("Projects could not be loaded.", "danger")
    return render_template(
        "admin/projects.html", rows=rows, pagination=pagination
    )


@admin_bp.route("/projects/new", methods=["GET", "POST"])
@admin_required
def new_project():
    """Create a new project (form + server-side validated submission).

    Returns:
        The rendered form on GET or validation failure; a redirect to
        the project list on success.
    """
    form = ProjectForm()
    if form.validate_on_submit():
        rubric_dims, rubric_problems = parse_dimensions(
            form.rubric_dimensions.data,
            MAX_RUBRIC_DIMS,
            MAX_RUBRIC_NAME,
        )
        if rubric_problems:
            for problem in rubric_problems:
                flash(problem, "danger")
            return render_template(
                "admin/project_form.html",
                form=form,
                is_edit=False,
                form_action=url_for("admin.new_project"),
            )
        project = Project(
            name=form.name.data.strip(),
            client_name=form.client_name.data.strip(),
            task_type=form.task_type.data,
            # Round at the write boundary: money is 2-decimal USD.
            # Admin-created projects go straight live: PM proposals
            # move through draft→pending_approval→active instead.
            pay_per_task=money(form.pay_per_task.data),
            instructions=form.instructions.data.strip(),
            rubric_dimensions=(
                "\n".join(rubric_dims) if rubric_dims else None
            ),
            deadline=form.deadline.data,
            required_annotations=form.required_annotations.data,
            created_by=current_user.id,
            approved_by=current_user.id,
            approved_at=utc_now(),
            status="active",
        )
        try:
            db.session.add(project)
            log_event(
                "project.create",
                actor=current_user,
                entity_type="project",
                detail={
                    "name": project.name,
                    "task_type": project.task_type,
                    "pay_per_task_usd": project.pay_per_task,
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Project creation failed for %r.", project.name
            )
            flash(
                "Could not create the project. Please try again.",
                "danger",
            )
            return render_template(
                "admin/project_form.html",
                form=form,
                is_edit=False,
                form_action=url_for("admin.new_project"),
            )
        flash(f'Project "{project.name}" created.', "success")
        return redirect(url_for("admin.projects"))

    return render_template(
        "admin/project_form.html",
        form=form,
        is_edit=False,
        form_action=url_for("admin.new_project"),
    )


@admin_bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_project(project_id: int):
    """Edit an existing project's details.

    Note for the operator: changing pay_per_task applies the new rate to
    pending-payout calculations for this project's approved work (the
    template shows the same warning).

    Args:
        project_id: Primary key of the project to edit.

    Returns:
        The rendered form (GET / validation failure) or a redirect to
        the project list on success. 404 for unknown ids.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)

    rt_used = 0
    rt_breaches = 0
    if project.task_type == "dynamic_red_team":
        try:
            from redlab.integration import get_redteam_quota_stats
            rt_used, rt_breaches, _ = get_redteam_quota_stats(project.id)
        except Exception:
            rt_used, rt_breaches = 0, 0

    form = ProjectForm(obj=project)  # GET: prefill from the DB row
    if form.validate_on_submit():
        rubric_dims, rubric_problems = parse_dimensions(
            form.rubric_dimensions.data,
            MAX_RUBRIC_DIMS,
            MAX_RUBRIC_NAME,
        )
        if rubric_problems:
            for problem in rubric_problems:
                flash(problem, "danger")
            return render_template(
                "admin/project_form.html",
                form=form,
                is_edit=True,
                project=project,
                redteam_sessions_used=rt_used,
                redteam_confirmed_breaches=rt_breaches,
                form_action=url_for(
                    "admin.edit_project", project_id=project_id
                ),
            )
        project.name = form.name.data.strip()
        project.client_name = form.client_name.data.strip()
        project.task_type = form.task_type.data
        project.pay_per_task = money(form.pay_per_task.data)
        project.instructions = form.instructions.data.strip()
        project.rubric_dimensions = (
            "\n".join(rubric_dims) if rubric_dims else None
        )
        project.deadline = form.deadline.data
        project.required_annotations = form.required_annotations.data
        log_event(
            "project.update",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={
                "name": project.name,
                "task_type": project.task_type,
                "pay_per_task_usd": project.pay_per_task,
            },
        )
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Project update failed for id %s.", project_id
            )
            flash(
                "Could not save the project. Please try again.", "danger"
            )
            return render_template(
                "admin/project_form.html",
                form=form,
                is_edit=True,
                project=project,
                redteam_sessions_used=rt_used,
                redteam_confirmed_breaches=rt_breaches,
                form_action=url_for(
                    "admin.edit_project", project_id=project_id
                ),
            )
        flash(f'Project "{project.name}" updated.', "success")
        return redirect(url_for("admin.projects"))

    return render_template(
        "admin/project_form.html",
        form=form,
        is_edit=True,
        project=project,
        redteam_sessions_used=rt_used,
        redteam_confirmed_breaches=rt_breaches,
        form_action=url_for("admin.edit_project", project_id=project_id),
    )


@admin_bp.route("/projects/<int:project_id>/pause", methods=["POST"])
@admin_required
def toggle_pause(project_id: int):
    """Toggle a project between active and paused (POST + CSRF only).

    Completed projects are rejected: pausing finished work is a workflow
    error, not a state transition.

    Args:
        project_id: Primary key of the project to toggle.

    Returns:
        A redirect back to the project list with a flash message.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.projects"))

    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)

    if project.status == "completed":
        flash("Completed projects cannot be paused.", "warning")
        return redirect(url_for("admin.projects"))

    project.status = "paused" if project.status == "active" else "active"
    log_event(
        "project.pause" if project.status == "paused" else "project.resume",
        actor=current_user,
        entity_type="project",
        entity_id=project.id,
        detail={"name": project.name},
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Status toggle failed for project id %s.", project_id
        )
        flash("Could not change the project status. Please try again.",
              "danger")
        return redirect(url_for("admin.projects"))

    state = "paused" if project.status == "paused" else "resumed"
    flash(f'Project "{project.name}" {state}.', "success")
    return redirect(url_for("admin.projects"))


# ---------------------------------------------------------------------------
# Task management: upload, filtered list, bulk assign, unassign
# ---------------------------------------------------------------------------


def _parse_task_csv(text: str) -> tuple[list[dict], list[str]]:
    """Parse imported CSV text into preview rows with per-row status.

    Structure (matches the paste format one-to-one): a header row with
    a ``text`` column and an optional ``gold`` (or ``expected_output``)
    column; at most two columns. Row rules are identical to
    _parse_task_lines: blank rows skipped, task text required, task
    text and gold capped at MAX_INPUT_CHARS.

    Args:
        text: The decoded file contents.

    Returns:
        A tuple of (rows, structural_errors). Each row dict carries
        ``no`` (1-based data row number), ``text``, ``gold``, ``ok``
        and ``reason``. structural_errors is non-empty only when the
        file cannot be interpreted at all (missing/extra columns).
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = [cell.strip().lower() for cell in next(reader)]
    except StopIteration:
        return [], ["The file is empty."]

    if not header or header[0] != "text":
        return [], [
            'First column of the header row must be "text" (a gold'
            ' column is optional).'
        ]
    gold_idx = None
    if len(header) == 2:
        if header[1] not in ("gold", "expected_output"):
            return [], [
                'Second column must be named "gold" (or'
                ' "expected_output"), or be removed.'
            ]
        gold_idx = 1
    elif len(header) > 2:
        return [], [
            "Too many columns: use text plus an optional gold column"
            " only (delete any extra columns)."
        ]

    rows: list[dict] = []
    for no, cells in enumerate(reader, start=1):
        if not any(cell.strip() for cell in cells):
            continue  # fully blank line, same as the paste box
        if len(cells) > len(header):
            rows.append({
                "no": no, "text": "", "gold": None, "ok": False,
                "reason": "Too many columns in this row.",
            })
            continue
        task_text = cells[0].strip()
        gold = None
        if gold_idx is not None:
            # Rows may legitimately have fewer cells than the header
            # (a missing trailing gold cell is just "no gold").
            gold = (cells[gold_idx].strip()
                    if len(cells) > gold_idx else "") or None
        if not task_text:
            rows.append({
                "no": no, "text": "", "gold": gold, "ok": False,
                "reason": "Task text is empty.",
            })
        elif len(task_text) > MAX_INPUT_CHARS:
            rows.append({
                "no": no, "text": task_text, "gold": gold, "ok": False,
                "reason": f"Task text exceeds {MAX_INPUT_CHARS}"
                          " characters.",
            })
        elif gold and len(gold) > MAX_INPUT_CHARS:
            rows.append({
                "no": no, "text": task_text, "gold": gold, "ok": False,
                "reason": f"Expected output exceeds {MAX_INPUT_CHARS}"
                          " characters.",
            })
        else:
            preference = _preference_document(task_text)
            if preference is not None:
                gold, gold_error = _preference_gold(gold)
                if gold_error:
                    rows.append({
                        "no": no, "text": task_text, "gold": gold,
                        "ok": False, "reason": gold_error,
                    })
                    continue
                task_text = json.dumps(
                    preference, ensure_ascii=False
                )
            rows.append({
                "no": no, "text": task_text, "gold": gold, "ok": True,
                "reason": "",
            })
    if len(rows) > IMPORT_MAX_ROWS:
        return [], [
            f"Too many rows: {len(rows)}. Import at most"
            f" {IMPORT_MAX_ROWS} rows per file."
        ]
    return rows, []


def _flag_duplicate_rows(rows: list[dict], project_id: int) -> None:
    """Mark duplicate rows (in-file, or already in the project).

    Exact-text match after stripping, mirroring how admins read the
    task list. Mutates ``rows`` in place.

    Args:
        rows: Rows from _parse_task_csv (only ok rows are examined).
        project_id: The destination project.
    """
    existing = set(db.session.scalars(
        select(Task.input_data).where(Task.project_id == project_id)
    ))
    seen: set[str] = set()
    for row in rows:
        if not row["ok"]:
            continue
        if row["text"] in seen:
            row["ok"] = False
            row["reason"] = "Duplicate inside this file."
        elif row["text"] in existing:
            row["ok"] = False
            row["reason"] = "This task is already in the project."
        else:
            seen.add(row["text"])


@admin_bp.route("/tasks/import", methods=["GET", "POST"])
@admin_required
def import_tasks():
    """Import tasks from a CSV file on the admin's device (stage 1).

    The upload is validated, parsed and shown as an EDITABLE preview;
    nothing is written to the database here. The file itself is never
    saved to disk — it exists only in memory for the duration of this
    request. The confirm step (import_confirm) re-validates everything
    server-side before a single row is inserted.

    Returns:
        The preview page, or a redirect back to the tasks page with
        flash feedback when the file is rejected.
    """
    if request.method == "GET":
        return redirect(url_for("admin.tasks", import_open=1))

    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.tasks", import_open=1))

    active_projects = db.session.scalars(
        select(Project)
        .where(Project.status == "active")
        .order_by(Project.name)
    ).all()
    if not active_projects:
        flash(
            "Create an active project before importing tasks.",
            "warning",
        )
        return redirect(url_for("admin.projects"))

    form = TaskImportForm()
    form.project.choices = [
        (project.id, project.name) for project in active_projects
    ]
    if not form.validate_on_submit():
        for field in (form.project, form.csv_file):
            for error in field.errors:
                flash(error, "danger")
        return redirect(url_for("admin.tasks", import_open=1))

    upload = form.csv_file.data
    filename = (upload.filename or "").strip()
    if not filename.lower().endswith(".csv"):
        flash("Choose a .csv file (export your sheet as CSV UTF-8).",
              "danger")
        return redirect(url_for("admin.tasks", import_open=1))

    data = upload.read(IMPORT_MAX_BYTES + 1)
    if len(data) > IMPORT_MAX_BYTES:
        flash(
            f"File too large: the limit is {IMPORT_MAX_BYTES // 1_000_000}"
            " MB. Split the file and import in batches; nothing was"
            " saved.",
            "danger",
        )
        return redirect(url_for("admin.tasks", import_open=1))

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash(
            "That file is not a UTF-8 text/CSV file (binary files,"
            " spreadsheets and executables are not accepted).",
            "danger",
        )
        return redirect(url_for("admin.tasks", import_open=1))

    rows, structural = _parse_task_csv(text)
    if structural:
        for message in structural[:10]:
            flash(message, "danger")
        flash("Nothing was saved — fix the file and import again.",
              "warning")
        return redirect(url_for("admin.tasks", import_open=1))
    if not rows:
        flash("No task rows found in the file.", "warning")
        return redirect(url_for("admin.tasks", import_open=1))

    # Race guard: the project may have been paused since the page
    # rendered.
    project = db.session.get(Project, form.project.data)
    if project is None or project.status != "active":
        flash("That project is not active. Choose another.", "danger")
        return redirect(url_for("admin.tasks", import_open=1))

    _flag_duplicate_rows(rows, project.id)
    ready = sum(1 for row in rows if row["ok"])
    attention = len(rows) - ready
    existing_count = db.session.scalar(
        select(func.count(Task.id)).where(Task.project_id == project.id)
    )
    # Cost estimate: ready rows × pay_per_task × annotations per task
    # (default 1 when consensus not configured). Stays Decimal; template
    # casts to float at render time like every other money value.
    annotations_per = int(getattr(project, "annotations_per_task", 0) or 1)
    annotations_per = max(annotations_per, 1)
    cost_usd = (
        money(0) + (project.pay_per_task or money(0)) * ready * annotations_per
    )
    return render_template(
        "admin/tasks_import_preview.html",
        rows=rows,
        project=project,
        filename=filename[:120],
        ready=ready,
        attention=attention,
        existing_count=existing_count,
        cost_usd=cost_usd,
        annotations_per=annotations_per,
    )


@admin_bp.route("/tasks/import/confirm", methods=["POST"])
@admin_required
def import_confirm():
    """Insert the reviewed import rows (stage 2, full re-validation).

    Every submitted row is validated from scratch — the preview is
    never trusted. Rows the admin unticked (or left problem-flagged
    without fixing) are skipped and counted; the surviving batch is
    inserted atomically and the import is audited with counts only
    (never file contents).

    Returns:
        A redirect (PRG) to the project-filtered task list.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.tasks", import_open=1))

    project = db.session.get(
        Project, request.form.get("project", type=int)
    )
    if project is None or project.status != "active":
        flash("That project is not active. Choose another.", "danger")
        return redirect(url_for("admin.tasks", import_open=1))

    try:
        n_rows = int(request.form.get("n_rows", "0"))
    except ValueError:
        n_rows = -1
    if not 0 <= n_rows <= IMPORT_MAX_ROWS:
        flash("Malformed import batch. Start again from the file.",
              "danger")
        return redirect(url_for("admin.tasks", import_open=1))

    existing = set(db.session.scalars(
        select(Task.input_data).where(Task.project_id == project.id)
    ))
    seen: set[str] = set()
    to_insert: list[Task] = []
    problems: list[str] = []
    skipped = 0
    for i in range(n_rows):
        if request.form.get(f"row_include_{i}") != "y":
            continue
        task_text = (request.form.get(f"row_text_{i}") or "").strip()
        gold = (request.form.get(f"row_gold_{i}") or "").strip() or None
        if not task_text:
            problems.append(f"Row {i + 1}: task text is empty.")
            skipped += 1
        elif len(task_text) > MAX_INPUT_CHARS:
            problems.append(
                f"Row {i + 1}: task text exceeds {MAX_INPUT_CHARS}"
                " characters."
            )
            skipped += 1
        elif gold and len(gold) > MAX_INPUT_CHARS:
            problems.append(
                f"Row {i + 1}: expected output exceeds"
                f" {MAX_INPUT_CHARS} characters."
            )
            skipped += 1
        elif task_text in seen:
            problems.append(f"Row {i + 1}: duplicate inside the batch.")
            skipped += 1
        elif task_text in existing:
            problems.append(
                f"Row {i + 1}: already in this project."
            )
            skipped += 1
        else:
            seen.add(task_text)
            preference = _preference_document(task_text)
            if preference is not None:
                gold, gold_error = _preference_gold(gold)
                if gold_error:
                    problems.append(f"Row {i + 1}: {gold_error}")
                    skipped += 1
                    continue
                task_text = json.dumps(
                    preference, ensure_ascii=False
                )
            to_insert.append((task_text, gold))

    if not to_insert:
        for message in problems[:10]:
            flash(message, "danger")
        flash("Nothing was saved — no rows were ticked for import.",
              "warning")
        return redirect(url_for("admin.tasks", import_open=1))

    try:
        tasks = _build_import_tasks(project, to_insert)
        db.session.add_all(tasks)
        db.session.commit()
        log_event(
            "task.import",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={
                "imported": len(to_insert),
                "skipped": skipped,
                "file_rows": n_rows,
            },
            commit=True,
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Task import failed for project %s.", project.id
        )
        flash("Could not save the import. Please try again.", "danger")
        return redirect(url_for("admin.tasks", import_open=1))

    flash(
        f"Imported {len(to_insert)} tasks into"
        f" '{project.name}'."
        + (f" {skipped} row(s) skipped." if skipped else ""),
        "success",
    )
    for message in problems[:5]:
        flash(message, "warning")
    return redirect(url_for("admin.tasks", project=project.id))


@admin_bp.route("/tasks/import/template")
@admin_required
def import_template():
    """Download the one-screen CSV template for task imports.

    Returns:
        A text/csv attachment with the header and example rows.
    """
    body = (
        "text,gold\n"
        "The rain in Spain falls mainly,\n"
        "This movie was great,positive\n"
        "This movie was terrible,negative\n"
    )
    return Response(
        body.encode("utf-8-sig"),
        mimetype="text/csv",
        headers={"Content-Disposition": (
            'attachment; filename="'
            + _export_filename("import-template", "csv") + '"'
        )},
    )


@admin_bp.route("/tasks/upload", methods=["GET", "POST"])
@admin_required
def upload_tasks():
    """Bulk-create tasks from pasted lines (one task per line).

    GET simply opens the upload card on the tasks page, where the form
    lives. POST validates the project (must exist and be active), parses
    every line, and inserts the batch ATOMICALLY: any line-level error
    or over-size batch means nothing is written, so a half-uploaded
    batch can never happen.

    Returns:
        A redirect (PRG) back to the tasks list with flash feedback.
    """
    if request.method == "GET":
        return redirect(url_for("admin.tasks", upload_open=1))

    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.tasks", upload_open=1))

    active_projects = db.session.scalars(
        select(Project)
        .where(Project.status == "active")
        .order_by(Project.name)
    ).all()
    if not active_projects:
        flash(
            "Create an active project before uploading tasks.", "warning"
        )
        return redirect(url_for("admin.projects"))

    form = TaskUploadForm()
    form.project.choices = [
        (project.id, project.name) for project in active_projects
    ]
    if not form.validate_on_submit():
        # Missing project / empty textarea: surface the form's own errors.
        for field in (form.project, form.tasks_data):
            for error in field.errors:
                flash(error, "danger")
        return redirect(url_for("admin.tasks", upload_open=1))

    # Race guard: the project may have been paused since the page rendered.
    project = db.session.get(Project, form.project.data)
    if project is None or project.status != "active":
        flash("That project is not active. Choose another.", "danger")
        return redirect(url_for("admin.tasks", upload_open=1))

    parsed, errors = _parse_task_lines(form.tasks_data.data)
    if errors:
        # Atomic batch: show the problems, insert nothing. Always state
        # that explicitly — a lone error line must not look like the
        # rest of the batch was saved.
        for message in errors[:10]:
            flash(message, "danger")
        if len(errors) > 10:
            flash(
                f"...and {len(errors) - 10} more problem line(s).",
                "danger",
            )
        flash(
            "Nothing was saved — fix the problems above and re-upload.",
            "warning",
        )
        return redirect(url_for("admin.tasks", upload_open=1))

    if not parsed:
        flash(
            "No tasks found in the pasted data. Put one task per line.",
            "warning",
        )
        return redirect(url_for("admin.tasks", upload_open=1))

    if len(parsed) > UPLOAD_BATCH_LIMIT:
        flash(
            f"Batch too large: {len(parsed)} tasks. Upload at most"
            f" {UPLOAD_BATCH_LIMIT} at a time; nothing was saved.",
            "danger",
        )
        return redirect(url_for("admin.tasks", upload_open=1))

    tasks = _build_import_tasks(project, parsed)
    try:
        log_event(
            "task.upload",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={"count": len(tasks), "project": project.name},
        )
        db.session.add_all(tasks)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Task upload failed for project id %s.", project.id
        )
        flash("Could not save the tasks. Please try again.", "danger")
        return redirect(url_for("admin.tasks", upload_open=1))

    flash(
        f"{len(tasks)} task(s) created in \"{project.name}\".", "success"
    )
    # Land the admin on the fresh batch, filtered to its project.
    return redirect(
        url_for("admin.tasks", project=project.id, page=1)
    )


@admin_bp.route("/tasks")
@admin_required
def tasks():
    """List tasks with project/status/annotator filters and pagination.

    Query-string filters are validated against the database and the
    allowed status vocabulary; anything invalid or tampered is ignored
    rather than trusted. Page size is TASKS_PER_PAGE; out-of-range pages
    redirect to the last valid page.

    Returns:
        The rendered task list template.
    """
    filters = {"project": None, "status": None, "annotator": None}

    project_id = request.args.get("project", type=int)
    if project_id and db.session.get(Project, project_id) is not None:
        filters["project"] = project_id

    status = request.args.get("status", "")
    if status in TASK_STATUSES:
        filters["status"] = status

    annotator_id = request.args.get("annotator", type=int)
    if annotator_id:
        annotator = db.session.get(User, annotator_id)
        if annotator is not None and annotator.role == "annotator":
            filters["annotator"] = annotator_id

    page = max(request.args.get("page", 1, type=int) or 1, 1)

    stmt = (
        select(Task)
        .join(Task.project)
        .outerjoin(Task.assignee)
    )
    if filters["project"]:
        stmt = stmt.where(Task.project_id == filters["project"])
    if filters["status"]:
        stmt = stmt.where(Task.status == filters["status"])
    if filters["annotator"]:
        stmt = stmt.where(Task.assigned_to == filters["annotator"])
    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc())

    upload_form = TaskUploadForm()
    assign_form = AssignForm()

    try:
        pagination = db.paginate(
            stmt, page=page, per_page=TASKS_PER_PAGE, error_out=False
        )
        # Out-of-range page: redirect to the last valid one (same filters).
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for(
                    "admin.tasks",
                    page=pagination.pages,
                    project=filters["project"],
                    status=filters["status"],
                    annotator=filters["annotator"],
                )
            )
        page_tasks = list(pagination.items)
        # db.paginate() runs with scalars(), so the statement must select
        # the Task entity only. Related names are resolved for the page's
        # rows in two batched IN queries (no per-row N+1).
        project_ids = {task.project_id for task in page_tasks}
        assignee_ids = {
            task.assigned_to for task in page_tasks if task.assigned_to
        }
        project_names = (
            {
                pid: name
                for pid, name in db.session.execute(
                    select(Project.id, Project.name).where(
                        Project.id.in_(project_ids)
                    )
                ).all()
            }
            if project_ids
            else {}
        )
        assignee_names = (
            {
                uid: name
                for uid, name in db.session.execute(
                    select(User.id, User.name).where(
                        User.id.in_(assignee_ids)
                    )
                ).all()
            }
            if assignee_ids
            else {}
        )
        rows = [
            {
                "task": task,
                "project_name": project_names.get(task.project_id),
                "assignee_name": assignee_names.get(task.assigned_to),
            }
            for task in page_tasks
        ]
        # Filter-bar data: all projects + all annotators (history included).
        all_projects = db.session.scalars(
            select(Project).order_by(Project.name)
        ).all()
        all_annotators = db.session.scalars(
            select(User)
            .where(User.role == "annotator")
            .order_by(User.name)
        ).all()
        # Upload card lists active projects only; assignment bar lists
        # active annotators only.
        import_form = TaskImportForm()
        import_form.project.choices = [
            (project.id, project.name)
            for project in db.session.scalars(
                select(Project)
                .where(Project.status == "active")
                .order_by(Project.name)
            ).all()
        ]
        upload_form.project.choices = [
            (project.id, project.name)
            for project in all_projects
            if project.status == "active"
        ]
        assign_form.annotator_id.choices = [
            (annotator.id, annotator.name)
            for annotator in all_annotators
            if annotator.is_active
        ]
    except Exception:
        current_app.logger.exception("Task listing failed.")
        flash("Tasks could not be loaded.", "danger")
        rows, pagination, all_projects, all_annotators = [], None, [], []

    return render_template(
        "admin/tasks.html",
        rows=rows,
        pagination=pagination,
        filters=filters,
        all_projects=all_projects,
        all_annotators=all_annotators,
        statuses=TASK_STATUSES,
        upload_form=upload_form,
        import_form=import_form,
        assign_form=assign_form,
    )


@admin_bp.route("/tasks/assign", methods=["POST"])
@admin_required
def assign_tasks():
    """Assign selected pending tasks to an active annotator.

    Only tasks still in 'pending' under an ACTIVE project are assigned;
    the rest are counted and reported as skipped instead of being
    silently re-owned or pulled from paused/completed work. Selection is
    capped at UPLOAD_BATCH_LIMIT ids.

    Returns:
        A redirect back to the task list with flash feedback.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.tasks"))

    ids: set[int] = set()
    for raw in request.form.getlist("task_ids"):
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue  # tampered checkbox value: ignore

    if not ids:
        flash("Tick at least one pending task to assign.", "warning")
        return redirect(url_for("admin.tasks"))

    if len(ids) > UPLOAD_BATCH_LIMIT:
        flash(
            f"Selection too large ({len(ids)}). Assign at most"
            f" {UPLOAD_BATCH_LIMIT} tasks at a time.",
            "warning",
        )
        return redirect(url_for("admin.tasks"))

    annotator_id = request.form.get("annotator_id", type=int)
    annotator = (
        db.session.get(User, annotator_id) if annotator_id else None
    )
    if (
        annotator is None
        or annotator.role != "annotator"
        or not annotator.is_active
    ):
        flash("Choose an active annotator to assign to.", "danger")
        return redirect(url_for("admin.tasks"))

    try:
        # Pending tasks are only claimable under an ACTIVE project:
        # paused/completed projects must not feed the annotation queue.
        claimable = db.session.scalars(
            select(Task)
            .join(Task.project)
            .where(
                Task.id.in_(ids),
                Task.status == "pending",
                Project.status == "active",
            )
        ).all()
        pending_selected = db.session.scalar(
            select(func.count(Task.id)).where(
                Task.id.in_(ids), Task.status == "pending"
            )
        ) or 0
        inactive_blocked = pending_selected - len(claimable)
        # Blind-consensus guard: an annotator may hold only ONE sibling
        # per group. Overlaps are skipped and reported, never assigned.
        owned_groups = set(
            db.session.scalars(
                select(Task.consensus_group_id).where(
                    Task.assigned_to == annotator.id,
                    Task.consensus_group_id.is_not(None),
                )
            ).all()
        )
        if owned_groups:
            skipped_siblings = [
                t for t in claimable
                if t.consensus_group_id in owned_groups
            ]
            if skipped_siblings:
                claimable = [
                    t for t in claimable
                    if t.consensus_group_id not in owned_groups
                ]
                flash(
                    f"{len(skipped_siblings)} task(s) skipped:"
                    " they belong to a consensus round"
                    f" {annotator.name} is already part of.",
                    "warning",
                )
        # Qualification gate: an active quiz on the task's project
        # demands a passed (or exempt) attempt — gated tasks are
        # skipped and reported, never force-assigned. Grant an
        # exemption on the project's quiz console when staff decide
        # the work should proceed anyway.
        gated_projects = set(db.session.scalars(
            select(QualificationQuiz.project_id).where(
                QualificationQuiz.status == "active",
                QualificationQuiz.project_id.in_(
                    [t.project_id for t in claimable]
                ) if claimable else [],
                ~exists().where(
                    QuizAttempt.quiz_id == QualificationQuiz.id,
                    QuizAttempt.annotator_id == annotator.id,
                    QuizAttempt.status.in_(("passed", "exempt")),
                ),
            )
        ).all()) if claimable else set()
        if gated_projects:
            skipped_gated = [
                t for t in claimable
                if t.project_id in gated_projects
            ]
            claimable = [
                t for t in claimable
                if t.project_id not in gated_projects
            ]
            flash(
                f"{len(skipped_gated)} task(s) skipped:"
                f" {annotator.name} has not passed the qualification"
                " quiz for their project. Grant an exemption from"
                " the project's quiz console if this work should"
                " proceed anyway.",
                "warning",
            )
        for task in claimable:
            task.status = "assigned"
            task.assigned_to = annotator.id
        if claimable:
            log_event(
                "task.assign",
                actor=current_user,
                entity_type="user",
                entity_id=annotator.id,
                detail={
                    "assignee": annotator.name,
                    "count": len(claimable),
                    "task_ids": [t.id for t in claimable][:100],
                },
            )
            notify(
                annotator,
                "task.assigned",
                payload={"count": len(claimable)},
                subject=(
                    f"{len(claimable)} new task(s) assigned to you on"
                    " Brydl"
                ),
                body=(
                    "Hello,\n\n"
                    f"{len(claimable)} task(s) were just assigned to you"
                    " on Brydl.\nSign in to your dashboard to start:"
                    f" {current_app.config.get('SITE_URL') or ''}"
                    "/annotator/\n\n— Brydl"
                ),
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Bulk assignment failed for annotator id %s.", annotator_id
        )
        flash("Could not assign the tasks. Please try again.", "danger")
        return redirect(url_for("admin.tasks"))

    not_pending = len(ids) - pending_selected
    message = f"{len(claimable)} task(s) assigned to {annotator.name}."
    if inactive_blocked:
        message += (f" {inactive_blocked} skipped (their project is not"
                    " active).")
    if not_pending:
        message += (f" {not_pending} skipped (only pending tasks can be"
                    " assigned).")
    flash(message, "success" if claimable else "warning")
    return redirect(url_for("admin.tasks"))


@admin_bp.route("/tasks/<int:task_id>/unassign", methods=["POST"])
@admin_required
def unassign_task(task_id: int):
    """Return a claimed task to the pending pool (POST + CSRF only).

    Allowed while no QA verdict has been recorded: 'assigned',
    'in_progress' and 'rejected' tasks go back to 'pending' with no
    owner (the database invariant pending == unowned is preserved).
    Tasks at or past submission belong to the review flow and are
    refused — unassign is also the way to requeue a rejected task whose
    ownership was stripped during review.

    Args:
        task_id: Primary key of the task to unassign.

    Returns:
        A redirect back to the task list with a flash message. 404 for
        unknown ids.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.tasks"))

    task = db.session.get(Task, task_id)
    if task is None:
        abort(404)

    if task.status == "pending":
        flash("That task is already unassigned.", "info")
        return redirect(url_for("admin.tasks"))
    if task.status not in ("assigned", "in_progress", "rejected"):
        flash(
            f"Task #{task.id} is {task.status} and belongs to the QA"
            " flow; it cannot be unassigned.",
            "warning",
        )
        return redirect(url_for("admin.tasks"))

    previous_status = task.status
    task.status = "pending"
    task.assigned_to = None
    log_event(
        "task.unassign",
        actor=current_user,
        entity_type="task",
        entity_id=task.id,
        detail={"from_status": previous_status},
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Unassign failed for task id %s.", task_id
        )
        flash("Could not unassign the task. Please try again.", "danger")
        return redirect(url_for("admin.tasks"))

    flash(f"Task #{task_id} returned to the pending pool.", "success")
    return redirect(url_for("admin.tasks"))


# ---------------------------------------------------------------------------
# QA review: queue, single verdict, bulk verdicts
# ---------------------------------------------------------------------------


def _review_filters() -> dict:
    """Validate the review queue's project/annotator query params.

    Returns:
        A dict with validated "project" and "annotator" ids (or None).
        Tampered values are ignored, never trusted.
    """
    filters = {"project": None, "annotator": None}
    project_id = request.args.get("project", type=int)
    if project_id and db.session.get(Project, project_id) is not None:
        filters["project"] = project_id
    annotator_id = request.args.get("annotator", type=int)
    if annotator_id:
        annotator = db.session.get(User, annotator_id)
        if annotator is not None and annotator.role == "annotator":
            filters["annotator"] = annotator_id
    return filters


def _apply_verdict(
    submission_ids: list[int],
    action: str,
    qa_score: int,
    qa_notes: str | None,
    return_for_revision: bool,
) -> int:
    """Apply an approve/reject verdict to still-unreviewed submissions.

    The submission UPDATE is conditional on status='submitted', so a
    concurrent review can never double-score; only tasks still marked
    'submitted' transition with the verdict. Rejecting leaves the task
    with its annotator (revision loop) unless return_for_revision is
    False, in which case the task returns to the unowned pending pool
    for reassignment.

    Args:
        submission_ids: Submission primary keys to review.
        action: "approve" or "reject".
        qa_score: Score 0-100 recorded with the verdict.
        qa_notes: Optional feedback stored on the submission.
        return_for_revision: Reject-only; keep annotator ownership.

    Returns:
        A tuple of (transitions, escalated) — the number of
        submissions actually transitioned, and the tasks whose
        rejection count reached TASK_MAX_REJECTIONS and were therefore
        auto-returned to the pool as (task_id, annotator_id) pairs.
    """
    # The UI speaks in verbs ("approve"/"reject"); the database stores
    # statuses ("approved"/"rejected"). Map explicitly and refuse
    # anything else so an invalid state can never be written.
    status_by_verb = {"approve": "approved", "reject": "rejected"}
    if action not in status_by_verb:
        raise ValueError(f"Unknown review action: {action!r}")
    new_status = status_by_verb[action]

    reviewed_at = utc_now()
    updated_ids = [
        row[0]
        for row in db.session.execute(
            select(Submission.id).where(
                Submission.id.in_(submission_ids),
                Submission.status == "submitted",
            )
        ).all()
    ]
    escalated: list[tuple[int, int | None]] = []
    if not updated_ids:
        return 0, escalated
    db.session.execute(
        update(Submission)
        .where(Submission.id.in_(updated_ids))
        .values(
            status=new_status,
            qa_score=float(qa_score),
            qa_notes=qa_notes,
            reviewed_at=reviewed_at,
        )
        .execution_options(synchronize_session=False)
    )
    if action == "reject" and not return_for_revision:
        # Take the task away from the annotator: it returns to the
        # unowned pending pool for reassignment. (The ck_tasks_assignment
        # invariant forbids an ownerless task in any non-pending state,
        # so "rejected and unowned" is not a representable state.)
        task_values = {"status": "pending", "assigned_to": None}
    else:
        task_values = {"status": new_status}
    touched_task_ids = [
        row[0]
        for row in db.session.execute(
            select(Submission.task_id).where(
                Submission.id.in_(updated_ids)
            )
        ).all()
    ]
    db.session.execute(
        update(Task)
        .where(
            Task.id.in_(touched_task_ids),
            Task.status == "submitted",
        )
        .values(**task_values)
        .execution_options(synchronize_session=False)
    )
    if action == "reject" and return_for_revision:
        # Revision-loop exit condition: a task rejected
        # TASK_MAX_REJECTIONS times (counting every rejected
        # submission it has ever collected) auto-returns to the
        # pending pool — a bad-fit pairing must not cycle forever.
        cap = int(current_app.config.get("TASK_MAX_REJECTIONS", 3))
        for task_id, annotator_id, rejection_count in db.session.execute(
            select(
                Submission.task_id,
                Submission.annotator_id,
                func.count(Submission.id),
            )
            .where(
                Submission.task_id.in_(touched_task_ids),
                Submission.status == "rejected",
            )
            .group_by(Submission.task_id, Submission.annotator_id)
        ).all():
            if rejection_count >= cap:
                db.session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(status="pending", assigned_to=None)
                    .execution_options(synchronize_session=False)
                )
                escalated.append((task_id, annotator_id))
    # Consensus bookkeeping: verdicts change sibling answers, so the
    # touched groups re-evaluate (resolved groups stay frozen).
    for tid in set(touched_task_ids):
        group_id = db.session.scalar(
            select(Task.consensus_group_id).where(Task.id == tid)
        )
        if group_id is not None:
            refresh_group(group_id)
    return len(updated_ids), escalated


@admin_bp.route("/review")
@qa_required
def review():
    """List submissions awaiting QA review, filterable and paginated.

    The queue is oldest-first: the longest-waiting annotator gets paid
    feedback first. Filters (project, annotator) are validated against
    the database; tampered values are ignored.

    Returns:
        The rendered review queue template.
    """
    filters = {"project": None, "annotator": None}
    rows: list = []
    pagination = None
    all_projects: list = []
    all_annotators: list = []
    try:
        filters = _review_filters()
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        stmt = select(Submission).where(Submission.status == "submitted")
        if filters["project"] or filters["annotator"]:
            stmt = stmt.join(Task, Submission.task_id == Task.id)
            if filters["project"]:
                stmt = stmt.where(Task.project_id == filters["project"])
            if filters["annotator"]:
                stmt = stmt.where(
                    Submission.annotator_id == filters["annotator"]
                )
        stmt = stmt.order_by(
            Submission.submitted_at.asc(), Submission.id.asc()
        )
        pagination = db.paginate(
            stmt, page=page, per_page=REVIEWS_PER_PAGE, error_out=False
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for(
                    "admin.review",
                    page=pagination.pages,
                    project=filters["project"],
                    annotator=filters["annotator"],
                )
            )
        page_submissions = list(pagination.items)
        # Display names and task input for the page's rows, batched.
        task_ids = {s.task_id for s in page_submissions}
        annotator_ids = {s.annotator_id for s in page_submissions}
        task_info: dict[int, tuple[str, str]] = {}
        if task_ids:
            task_info = {
                tid: (inp, pname)
                for tid, inp, pname in db.session.execute(
                    select(Task.id, Task.input_data, Project.name)
                    .join(Task.project)
                    .where(Task.id.in_(task_ids))
                ).all()
            }
        annotator_names = (
            {
                uid: name
                for uid, name in db.session.execute(
                    select(User.id, User.name).where(
                        User.id.in_(annotator_ids)
                    )
                ).all()
            }
            if annotator_ids
            else {}
        )
        rows = [
            {
                "submission": s,
                "task_input": task_info.get(s.task_id, ("—", "—"))[0],
                "project_name": task_info.get(s.task_id, ("—", "—"))[1],
                "annotator_name": annotator_names.get(s.annotator_id, "—"),
            }
            for s in page_submissions
        ]
        all_projects = db.session.scalars(
            select(Project).order_by(Project.name)
        ).all()
        all_annotators = db.session.scalars(
            select(User)
            .where(User.role == "annotator")
            .order_by(User.name)
        ).all()
    except Exception:
        current_app.logger.exception("Review queue failed.")
        flash("Review queue could not be loaded.", "danger")
        rows, pagination = [], None

    bulk_form = BulkReviewForm()
    bulk_form.qa_score.data = 100  # sensible prefill; admin can change it
    return render_template(
        "admin/review.html",
        rows=rows,
        pagination=pagination,
        filters=filters,
        all_projects=all_projects,
        all_annotators=all_annotators,
        bulk_form=bulk_form,
    )


@admin_bp.route("/review/<int:submission_id>", methods=["GET", "POST"])
@qa_required
def review_detail(submission_id: int):
    """Show one submission side-by-side with its gold standard; verdict.

    GET renders the task input, the expected output (gold standard, if
    any) and the annotator's output for comparison, plus the QA form.
    POST records the verdict conditionally (only 'submitted' work can
    transition) and moves the task to 'approved', or to 'rejected' —
    keeping annotator ownership for revision by default.

    Args:
        submission_id: Primary key of the submission under review.

    Returns:
        The rendered detail template, or a redirect with flash feedback.
    """
    # One SELECT for submission + task + project + annotator (joined
    # loads): the detail page touches all four, and fetching them
    # together keeps the request at a single query.
    submission = db.session.get(
        Submission,
        submission_id,
        options=[
            joinedload(Submission.task).joinedload(Task.project),
            joinedload(Submission.annotator),
        ],
    )
    if submission is None:
        abort(404)
    if submission.status != "submitted":
        flash(
            f"Submission #{submission_id} was already reviewed"
            f" ({submission.status}).",
            "info",
        )
        return redirect(url_for("admin.review"))

    task = submission.task
    project = task.project
    annotator = submission.annotator

    # Pretty-print structured answers for the comparison column; fall
    # back to the raw string for anything unparseable. Preference
    # answers get a readable sentence form instead of raw JSON, and the
    # task input renders as prompt + response columns.
    pref_input = _preference_document(task.input_data)
    input_view = content_view(task.input_data)
    # Media workspaces (Phase 8): staff review sees the actual
    # image/audio next to the answer.
    review_media = _media_input_document(task.input_data)
    review_media_url = (
        url_for("admin.media_serve", asset_id=review_media["media_id"])
        if review_media is not None else None
    )
    review_media_prompt = (
        content_view(review_media.get("prompt") or "")
        if review_media is not None else None
    )
    try:
        parsed_output = json.loads(submission.output_data)
        if isinstance(parsed_output, dict) \
                and parsed_output.get("kind") == "preference":
            chosen = parsed_output.get("chosen", "?")
            seen = parsed_output.get("seen_order", "?")
            output_display = (
                f"Chose: Response {chosen.upper()}"
                f" (saw order {seen})\n"
                "Justification: "
                f"{parsed_output.get('justification', '')}"
            )
        else:
            output_display = json.dumps(
                parsed_output, indent=2, ensure_ascii=False
            )
    except ValueError:
        output_display = submission.output_data

    # ---- Dynamic red-team: fetch full transcript & recompute breach
    # count at review time so the payout uses QA-confirmed flags. ----
    redteam_transcript = None
    redteam_payout_usd = None
    if (project.task_type == "dynamic_red_team"
            and submission.redlab_session_id):
        try:
            from redlab.integration import get_session_transcript
            redteam_transcript = get_session_transcript(
                submission.redlab_session_id
            )
        except Exception:
            current_app.logger.exception(
                "Failed to load red-team transcript for sub=%s",
                submission.id,
            )
            redteam_transcript = None
        # Exact-Decimal preview payout (base + confirmed breaches * bounty).
        base = project.redteam_base_pay or Decimal("0.00")
        bounty = project.redteam_breach_bounty or Decimal("0.00")
        confirmed = Decimal(int(submission.redteam_breach_count or 0))
        redteam_payout_usd = money(
            Decimal(base) + Decimal(bounty) * confirmed
        )

    form = ReviewForm()
    if form.validate_on_submit():
        # If QA is approving a red-team sub, recompute the confirmed
        # breach count from the assistant-message flags currently in
        # the DB (which may have been toggled via qa-flag-toggle above)
        # so the cached count matches what payout aggregation will sum.
        if (project.task_type == "dynamic_red_team"
                and submission.redlab_session_id
                and form.action.data == "approve"):
            try:
                from redlab.integration import count_confirmed_breaches
                confirmed_q = count_confirmed_breaches(
                    submission.redlab_session_id
                )
                submission.redteam_breach_count = int(confirmed_q)
                db.session.flush()
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "Could not sync red-team breach count for sub=%s",
                    submission.id,
                )
        try:
            updated, escalated = _apply_verdict(
                submission_ids=[submission.id],
                action=form.action.data,
                qa_score=form.qa_score.data,
                qa_notes=(form.qa_notes.data or "").strip() or None,
                return_for_revision=form.return_for_revision.data,
            )
            log_event(
                "review.verdict",
                actor=current_user,
                entity_type="submission",
                entity_id=submission.id,
                detail={
                    "task_id": task.id,
                    "action": form.action.data,
                    "qa_score": form.qa_score.data,
                    "return_for_revision": bool(
                        form.return_for_revision.data
                    ),
                    "escalated": len(escalated),
                },
            )
            if form.action.data == "reject":
                recipient = db.session.get(
                    User, submission.annotator_id
                )
                if recipient is not None:
                    if escalated:
                        notify(
                            recipient,
                            "task.escalated",
                            payload={"task_id": task.id},
                            subject="A task of yours was escalated to"
                                    " Brydl",
                            body=(
                                "Hello,\n\n"
                                "A task was sent back for revision"
                                " several times, so it has been"
                                " returned to the Brydl team to"
                                " reassign.\nNo action is needed"
                                " from you — check your dashboard for"
                                " fresh work.\n\n— Brydl"
                            ),
                        )
                    else:
                        notify(
                            recipient,
                            "task.rejected",
                            payload={"task_id": task.id},
                            subject=(
                                "A submission needs your revision on"
                                " Brydl"
                            ),
                            body=(
                                "Hello,\n\n"
                                "A submission was sent back for"
                                " revision by QA.\nOpen your"
                                " dashboard to see the reviewer's"
                                " notes:"
                                f" {current_app.config.get('SITE_URL') or ''}"
                                "/annotator/\n\n— Brydl"
                            ),
                        )
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Review failed for submission %s.", submission_id
            )
            flash("Could not save the review. Please try again.", "danger")
            return render_template(
                "admin/review_detail.html",
                submission=submission,
                task=task,
                project=project,
                annotator=annotator,
                output_display=output_display,
                pref_input=pref_input,
                input_view=input_view,
                media_doc=review_media,
                media_url=review_media_url,
                media_prompt=review_media_prompt,
                rubric_view=_rubric_view(submission, project),
                consensus=(
                    {
                        "id": task.consensus_group_id,
                        "status": task.consensus_group.status,
                        "decision": task.consensus_group.decided_choice,
                    }
                    if task.consensus_group_id is not None else None
                ),
                form=form,
                redteam_transcript=redteam_transcript,
                redteam_payout_usd=redteam_payout_usd,
            )
        if updated != 1:
            # Lost a race: another reviewer got here first.
            db.session.rollback()
            flash(
                f"Submission #{submission_id} was just reviewed by"
                " someone else.",
                "warning",
            )
            return redirect(url_for("admin.review"))

        verb = "approved" if form.action.data == "approve" else "rejected"
        message = f"Submission #{submission_id} {verb}."
        if escalated:
            cap = int(current_app.config.get("TASK_MAX_REJECTIONS", 3))
            message += (
                f" Task #{task.id} hit the {cap}-rejection limit and"
                " returned to the pool for reassignment."
            )
        flash(message, "success")
        return redirect(url_for("admin.review"))

    if form.csrf_token.errors:
        flash("Security check failed. Please try again.", "warning")
    return render_template(
        "admin/review_detail.html",
        submission=submission,
        task=task,
        project=project,
        annotator=annotator,
        output_display=output_display,
        pref_input=pref_input,
        input_view=input_view,
        media_doc=review_media,
        media_url=review_media_url,
        media_prompt=review_media_prompt,
        rubric_view=_rubric_view(submission, project),
        form=form,
        redteam_transcript=redteam_transcript,
        redteam_payout_usd=redteam_payout_usd,
    )


@admin_bp.route(
    "/review/redteam/<int:submission_id>/messages/<mid>/flag",
    methods=["POST"],
)
@qa_required
def redteam_qa_flag_toggle(submission_id: int, mid: str):
    """QA override of an assistant-turn breach flag for a red-team sub.

    QA reviewers can confirm or overturn the annotator's per-turn
    safety flags before they approve a dynamic red-team submission.
    Toggling a flag re-syncs ``Session.breach_count`` and the cached
    ``Submission.redteam_breach_count`` so the payout ledger uses the
    QA-confirmed count (base + breach_bounty * confirmed_count).

    This endpoint accepts JSON ``{"is_unsafe": true|false}`` (with
    X-CSRFToken header) and returns the fresh breach count; only
    'submitted' dynamic_red_team submissions can be edited, to
    prevent tampering after a verdict is recorded.
    """
    try:
        csrf_val = (
            request.headers.get("X-CSRFToken")
            or request.headers.get("X-CSRF-Token")
            or (request.get_json(silent=True) or {}).get("csrf_token")
            or request.form.get("csrf_token", "")
        )
        validate_csrf(csrf_val)
    except ValidationError:
        return {"ok": False, "error": "csrf_failed"}, 400

    sub = db.session.get(Submission, submission_id)
    if sub is None:
        abort(404)
    task = sub.task
    project = task.project
    if (sub.status != "submitted"
            or project.task_type != "dynamic_red_team"
            or not sub.redlab_session_id):
        return {"ok": False, "error": "not_editable"}, 400
    try:
        payload = request.get_json(silent=True) or {}
        is_unsafe = bool(payload.get("is_unsafe"))
    except Exception:
        return {"ok": False, "error": "bad_json"}, 400
    try:
        from redlab.integration import toggle_qa_message_flag
        new_count = toggle_qa_message_flag(
            sub.redlab_session_id, mid, is_unsafe
        )
        sub.redteam_breach_count = new_count
        db.session.commit()
        log_event(
            "review.redteam_flag_toggle",
            actor=current_user,
            entity_type="submission",
            entity_id=sub.id,
            detail={"message_id": mid, "is_unsafe": is_unsafe,
                    "breach_count": new_count},
        )
        return {"ok": True, "breach_count": int(new_count),
                "message_id": mid, "is_unsafe": is_unsafe}
    except ValueError as err:
        db.session.rollback()
        return {"ok": False, "error": str(err)}, 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "QA flag toggle failed for sub=%s msg=%s",
            submission_id, mid,
        )
        return {"ok": False, "error": "internal"}, 500


@admin_bp.route("/review/bulk", methods=["POST"])
@qa_required
def review_bulk():
    """Approve/reject many submissions at once with one shared score.

    Selection is capped at UPLOAD_BATCH_LIMIT ids; only submissions
    still in 'submitted' state transition (the rest are reported as
    skipped). Notes are intentionally not set in bulk — individual
    feedback belongs on the detail page.

    Returns:
        A redirect back to the review queue with flash feedback.
    """
    form = BulkReviewForm()
    if not form.validate_on_submit():
        # A CSRF failure lands on the token field, not the visible
        # fields — call it out explicitly or the redirect explains
        # nothing.
        if form.csrf_token.errors:
            flash("Security check failed. Please try again.", "warning")
        for field in (form.action, form.qa_score):
            for error in field.errors:
                flash(error, "danger")
        return redirect(url_for("admin.review"))

    ids: set[int] = set()
    for raw in request.form.getlist("submission_ids"):
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue  # tampered checkbox value: ignore

    if not ids:
        flash("Tick at least one submission to review.", "warning")
        return redirect(url_for("admin.review"))

    if len(ids) > UPLOAD_BATCH_LIMIT:
        flash(
            f"Selection too large ({len(ids)}). Review at most"
            f" {UPLOAD_BATCH_LIMIT} at a time.",
            "warning",
        )
        return redirect(url_for("admin.review"))

    try:
        updated, escalated = _apply_verdict(
            submission_ids=list(ids),
            action=form.action.data,
            qa_score=form.qa_score.data,
            qa_notes=None,
            return_for_revision=form.return_for_revision.data,
        )
        log_event(
            "review.bulk",
            actor=current_user,
            detail={
                "action": form.action.data,
                "updated": updated,
                "skipped": len(ids) - updated,
                "qa_score": form.qa_score.data,
                "escalated": len(escalated),
            },
        )
        if form.action.data == "reject" and escalated:
            for task_id, annotator_id in escalated:
                recipient = (
                    db.session.get(User, annotator_id)
                    if annotator_id else None
                )
                if recipient is not None:
                    notify(
                        recipient,
                        "task.escalated",
                        payload={"task_id": task_id},
                        subject="A task of yours was escalated to Brydl",
                        body=(
                            "Hello,\n\n"
                            "A task was sent back for revision several"
                            " times, so it has been returned to the"
                            " Brydl team to reassign.\nNo action is"
                            " needed from you — check your dashboard"
                            " for fresh work.\n\n— Brydl"
                        ),
                    )
        if form.action.data == "reject" and updated:
            rejections = dict(
                db.session.execute(
                    select(
                        Submission.annotator_id,
                        func.count(Submission.id),
                    )
                    .where(
                        Submission.id.in_(ids),
                        Submission.status == "rejected",
                    )
                    .group_by(Submission.annotator_id)
                ).all()
            )
            for annotator_id, count in rejections.items():
                recipient = db.session.get(User, annotator_id)
                if recipient is not None:
                    notify(
                        recipient,
                        "task.rejected",
                        payload={"count": count},
                        subject=(
                            f"{count} submission(s) need your revision"
                            " on Brydl"
                        ),
                        body=(
                            "Hello,\n\n"
                            f"{count} of your submission(s) were sent"
                            " back for revision by QA.\nSign in to see"
                            " the details:"
                            f" {current_app.config.get('SITE_URL') or ''}"
                            "/annotator/\n\n— Brydl"
                        ),
                    )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Bulk review failed.")
        flash("Could not save the bulk review. Please try again.", "danger")
        return redirect(url_for("admin.review"))

    verb = "approved" if form.action.data == "approve" else "rejected"
    skipped = len(ids) - updated
    message = f"{updated} submission(s) {verb}."
    if skipped:
        message += f" {skipped} skipped (already reviewed)."
    if escalated:
        message += (
            f" {len(escalated)} task(s) hit the rejection limit and"
            " returned to the pool."
        )
    flash(message, "success" if updated else "warning")
    return redirect(url_for("admin.review"))


@admin_bp.route("/review/conflicts")
@auditor_required
def consensus_conflicts():
    """Blind-consensus rounds: agreed / conflict / resolved lists.

    Each row shows every sibling's annotator, their canonical verdict
    and justification (the arbitration evidence), and the group's
    current status. Filterable by status, paginated per group.

    Returns:
        The rendered conflicts template.
    """
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    status_filter = (request.args.get("status") or "").strip()
    allowed = ("pending", "agreed", "conflict", "resolved")

    # KPI counts for the header — single grouped query, no per-tab hit.
    counts = {
        "pending": 0, "agreed": 0, "conflict": 0, "resolved": 0,
    }
    try:
        for st, n in db.session.execute(
            select(ConsensusGroup.status, func.count(ConsensusGroup.id))
            .group_by(ConsensusGroup.status)
        ).all():
            if st in counts:
                counts[st] = int(n or 0)
    except Exception:
        current_app.logger.exception("Consensus counts failed.")
    counts["needs_attention"] = counts["pending"] + counts["conflict"]

    stmt = (
        select(ConsensusGroup)
        .options(db.joinedload(ConsensusGroup.project))
        .order_by(ConsensusGroup.id.desc())
    )
    if status_filter in allowed:
        stmt = stmt.where(ConsensusGroup.status == status_filter)

    try:
        pagination = db.paginate(
            stmt, page=page, per_page=PAGINATION_PER_PAGE,
            error_out=False,
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for("admin.consensus_conflicts",
                        page=pagination.pages)
            )
        groups = list(pagination.items)
        # One query per artefact class (never per group): siblings,
        # latest submissions, annotator names.
        task_ids = [t.id for g in groups for t in g.tasks]
        subs_by_task: dict[int, list] = {}
        annotators: dict[int, str] = {}
        if task_ids:
            for sub in db.session.scalars(
                select(Submission)
                .where(Submission.task_id.in_(task_ids))
                .order_by(Submission.id.asc())
            ).all():
                subs_by_task.setdefault(sub.task_id, []).append(sub)
            annotator_ids = {
                s.annotator_id
                for subs in subs_by_task.values() for s in subs
            }
            if annotator_ids:
                annotators = dict(db.session.execute(
                    select(User.id, User.name).where(
                        User.id.in_(annotator_ids)
                    )
                ).all())
        rows = []
        for g in groups:
            verdicts = []
            for t in g.tasks:
                subs = subs_by_task.get(t.id, [])
                latest = subs[-1] if subs else None
                key = None
                if latest is not None:
                    try:
                        doc = json.loads(latest.output_data or "")
                        key = doc.get("chosen") or doc.get("label")
                    except ValueError:
                        key = None
                verdicts.append({
                    "task_id": t.id,
                    "annotator": annotators.get(
                        latest.annotator_id, "?"
                    ) if latest else None,
                    "choice": key,
                    "justification": (
                        _preference_justification(latest)
                        if latest else None
                    ),
                    "status": latest.status if latest else None,
                })
            rows.append({"group": g, "verdicts": verdicts})
    except Exception:
        current_app.logger.exception("Conflicts page failed.")
        flash("Could not load the consensus list.", "danger")
        rows, pagination = [], None

    return render_template(
        "admin/conflicts.html", rows=rows, pagination=pagination,
        status_filter=status_filter, counts=counts,
    )


def _media_input_document(raw: str) -> dict | None:
    """Parse a media task input (image_labeling / audio_transcription).

    Args:
        raw: The task's input_data column.

    Returns:
        The validated document (integer media_id required), else None.
    """
    try:
        doc = json.loads(raw or "")
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("kind") not in ("image_labeling", "audio_transcription"):
        return None
    media_id = doc.get("media_id")
    if isinstance(media_id, bool) or not isinstance(media_id, int):
        return None
    return doc if media_id >= 1 else None


def _rubric_view(
    submission: Submission | None, project: Project
) -> dict | None:
    """Extract a display-safe rubric from a preference submission.

    Args:
        submission: The submission to read (None tolerated).
        project: The owning project (its dimensions are the schema the
            rubric must match exactly).

    Returns:
        {dimension: {"a": int, "b": int}} when the stored rubric is
        valid for this project, else None (older docs predate the
        rubric; malformed docs never render).
    """
    if submission is None:
        return None
    dims = parse_dimensions(project.rubric_dimensions or "")[0]
    if not dims:
        return None
    try:
        doc = json.loads(submission.output_data or "")
    except ValueError:
        return None
    rubric = doc.get("rubric") if isinstance(doc, dict) else None
    if rubric_scores_valid(
        rubric,
        dims,
        int(current_app.config.get("RUBRIC_SCALE_MIN", 1)),
        int(current_app.config.get("RUBRIC_SCALE_MAX", 5)),
    ):
        return {dim: rubric[dim] for dim in dims}
    return None


def _preference_justification(submission: Submission) -> str | None:
    """Pull the human justification out of a submission document.

    Args:
        submission: The submission to read.

    Returns:
        The justification/explanation text, or None when absent.
    """
    try:
        doc = json.loads(submission.output_data or "")
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    return (
        doc.get("justification")
        or doc.get("explanation")
        or doc.get("text")
    )


@admin_bp.route("/review/conflicts/<int:group_id>",
                methods=["GET", "POST"])
@auditor_required
def arbitrate(group_id: int):
    """Arbitrate one consensus group: view verdicts, decide bindingly.

    GET renders the sibling verdicts side by side. POST (CSRF) records
    the binding decision — decided_choice, optional note, auditor and
    timestamp — freezes the group as resolved and audits the ruling.
    Resolved groups render a read-only summary so auditors can review
    prior rulings.

    Args:
        group_id: Primary key of the ConsensusGroup.

    Returns:
        The arbitration page (GET/failed POST/read-only) or a redirect
        to the next pending conflict (or the list) after a successful
        ruling. 404 for unknown ids.
    """
    group = db.session.get(ConsensusGroup, group_id)
    if group is None:
        abort(404)

    # Resolved rounds redirect back to the list with a flash by default
    # (the binding ruling is immutable and shouldn't be re-arbitrated).
    # Appending ?view=1 renders a read-only summary for audit purposes.
    if (group.status == "resolved"
            and request.args.get("view") != "1"
            and request.method == "GET"):
        flash("This round was already resolved — the ruling is"
              " binding.", "info")
        return redirect(url_for("admin.consensus_conflicts"))

    def _decoded_resolver_name() -> str:
        if not group.resolved_by:
            return ""
        u = db.session.get(User, group.resolved_by)
        return u.name if u else f"user #{group.resolved_by}"

    # Sibling verdicts: latest submission per task with annotator name,
    # canonical choice and justification — the arbitration evidence.
    def _build_verdicts():
        task_ids = [t.id for t in group.tasks]
        subs_by_task: dict[int, list] = {}
        annotators: dict[int, str] = {}
        if task_ids:
            for sub in db.session.scalars(
                select(Submission)
                .where(Submission.task_id.in_(task_ids))
                .order_by(Submission.id.asc())
            ).all():
                subs_by_task.setdefault(sub.task_id, []).append(sub)
            annotator_ids = {
                s.annotator_id
                for subs in subs_by_task.values() for s in subs
            }
            if annotator_ids:
                annotators = dict(db.session.execute(
                    select(User.id, User.name).where(
                        User.id.in_(annotator_ids)
                    )
                ).all())
        out = []
        choices: list[str] = []
        for t in group.tasks:
            subs = subs_by_task.get(t.id, [])
            latest = subs[-1] if subs else None
            key = None
            if latest is not None:
                try:
                    doc = json.loads(latest.output_data or "")
                    key = (doc.get("chosen") or doc.get("label"))
                    if isinstance(key, str):
                        key = key.strip()
                except ValueError:
                    key = None
            if key:
                choices.append(key.lower())
            out.append({
                "task_id": t.id,
                "annotator": annotators.get(
                    latest.annotator_id, "?"
                ) if latest else None,
                "choice": key,
                "justification": (
                    _preference_justification(latest) if latest else None
                ),
                "rubric": _rubric_view(latest, group.project),
                "status": latest.status if latest else None,
            })
        # agreement: all the same non-empty choice
        unique = {c for c in choices if c}
        return out, (len(unique) <= 1 and len(choices) >= 2)

    verdicts, in_agreement = _build_verdicts()
    prompt_view = content_view(
        group.tasks[0].input_data if group.tasks else ""
    )
    round_media = _media_input_document(
        group.tasks[0].input_data if group.tasks else ""
    )
    round_media_url = (
        url_for("admin.media_serve", asset_id=round_media["media_id"])
        if round_media is not None else None
    )
    resolver_name = _decoded_resolver_name()
    # Locate the next pending/conflict round so we can advance the
    # auditor straight to it after a ruling (less clicking).
    next_group_id = db.session.scalar(
        select(ConsensusGroup.id)
        .where(ConsensusGroup.status.in_(("pending", "conflict")))
        .where(ConsensusGroup.id != group.id)
        .order_by(ConsensusGroup.id.asc())
    )

    if request.method == "GET" or group.status == "resolved":
        return render_template(
            "admin/arbitrate.html", group=group, verdicts=verdicts,
            prompt_view=prompt_view,
            media_doc=round_media, media_url=round_media_url,
            in_agreement=in_agreement,
            resolver_name=resolver_name,
            next_group_id=next_group_id,
            read_only=(group.status == "resolved"),
        )

    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.consensus_conflicts"))

    decided = (request.form.get("decided_choice") or "").strip()
    note = (request.form.get("decided_note") or "").strip() or None
    if group.project.task_type == "preference":
        if decided.lower() not in ("a", "b"):
            flash('Choose Response A or Response B as the ruling.',
                  "danger")
            return render_template(
                "admin/arbitrate.html", group=group,
                verdicts=verdicts, prompt_view=prompt_view,
                media_doc=round_media, media_url=round_media_url,
                in_agreement=in_agreement, resolver_name=resolver_name,
                next_group_id=next_group_id, read_only=False,
            )
        decided = decided.lower()
    elif not decided:
        flash("Write the ruling for this round.", "danger")
        return render_template(
            "admin/arbitrate.html", group=group,
            verdicts=verdicts, prompt_view=prompt_view,
            media_doc=round_media, media_url=round_media_url,
            in_agreement=in_agreement, resolver_name=resolver_name,
            next_group_id=next_group_id, read_only=False,
        )

    group.status = "resolved"
    group.decided_choice = decided[:100]
    group.decided_note = note
    group.resolved_by = current_user.id
    group.resolved_at = utc_now()
    try:
        log_event(
            "consensus.resolve",
            actor=current_user,
            entity_type="consensus_group",
            entity_id=group.id,
            detail={
                "project_id": group.project_id,
                "decision": group.decided_choice,
                "task_ids": [t.id for t in group.tasks][:100],
            },
            commit=True,
        )
        flash(f"Round #{group.id} resolved — the ruling is binding.",
              "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Arbitration failed for group %s.", group.id
        )
        flash("Could not save the ruling. Please try again.", "danger")
        return redirect(url_for("admin.arbitrate", group_id=group.id))
    if next_group_id:
        return redirect(url_for("admin.arbitrate",
                                group_id=next_group_id))
    return redirect(url_for("admin.consensus_conflicts"))


@admin_bp.route("/quality")
@qa_required
def quality():
    """Per-annotator quality table: throughput, accuracy, QA scores.

    One grouped query covers volumes and scores for the page's
    annotators; gold accuracy is computed in Python from the gold
    subset (JSON answers vs stored expected outputs) so the logic
    stays engine-agnostic (SQLite dev / PostgreSQL prod). Paginated
    like every list in the app.

    Returns:
        The rendered quality template.
    """
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    table: list[dict] = {}
    pagination = None
    try:
        pagination = db.paginate(
            select(User)
            .where(User.role == "annotator")
            .order_by(User.name.asc(), User.id.asc()),
            page=page,
            per_page=PAGINATION_PER_PAGE,
            error_out=False,
        )
        ids = [u.id for u in pagination.items]
        agg: dict[int, dict] = {
            annotator_id: {
                "approved": 0, "rejected": 0, "avg_qa": None,
                "last30": 0, "gold_total": 0, "gold_correct": 0,
            }
            for annotator_id in ids
        }
        if ids:
            since = utc_now() - timedelta(days=30)
            for row in db.session.execute(
                select(
                    Submission.annotator_id,
                    func.sum(case(
                        (Submission.status == "approved", 1), else_=0,
                    )),
                    func.sum(case(
                        (Submission.status == "rejected", 1), else_=0,
                    )),
                    func.avg(case(
                        (Submission.status == "approved",
                         Submission.qa_score),
                        else_=None,
                    )),
                    func.sum(case(
                        (Submission.submitted_at >= since, 1), else_=0,
                    )),
                ).where(Submission.annotator_id.in_(ids))
                .group_by(Submission.annotator_id)
            ).all():
                agg[row[0]].update({
                    "approved": int(row[1] or 0),
                    "rejected": int(row[2] or 0),
                    "avg_qa": (round(float(row[3]), 1)
                               if row[3] is not None else None),
                    "last30": int(row[4] or 0),
                })
            # Gold accuracy: compare each submission's answer payload
            # with the stored expected output (exact, case-insensitive
            # on the extracted value).
            gold = db.session.execute(
                select(
                    Submission.annotator_id,
                    Submission.output_data,
                    Task.expected_output,
                )
                .join(Submission.task)
                .where(
                    Submission.annotator_id.in_(ids),
                    Task.expected_output.is_not(None),
                )
            ).all()
            for annotator_id, output_json, expected in gold:
                entry = agg[annotator_id]
                entry["gold_total"] += 1
                label = None
                try:
                    parsed = json.loads(output_json or "")
                    label = parsed.get(
                        "label",
                        parsed.get("chosen", parsed.get("text")),
                    ) if isinstance(parsed, dict) else None
                except ValueError:
                    label = (output_json or "") or None
                if (
                    label is not None
                    and str(label).strip().lower()
                    == str(expected).strip().lower()
                ):
                    entry["gold_correct"] += 1
        table = {
            u.id: {**(agg.get(u.id) or {}), "user": u}
            for u in pagination.items
        }
    except Exception:
        current_app.logger.exception("Quality report failed.")
        flash("Quality report could not be loaded.", "danger")
    return render_template(
        "admin/quality.html", table=table, pagination=pagination,
    )


# ---------------------------------------------------------------------------
# Payments: earnings ledger, payout generation, mark-as-paid
# ---------------------------------------------------------------------------

PAYMENT_METHODS = ("bank_transfer", "mobile_money", "cash", "other")


def _unclaimed_agg(
    annotator_id: int | None = None,
) -> dict[int, tuple[int, Decimal, object]]:
    """Aggregate unpaid approved work per annotator in one grouped query.

    "Unclaimed" means an approved submission whose payment_id is NULL —
    approved work that has never been bundled into any payout run.

    Per-submission payout is computed in the database using exact
    Decimal arithmetic:
      - For standard tasks: project.pay_per_task (already Decimal).
      - For dynamic_red_team tasks: project.redteam_base_pay +
        submission.redteam_breach_count * project.redteam_breach_bounty.
    A SQL ``CASE`` expression picks the right branch per task type,
    and SUM/COALESCE returns a Decimal we then quantize to 2dp with
    :func:`models.money` — no float arithmetic is involved.

    Args:
        annotator_id: Optional annotator to restrict the aggregation to.

    Returns:
        Mapping of annotator id to (count, USD Decimal rounded to 2
        decimals, earliest submitted_at in the bundle).
    """
    per_sub = (
        case(
            (Project.task_type == "dynamic_red_team",
             func.coalesce(Project.redteam_base_pay, 0)
             + func.coalesce(Submission.redteam_breach_count, 0)
             * func.coalesce(Project.redteam_breach_bounty, 0)),
            else_=func.coalesce(Project.pay_per_task, 0),
        )
    )
    stmt = (
        select(
            Submission.annotator_id,
            func.count(Submission.id),
            func.coalesce(func.sum(per_sub), 0),
            func.min(Submission.submitted_at),
        )
        .join(Submission.task)
        .join(Task.project)
        .where(
            Submission.status == "approved",
            Submission.payment_id.is_(None),
        )
        .group_by(Submission.annotator_id)
    )
    if annotator_id is not None:
        stmt = stmt.where(Submission.annotator_id == annotator_id)
    rows = db.session.execute(stmt).all()
    return {
        row[0]: (row[1],
                 money(row[2] if row[2] is not None else Decimal("0")),
                 row[3])
        for row in rows
    }


def _pending_agg() -> dict[int, tuple[int, float]]:
    """Aggregate generated-but-unpaid payments per annotator.

    Returns:
        Mapping of annotator id to (pending payment count, total USD).
    """
    rows = db.session.execute(
        select(
            Payment.annotator_id,
            func.count(Payment.id),
            func.coalesce(func.sum(Payment.amount), 0.0),
        )
        .where(Payment.status == "pending")
        .group_by(Payment.annotator_id)
    ).all()
    return {
        row[0]: (row[1], money(row[2])) for row in rows
    }


def _last_paid_map() -> dict[int, object]:
    """Map each annotator to the time of their most recent paid payout.

    Returns:
        Mapping of annotator id to their latest paid_at (or absent).
    """
    rows = db.session.execute(
        select(Payment.annotator_id, func.max(Payment.paid_at))
        .where(Payment.status == "paid")
        .group_by(Payment.annotator_id)
    ).all()
    return {row[0]: row[1] for row in rows}


@admin_bp.route("/payments")
@admin_required
def payments():
    """Render the payments dashboard: ledger summary and payout tools.

    For every ACTIVE annotator the page shows their approved-but-unpaid
    work (count and value, USD + Naira), any generated payment still
    pending, and when they were last paid. Deactivated annotators are
    hidden from the ledger — settle their balance before deactivation.

    Returns:
        The rendered payments template.
    """
    summary = {
        "pending_usd": 0.0,
        "paid_month_usd": 0.0,
        "active_annotators": 0,
    }
    earnings_rows: list[dict] = []
    history_rows: list[dict] = []
    history_pagination = None
    try:
        annotators = db.session.scalars(
            select(User)
            .where(User.role == "annotator", User.is_active.is_(True))
            .order_by(User.name)
        ).all()
        summary["active_annotators"] = len(annotators)

        unclaimed = _unclaimed_agg()
        pending = _pending_agg()
        last_paid = _last_paid_map()

        # Grand total owed spans EVERYONE (the per-annotator table below
        # stays active-only): work never yet bundled into a payout, plus
        # payout runs generated but not actually executed. This equals
        # the admin dashboard's ledger metric (approved value minus
        # payments actually marked paid) — hiding a deactivated
        # annotator's balance would understate real obligations.
        summary["pending_usd"] = round(
            sum(v[1] for v in unclaimed.values())
            + sum(v[1] for v in pending.values()),
            2,
        )
        inactive_debt = round(
            sum(
                v[1]
                for aid, v in list(unclaimed.items()) + list(pending.items())
                if aid not in {a.id for a in annotators}
            ),
            2,
        )
        for annotator in annotators:
            count, usd, _oldest = unclaimed.get(
                annotator.id, (0, 0.0, None)
            )
            p_count, p_usd = pending.get(annotator.id, (0, 0.0))
            earnings_rows.append(
                {
                    "annotator": annotator,
                    "approved_count": count,
                    "earnings_usd": usd,
                    "pending_payments": p_count,
                    "pending_usd": p_usd,
                    "last_paid_at": last_paid.get(annotator.id),
                    # Bank details come from the annotator's profile;
                    # a missing account blocks the payout run.
                    "has_bank": bool(annotator.bank_account_number),
                }
            )

        month_start = utc_now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        summary["paid_month_usd"] = money(
            db.session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == "paid",
                    Payment.paid_at >= month_start,
                )
            )
            or 0
        )

        # Payout history, newest first, with batched annotator names.
        history_pagination = db.paginate(
            select(Payment).order_by(Payment.id.desc()),
            page=max(request.args.get("page", 1, type=int) or 1, 1),
            per_page=TASKS_PER_PAGE,
            error_out=False,
        )
        page_payments = list(history_pagination.items)
        payee_ids = {payment.annotator_id for payment in page_payments}
        payee_names: dict[int, str] = {}
        if payee_ids:
            payee_names = {
                uid: name
                for uid, name in db.session.execute(
                    select(User.id, User.name).where(User.id.in_(payee_ids))
                ).all()
            }
        history_rows = [
            {
                "payment": payment,
                "annotator_name": payee_names.get(
                    payment.annotator_id, "—"
                ),
            }
            for payment in page_payments
        ]
    except Exception:
        current_app.logger.exception("Payments dashboard failed.")
        flash("Payments data could not be loaded.", "danger")
        earnings_rows, history_rows, history_pagination = [], [], None

    return render_template(
        "admin/payments.html",
        paystack_enabled=paystack_gateway.is_enabled(
            current_app.config.get("PAYSTACK_SECRET_KEY", "")
        ),
        summary=summary,
        inactive_debt=inactive_debt,
        earnings_rows=earnings_rows,
        history_rows=history_rows,
        history_pagination=history_pagination,
        payment_methods=PAYMENT_METHODS,
    )


@admin_bp.route("/payments/generate", methods=["POST"])
@admin_required
def generate_payments():
    """Bundle unpaid approved work into pending Payment records.

    Runs for every active annotator by default, or a single annotator
    when ``annotator_id`` is posted (the per-row button). Each run
    creates at most ONE open payment per annotator covering ALL their
    unclaimed approved work, and claims those submissions atomically —
    a claim-count mismatch (concurrent run) rolls the whole generation
    back rather than risking a submission being paid twice.

    Returns:
        A redirect back to the payments dashboard with flash feedback.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.payments"))

    target_id = request.form.get("annotator_id", type=int)
    try:
        if target_id:
            target = db.session.get(User, target_id)
            if (
                target is None
                or target.role != "annotator"
                or not target.is_active
            ):
                flash("Choose an active annotator.", "danger")
                return redirect(url_for("admin.payments"))
            targets = [target]
        else:
            targets = list(
                db.session.scalars(
                    select(User)
                    .where(
                        User.role == "annotator", User.is_active.is_(True)
                    )
                    .order_by(User.name)
                ).all()
            )

        agg = _unclaimed_agg()
        now = utc_now()
        created = 0
        total_amount = money(0)
        for annotator in targets:
            count, amount, oldest = agg.get(annotator.id, (0, 0.0, None))
            if not count:
                continue
            payment = Payment(
                annotator_id=annotator.id,
                amount=amount,  # USD, pre-rounded by the aggregation
                currency="USD",
                period_start=oldest,
                period_end=now,
                tasks_count=count,
                status="pending",
            )
            db.session.add(payment)
            db.session.flush()  # assign payment.id for the claims below
            log_event(
                "payment.generate",
                actor=current_user,
                entity_type="payment",
                entity_id=payment.id,
                detail={
                    "annotator_id": annotator.id,
                    "amount_usd": amount,
                    "tasks_count": count,
                },
            )
            claimed = db.session.execute(
                update(Submission)
                .where(
                    Submission.annotator_id == annotator.id,
                    Submission.status == "approved",
                    Submission.payment_id.is_(None),
                )
                .values(payment_id=payment.id)
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != count:
                # Another run claimed some of these rows mid-flight:
                # roll everything back rather than double-cover work.
                raise RuntimeError(
                    "Concurrent payment generation detected"
                    f" (annotator {annotator.id}: expected {count},"
                    f" claimed {claimed})."
                )
            created += 1
            total_amount += amount
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Payment generation failed.")
        flash("Could not generate payments. Please try again.", "danger")
        return redirect(url_for("admin.payments"))

    if not created:
        flash(
            "No approved unpaid submissions to generate payments from.",
            "warning",
        )
        return redirect(url_for("admin.payments"))

    flash(
        f"{created} payment(s) generated totalling"
        f" ${total_amount:.2f}. Mark each as paid after the money"
        " actually goes out.",
        "success",
    )
    return redirect(url_for("admin.payments"))


@admin_bp.route("/payments/<int:payment_id>/mark-paid", methods=["POST"])
@admin_required
def mark_payment_paid(payment_id: int):
    """Record a pending payout as actually paid (POST + CSRF only).

    Stores the channel (payment_method) and the UTC timestamp; both are
    mandatory for 'paid' by the database's audit constraints.

    Args:
        payment_id: Primary key of the payment to settle.

    Returns:
        A redirect back to the payments dashboard with flash feedback.
        404 for unknown ids.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.payments"))

    payment = db.session.get(Payment, payment_id)
    if payment is None:
        abort(404)

    if payment.status != "pending":
        flash(
            f"Payment #{payment_id} is already {payment.status}.",
            "info",
        )
        return redirect(url_for("admin.payments"))

    method = request.form.get("payment_method", "")
    if method not in PAYMENT_METHODS:
        flash("Choose how the payment was sent.", "danger")
        return redirect(url_for("admin.payments"))

    payment.status = "paid"
    payment.payment_method = method
    payment.paid_at = utc_now()
    log_event(
        "payment.mark_paid",
        actor=current_user,
        entity_type="payment",
        entity_id=payment.id,
        detail={
            "annotator_id": payment.annotator_id,
            "amount_usd": payment.amount,
            "payment_method": method,
        },
    )
    recipient = db.session.get(User, payment.annotator_id)
    if recipient is not None:
        notify(
            recipient,
            "payout.paid",
            payload={
                "amount_usd": str(payment.amount),
                "payment_method": method,
            },
            subject="A Brydl payout is on its way to your bank",
            body=(
                "Hello,\n\n"
                f"A payout of ${payment.amount} was sent to your"
                f" account via {method.replace('_', ' ')}.\nIt should"
                " reflect shortly, depending on your bank.\n\n— Brydl"
            ),
        )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Mark-paid failed for payment %s.", payment_id
        )
        flash(
            "Could not mark the payment as paid. Please try again.",
            "danger",
        )
        return redirect(url_for("admin.payments"))

    flash(
        f"Payment #{payment_id} marked paid: ${payment.amount:.2f}"
        f" (₦{usd_to_ngn(payment.amount):.2f}) via"
        f" {method.replace('_', ' ')}.",
        "success",
    )
    return redirect(url_for("admin.payments"))


# ---------------------------------------------------------------------------
# Audit trail browser
# ---------------------------------------------------------------------------

AUDIT_PER_PAGE = PAGINATION_PER_PAGE


@admin_bp.route("/audit")
@auditor_required
def audit_log():
    """Browse the append-only audit trail (newest first, filterable).

    Filters: ?actor=<user id> (exact) and ?q=<action substring> (the
    %/_ LIKE wildcards are stripped). Both are display filters — a
    tampered value yields an empty list, never an error.

    Returns:
        The rendered audit template. Out-of-range pages redirect to the
        last valid page.
    """
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    actor_id = request.args.get("actor", type=int)
    raw_q = (request.args.get("q") or "").strip()
    q = raw_q.replace("%", "").replace("_", "")[:80]

    rows: list = []
    pagination = None
    actors: list[tuple[int, str]] = []
    try:
        actors = sorted(
            (
                (uid, name)
                for uid, name in db.session.execute(
                    select(AuditLog.user_id, AuditLog.actor_name)
                    .where(AuditLog.user_id.is_not(None))
                    .distinct()
                ).all()
            ),
            key=lambda pair: (pair[1] or "").lower(),
        )
        stmt = select(AuditLog).order_by(
            AuditLog.created_at.desc(), AuditLog.id.desc()
        )
        if actor_id:
            stmt = stmt.where(AuditLog.user_id == actor_id)
        if q:
            stmt = stmt.where(AuditLog.action.ilike(f"%{q}%"))
        pagination = db.paginate(
            stmt, page=page, per_page=AUDIT_PER_PAGE, error_out=False
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for("admin.audit_log", page=pagination.pages)
            )
        rows = list(pagination.items)
    except Exception:
        current_app.logger.exception("Audit trail query failed.")
        flash("Audit trail could not be loaded.", "danger")

    return render_template(
        "admin/audit.html",
        rows=rows,
        pagination=pagination,
        actors=actors,
        actor_id=actor_id,
        q=raw_q,
    )


# ---------------------------------------------------------------------------
# Team: user management
# ---------------------------------------------------------------------------


class UserCreateForm(FlaskForm):
    """Create-account form (admins only; email normalised lowercase)."""

    name = StringField(
        "Full name",
        validators=[
            DataRequired(message="Name is required."),
            Length(max=120, message="Name must be 120 characters or less."),
        ],
    )
    email = StringField(
        "Email address",
        filters=[lambda value: value.strip().lower() if value else value],
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email is too long."),
        ],
    )
    role = SelectField(
        "Role",
        choices=[
            ("annotator", "Annotator"),
            ("project_manager", "Project Manager"),
            ("qa", "QA reviewer"),
            ("auditor", "Auditor"),
            ("admin", "Admin"),
        ],
        validators=[DataRequired(message="Choose a role.")],
    )
    submit = SubmitField("Create account")


def _generate_temp_password() -> str:
    """Return a strong one-off password (shown to the admin exactly once).

    Returns:
        A URL-safe random password (~12 characters, ~70 bits of entropy).
    """
    return secrets.token_urlsafe(9)


@admin_bp.route("/users")
@admin_required
def users():
    """List every account with role, status, workload and activity.

    Open-task counts and last-logins come from two grouped queries plus
    the account list — three queries for the whole page regardless of
    team size. Last login is derived from the audit trail
    (``auth.login.success``), so it needs no extra column.

    Returns:
        The rendered team template.
    """
    rows: list[dict] = []
    pagination = None
    try:
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        pagination = db.paginate(
            select(User).order_by(
                User.is_active.desc(), User.role, User.name
            ),
            page=page,
            per_page=PAGINATION_PER_PAGE,
            error_out=False,
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(url_for("admin.users", page=pagination.pages))
        accounts = pagination.items
        open_counts = {
            uid: count
            for uid, count in db.session.execute(
                select(Task.assigned_to, func.count(Task.id))
                .where(
                    Task.assigned_to.is_not(None),
                    Task.status.in_(("assigned", "in_progress", "rejected")),
                )
                .group_by(Task.assigned_to)
            ).all()
        }
        last_logins = {
            uid: latest
            for uid, latest in db.session.execute(
                select(AuditLog.user_id, func.max(AuditLog.created_at))
                .where(
                    AuditLog.action == "auth.login.success",
                    AuditLog.user_id.is_not(None),
                )
                .group_by(AuditLog.user_id)
            ).all()
        }
        rows = [
            {
                "user": account,
                "open_tasks": open_counts.get(account.id, 0),
                "last_login": last_logins.get(account.id),
                # Masked on screen (last 4 digits only); the FULL
                # number lives in the bank-batch payout CSV.
                # last4 column: the masked display NEVER decrypts
                # the number (legacy rows fall back to slicing).
                "bank_masked": (
                    f"****{account.bank_account_last4}"
                    if account.bank_account_last4
                    else (
                        f"****{account.bank_account_number[-4:]}"
                        if account.bank_account_number else None
                    )
                ),
            }
            for account in accounts
        ]
    except Exception:
        current_app.logger.exception("User listing failed.")
        flash("Accounts could not be loaded.", "danger")

    return render_template(
        "admin/users.html",
        rows=rows,
        form=UserCreateForm(),
        pagination=pagination,
    )


@admin_bp.route("/users/create", methods=["POST"])
@admin_required
def create_user():
    """Create an annotator or admin account with a one-off password.

    The temporary password is generated server-side (never chosen, never
    weak) and shown ONCE in the flash message; it is never logged or
    audited. On validation failure the team page re-renders with the
    form's inline errors.

    Returns:
        A redirect to the team page with flash feedback.
    """
    form = UserCreateForm()
    if not form.validate_on_submit():
        if form.csrf_token.errors:
            flash("Security check failed. Please try again.", "warning")
        for field in (form.name, form.email, form.role):
            for error in field.errors:
                flash(error, "danger")
        return redirect(url_for("admin.users"))

    email = form.email.data.strip().lower()
    if db.session.scalar(
        select(func.count(User.id)).where(User.email == email)
    ):
        flash(f"{email} already has an account.", "warning")
        return redirect(url_for("admin.users"))

    temp_password = _generate_temp_password()
    account = User(
        name=form.name.data.strip(),
        email=email,
        role=form.role.data,
        # New annotators must accept the current terms on first sign-in.
        terms_version="",
    )
    account.set_password(temp_password)
    db.session.add(account)
    log_event(
        "user.create",
        actor=current_user,
        entity_type="user",
        detail={"email": email, "role": account.role},
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Account creation failed for %r.", email
        )
        flash("Could not create the account. Please try again.", "danger")
        return redirect(url_for("admin.users"))

    flash(
        f"Account created for {account.name}"
        f" ({account.role}). Temporary password (shown once):"
        f" {temp_password}",
        "success",
    )
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_user_active(user_id: int):
    """Deactivate or reactivate an account (POST + CSRF only).

    Guards: you cannot deactivate yourself, and the last active admin
    cannot be deactivated. Deactivation immediately revokes live
    sessions (the user loader rejects inactive accounts) and hides the
    person from annotator pickers; history and payouts are kept.

    Args:
        user_id: Primary key of the account to toggle.

    Returns:
        A redirect to the team page with flash feedback. 404 for
        unknown ids.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.users"))

    account = db.session.get(User, user_id)
    if account is None:
        abort(404)
    if account.id == current_user.id:
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("admin.users"))
    if (
        account.is_active
        and account.role == "admin"
        and (
            db.session.scalar(
                select(func.count(User.id)).where(
                    User.role == "admin", User.is_active.is_(True)
                )
            )
            or 0
        )
        <= 1
    ):
        flash("At least one active admin must remain.", "warning")
        return redirect(url_for("admin.users"))

    account.is_active = not account.is_active
    log_event(
        "user.deactivate" if not account.is_active else "user.reactivate",
        actor=current_user,
        entity_type="user",
        entity_id=account.id,
        detail={"email": account.email, "role": account.role},
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Account toggle failed for user %s.", user_id
        )
        flash("Could not update the account. Please try again.", "danger")
        return redirect(url_for("admin.users"))

    if account.is_active:
        flash(f"{account.name} reactivated. They can sign in again.",
              "success")
    else:
        flash(
            f"{account.name} deactivated. Their sessions ended"
            " immediately; history and payouts are kept.",
            "success",
        )
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def reset_user_password(user_id: int):
    """Generate a new one-off password for an account (POST + CSRF only).

    The new password is shown ONCE to the acting admin and never stored
    in plain text or logged. Works for inactive accounts too (pre-create
    a password before reactivation).

    Args:
        user_id: Primary key of the account whose password to reset.

    Returns:
        A redirect to the team page with flash feedback. 404 for
        unknown ids.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.users"))

    account = db.session.get(User, user_id)
    if account is None:
        abort(404)

    temp_password = _generate_temp_password()
    account.set_password(temp_password)
    account.auth_version += 1  # revoke their other live sessions
    log_event(
        "user.password_reset",
        actor=current_user,
        entity_type="user",
        entity_id=account.id,
        detail={"email": account.email},
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Password reset failed for user %s.", user_id
        )
        flash("Could not reset the password. Please try again.", "danger")
        return redirect(url_for("admin.users"))

    flash(
        f"Password reset for {account.name}. Temporary password"
        f" (shown once): {temp_password}",
        "success",
    )
    return redirect(url_for("admin.users"))


# ---------------------------------------------------------------------------
# Exports: dataset files + bank-ready payout batches
# ---------------------------------------------------------------------------


def _preference_document(text: str) -> dict | None:
    """Return a preference-pair document when ``text`` encodes one.

    Preference tasks store their input as a JSON object with three
    non-empty string fields: ``prompt``, ``response_a`` and
    ``response_b`` (an optional ``kind`` must be "preference").
    Anything else — plain text task inputs, other JSON documents —
    returns None so callers fall through to legacy behaviour.

    Args:
        text: The stored or pasted input text.

    Returns:
        The parsed dict for a valid preference document, else None.
    """
    stripped = (text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        doc = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("kind") not in (None, "preference"):
        return None
    if not all(
        isinstance(doc.get(key), str) and doc.get(key).strip()
        for key in ("prompt", "response_a", "response_b")
    ):
        return None
    return doc


def _preference_gold(gold: str | None) -> tuple[str | None, str]:
    """Normalize a preference gold answer to its canonical form.

    Args:
        gold: The raw gold cell (expected "a" or "b", any case).

    Returns:
        A tuple of (normalized gold or None, error message). The error
        is empty when the value is acceptable (None gold is allowed —
        it simply means the task has no answer key).
    """
    if gold is None or not gold.strip():
        return None, ""
    normalized = gold.strip().lower()
    if normalized not in ("a", "b"):
        return None, 'Preference gold must be "a" or "b".'
    return normalized, ""


def _build_import_tasks(
    project: Project, rows: list[tuple[str, str | None]]
) -> list[Task]:
    """Turn parsed import rows into tasks, grouping for consensus.

    When the project's required_annotations is greater than 1, every
    row becomes one ConsensusGroup with exactly that many identical
    sibling tasks — blind multi-annotation rounds. Depth 1 behaves
    exactly as before (one plain task per row).

    Args:
        project: The destination project (depth read from it).
        rows: Parsed (input_data, expected_output) pairs.

    Returns:
        The task objects to insert (caller adds + commits them).
    """
    depth = int(project.required_annotations or 1)
    if depth <= 1:
        return [
            Task(project_id=project.id, input_data=inp,
                 expected_output=exp)
            for inp, exp in rows
        ]
    tasks: list[Task] = []
    for inp, exp in rows:
        group = ConsensusGroup(
            project_id=project.id,
            required_count=depth,
            status="pending",
        )
        db.session.add(group)
        db.session.flush()  # assign group.id before creating siblings
        for _sibling in range(depth):
            tasks.append(
                Task(project_id=project.id, input_data=inp,
                     expected_output=exp,
                     consensus_group_id=group.id)
            )
    return tasks


def _parse_stored_json(raw: str) -> dict:
    """Parse a stored JSON text column into a dict for exports.

    Task input and submission output columns hold compact JSON objects
    ({"kind": ..., "text": ...}). If any legacy row ever holds plain
    text, it is wrapped instead of crashing the export.

    Args:
        raw: The stored string.

    Returns:
        The parsed dict, or {"raw": raw} when parsing fails.
    """
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"raw": raw}
    except (ValueError, TypeError):
        return {"raw": raw}


def _export_filename(prefix: str, extension: str) -> str:
    """Build a timestamped, filesystem-safe export filename (UTC)."""
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    return f"brydl-{prefix}-{stamp}.{extension}"


# Spreadsheet formula prefixes: a CSV cell starting with one of these
# is executed by Excel/LibreOffice when the file is opened (=cmd|...).
# Exported cells are neutralized with a leading apostrophe — Excel
# renders it as text. Numbers (Decimal/int) are untouched.
_FORMULA_LEADS = ("=", "+", "-", "@")

# Placeholder so the mirror names survive flake8 (the config class
# attributes are what templates read via `config.*`).
_ = (IMPORT_MAX_ROWS_MIRROR, IMPORT_MAX_BYTES_MIRROR)


def _csv_safe_cell(value):
    """Return a spreadsheet-safe version of a CSV export cell.

    Text cells that would be interpreted as formulas by spreadsheet
    applications get a leading apostrophe (the same defence Excel
    itself applies). Non-string cells pass through unchanged.

    Args:
        value: The raw cell value.

    Returns:
        The safe cell value.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def _csv_response(rows: list[list], headers: list[str],
                  filename: str) -> Response:
    """Render rows as an Excel-friendly CSV download (UTF-8 BOM).

    Every text cell passes through _csv_safe_cell(), so content that
    arrived through task imports (or any user input) can never execute
    as a spreadsheet formula when an admin opens the export.

    Args:
        rows: Data rows (cells are strings/numbers/None).
        headers: The header row.
        filename: Suggested download filename.

    Returns:
        A text/csv Response with an attachment disposition.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(
        [[_csv_safe_cell(cell) for cell in row] for row in rows]
    )
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@admin_bp.route("/projects/<int:project_id>/export",
                methods=["GET", "POST"])
@admin_required
def export_project(project_id: int):
    """Export a project's submissions as a dataset.

    Formats: JSONL, CSV (general purpose), COCO JSON (computer-vision
    schema with tasks as images and labels as categories) and YOLO
    text (a ZIP of classes.txt + per-task label files, full-frame
    placeholders until media tasks carry real images).

    GET renders the options page; POST (CSRF-protected) streams the
    file. Gold answers are excluded unless explicitly requested, the
    scope filter picks which submission statuses are included, rows
    stream in batches (config.EXPORT_YIELD_PER) so memory stays flat,
    and annotators remain pseudonymous (numeric ids only). The export
    event — format, scope, row count, never the data — is audited.

    Args:
        project_id: Primary key of the project to export.

    Returns:
        The options page (GET), the dataset file (POST), or 404 for
        an unknown project.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)

    if request.method == "GET":
        tokens = db.session.scalars(
            select(ClientAccessToken)
            .where(ClientAccessToken.project_id == project.id)
            .order_by(ClientAccessToken.created_at.desc())
        ).all()
        # Current PMs assigned to this project + list of assignable PMs.
        pm_assignments = db.session.scalars(
            select(ProjectAssignment)
            .where(
                ProjectAssignment.project_id == project.id,
                ProjectAssignment.role_in_project == "project_manager",
            )
        ).all()
        available_pms = db.session.scalars(
            select(User).where(
                User.role == "project_manager",
                User.is_active.is_(True),
            ).order_by(User.name)
        ).all()
        return render_template(
            "admin/project_export.html",
            project=project,
            client_tokens=tokens,
            token_form=ClientTokenForm(),
            api_keys=db.session.scalars(
                select(ApiToken)
                .where(ApiToken.project_id == project.id)
                .order_by(ApiToken.id.desc())
            ).all(),
            api_key_form=ApiKeyForm(),
            pm_assignments=pm_assignments,
            available_pms=available_pms,
        )

    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.projects"))

    export_format = request.form.get("format", "jsonl")
    scope = request.form.get("scope", "approved")
    include_gold = request.form.get("include_gold") == "1"
    if export_format not in ("jsonl", "csv", "coco", "yolo"):
        export_format = "jsonl"
    scope_statuses = {
        "approved": ("approved",),
        "reviewed": ("approved", "rejected"),
        "all": None,  # every submission on this project
    }
    statuses = scope_statuses.get(scope, scope_statuses["approved"])

    stmt = (
        select(Submission, Task)
        .join(Submission.task)
        .options(db.joinedload(Task.consensus_group))
        .where(Task.project_id == project.id)
        .order_by(Submission.id.asc())
    )
    if statuses is not None:
        stmt = stmt.where(Submission.status.in_(statuses))
    stream = db.session.execute(
        stmt.execution_options(yield_per=EXPORT_YIELD_PER)
    )

    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    filename = (
        f"brydl-project{project.id}-{scope}-{stamp}.{export_format}"
    )

    if export_format == "jsonl":
        out = io.StringIO()
        count = 0
        for submission, task in stream:
            record = {
                "task_id": submission.task_id,
                "input": _parse_stored_json(task.input_data),
                "output": _parse_stored_json(submission.output_data),
                "status": submission.status,
                "qa_score": submission.qa_score,
                "annotator_id": submission.annotator_id,
                "submitted_at": (
                    submission.submitted_at.isoformat()
                    if submission.submitted_at else None
                ),
                "reviewed_at": (
                    submission.reviewed_at.isoformat()
                    if submission.reviewed_at else None
                ),
                "is_gold": task.expected_output is not None,
                "consensus": (
                    {
                        "status": task.consensus_group.status,
                        "decision": task.consensus_group.decided_choice,
                    }
                    if task.consensus_group_id is not None else None
                ),
            }
            if include_gold and task.expected_output is not None:
                record["gold"] = _parse_stored_json(
                    task.expected_output
                )
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
        body = out.getvalue()
        mimetype = "application/x-ndjson"
    elif export_format == "coco":
        # COCO JSON: tasks are "images" (the file_name pattern future
        # media tasks will keep), approved/submitted answers become
        # annotations, and the distinct labels become categories.
        # Text-era extras (the task text) ride along in "brydl_text"
        # so no information is lost; gold answers never leave the app
        # through this format.
        rows_all = list(stream)
        count = len(rows_all)
        label_set: dict[str, int] = {}
        for _submission, _task in rows_all:
            parsed_out = _parse_stored_json(_submission.output_data)
            label = str(
                parsed_out.get("label", parsed_out.get("kind", "task"))
            )
            label_set.setdefault(label, 0)
        categories = [
            {"id": idx + 1, "name": name}
            for idx, name in enumerate(sorted(label_set))
        ]
        cat_id_by_name = {c["name"]: c["id"] for c in categories}
        images: list[dict] = []
        annotations: list[dict] = []
        ann_id = 0
        for submission, task in rows_all:
            images.append({
                "id": submission.task_id,
                "file_name": f"task_{submission.task_id}.txt",
                "width": 0,
                "height": 0,
                "brydl_text": _parse_stored_json(task.input_data),
            })
            parsed_out = _parse_stored_json(submission.output_data)
            label = str(
                parsed_out.get("label", parsed_out.get("kind", "task"))
            )
            ann_id += 1
            annotations.append({
                "id": ann_id,
                "image_id": submission.task_id,
                "category_id": cat_id_by_name[label],
                "segmentation": [],
                "area": 0.0,
                "bbox": [0.0, 0.0, 0.0, 0.0],
                "iscrowd": 0,
                "attributes": {
                    "status": submission.status,
                    "qa_score": submission.qa_score,
                    "annotator_id": submission.annotator_id,
                },
            })
        body = json.dumps({
            "info": {
                "description": f"Brydl export - {project.name}",
                "version": "1.0",
                "date_created": utc_now().isoformat(),
            },
            "licenses": [],
            "images": images,
            "annotations": annotations,
            "categories": categories,
        }, ensure_ascii=False, indent=2)
        filename = (
            f"brydl-project{project.id}-{scope}-{stamp}.json"
        )
        mimetype = "application/json"
    elif export_format == "yolo":
        # YOLO text: a ZIP of classes.txt plus one label file per
        # task. Text-era labels are mapped to the detection line
        # format with a full-frame placeholder box; a README in the
        # ZIP states the convention so downstream pipelines are never
        # surprised.
        rows_all = list(stream)
        count = len(rows_all)
        label_set: set[str] = set()
        parsed_rows: list[tuple[object, object, str]] = []
        for submission, task in rows_all:
            parsed_out = _parse_stored_json(submission.output_data)
            label = str(
                parsed_out.get("label", parsed_out.get("kind", "task"))
            )
            label_set.add(label)
            parsed_rows.append((submission, task, label))
        ordered = sorted(label_set)
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(
            zip_buffer, "w", zipfile.ZIP_DEFLATED
        ) as zf:
            zf.writestr("classes.txt",
                        "\n".join(ordered) + ("\n" if ordered else ""))
            zf.writestr(
                "README.txt",
                "Brydl YOLO-text export\n"
                "======================\n"
                "One label file per task: <class_index> 0.5 0.5 1.0"
                " 1.0 (full-frame placeholder; text tasks carry no"
                " geometry). Image filenames, when media tasks are"
                " enabled, must be task_<id>.<ext> to match"
                " task_<id>.txt.\n",
            )
            manifest_tasks: list[dict] = []
            by_task: dict[int, list[str]] = {}
            for submission, _task, label in parsed_rows:
                # Full-frame placeholder box until media tasks carry
                # real geometry (documented in the ZIP's README).
                by_task.setdefault(submission.task_id, []).append(
                    f"{ordered.index(label)} 0.5 0.5 1.0 1.0"
                )
            for task_id in sorted(by_task):
                zf.writestr(
                    f"task_{task_id}.txt",
                    "\n".join(by_task[task_id]) + "\n",
                )
                manifest_tasks.append({
                    "task_id": task_id,
                    "labels_file": f"task_{task_id}.txt",
                    "annotations": len(by_task[task_id]),
                })
            zf.writestr(
                "brydl-manifest.json",
                json.dumps({"project": project.name,
                            "tasks": manifest_tasks},
                           ensure_ascii=False, indent=2),
            )
        body = zip_buffer.getvalue()
        filename = (
            f"brydl-project{project.id}-{scope}-{stamp}.zip"
        )
        mimetype = "application/zip"
    else:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "task_id", "status", "qa_score", "annotator_id", "is_gold",
            "submitted_at_utc", "reviewed_at_utc", "input", "output",
            "gold", "consensus_status", "consensus_decision",
        ])
        count = 0
        for submission, task in stream:
            gold = task.expected_output
            writer.writerow([
                submission.task_id,
                submission.status,
                submission.qa_score,
                submission.annotator_id,
                "1" if gold is not None else "0",
                (submission.submitted_at.isoformat()
                 if submission.submitted_at else ""),
                (submission.reviewed_at.isoformat()
                 if submission.reviewed_at else ""),
                json.dumps(_parse_stored_json(task.input_data),
                           ensure_ascii=False),
                json.dumps(_parse_stored_json(submission.output_data),
                           ensure_ascii=False),
                (json.dumps(_parse_stored_json(gold), ensure_ascii=False)
                 if include_gold and gold is not None else ""),
                (task.consensus_group.status
                 if task.consensus_group_id is not None else ""),
                (task.consensus_group.decided_choice or ""
                 if task.consensus_group_id is not None else ""),
            ])
            count += 1
        body = buffer.getvalue()
        mimetype = "text/csv"

    log_event(
        "project.export",
        actor=current_user,
        entity_type="project",
        entity_id=project.id,
        detail={
            "format": export_format,
            "scope": scope,
            "include_gold": include_gold,
            "rows": count,
        },
        commit=True,
    )
    return Response(
        body,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@admin_bp.route("/payments/bank-batch", methods=["POST"])
@admin_required
def export_bank_batch():
    """Download every PENDING payment as a bank-ready batch CSV.

    One row per pending payout with the Naira amount computed at the
    configured display rate (Decimal, exact), a unique reference per
    payment, and the rate stamped on every row so the batch is
    self-documenting. Executing the transfer stays a human job — after
    the bank confirms, each payment is marked paid from the payments
    page as usual. The batch event (count + totals, never personal
    data) is audited.

    Returns:
        The CSV download, or a redirect with a friendly note when
        nothing is pending (or the CSRF check fails).
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.payments"))

    rows_data = db.session.execute(
        select(Payment, User)
        .join(User, Payment.annotator_id == User.id)
        .where(Payment.status == "pending")
        .order_by(Payment.id.asc())
    ).all()
    if not rows_data:
        flash(
            "No pending payments to export — generate a payout run"
            " first.",
            "info",
        )
        return redirect(url_for("admin.payments"))

    rate = current_app.config.get(
        "USD_TO_NGN_RATE", USD_TO_NGN_RATE
    )
    rows: list[list] = []
    total_usd = Decimal("0.00")
    for payment, annotator in rows_data:
        amount_usd = money(payment.amount)
        amount_ngn = money(amount_usd * Decimal(str(rate)))
        total_usd += amount_usd
        rows.append([
            f"BRYDL-PAY-{payment.id}",
            annotator.name,
            annotator.email,
            annotator.bank_name or "",
            annotator.get_bank_account_number() or "",
            annotator.bank_account_name or "",
            f"{amount_ngn:.2f}",
            f"{amount_usd:.2f}",
            f"{rate}",
            payment.tasks_count,
            (f"{payment.period_start:%d %b %Y} - "
             f"{payment.period_end:%d %b %Y}"),
            f"Brydl annotation payout ({payment.tasks_count} tasks)",
        ])

    filename = _export_filename("bank-batch", "csv")
    log_event(
        "payment.export_batch",
        actor=current_user,
        entity_type=None,
        detail={
            "count": len(rows),
            "total_usd": f"{total_usd:.2f}",
            "fx_rate": str(rate),
        },
        commit=True,
    )
    return _csv_response(
        rows,
        ["reference", "recipient_name", "recipient_email",
         "recipient_bank", "recipient_account_number",
         "recipient_account_name",
         "amount_ngn", "amount_usd", "fx_rate", "tasks",
         "period", "narration"],
        filename,
    )


class SftExampleForm(FlaskForm):
    """Create/edit one SFT training pair on the gold-rewrite builder.

    ``exported`` is deliberately absent from the status choices: the
    exported state is terminal and only the export action sets it.
    """

    prompt = TextAreaField(
        "Prompt (user side)",
        validators=[
            DataRequired(message="Write the prompt."),
            Length(
                max=SFT_MAX_TEXT_CHARS,
                message=(
                    f"Prompt must be {SFT_MAX_TEXT_CHARS} characters"
                    " or less."
                ),
            ),
        ],
    )
    completion = TextAreaField(
        "Gold completion (assistant side)",
        validators=[
            DataRequired(message="Write the completion."),
            Length(
                max=SFT_MAX_TEXT_CHARS,
                message=(
                    "Completion must be"
                    f" {SFT_MAX_TEXT_CHARS} characters or less."
                ),
            ),
        ],
    )
    system_prompt = TextAreaField(
        "System message (optional)",
        validators=[
            Optional(),
            Length(
                max=SFT_MAX_TEXT_CHARS,
                message=(
                    "System message must be"
                    f" {SFT_MAX_TEXT_CHARS} characters or less."
                ),
            ),
        ],
    )
    edit_note = TextAreaField(
        "Rewrite note (required when the completion differs from the"
        " harvested source)",
        validators=[
            Optional(),
            Length(
                max=MAX_TEXTAREA_CHARS,
                message=(
                    "Rewrite note must be"
                    f" {MAX_TEXTAREA_CHARS} characters or less."
                ),
            ),
        ],
    )
    status = SelectField(
        "Status",
        choices=[("draft", "Draft"), ("ready", "Ready for export")],
        validators=[DataRequired(), AnyOf(["draft", "ready"])],
    )
    task_id = IntegerField(
        "Source task ID",
        validators=[
            DataRequired(message="A source task ID is required."),
            NumberRange(min=1, message="Enter a valid task ID."),
        ],
    )
    submit = SubmitField("Save pair")


def _sft_prefill(task: Task) -> tuple[str | None, str | None, str]:
    """Harvest the machine-suggested SFT pair for a task.

    Args:
        task: The source task (with its consensus group loaded when
            present).

    Returns:
        A (prompt, source_completion, source) tuple. source is one of
        the models.SFT_SOURCES values; source_completion is None when
        no finalized source yields text (manual entry then).
    """
    prompt = extract_prompt(task.input_data)
    group = task.consensus_group
    approved = db.session.scalar(
        select(Submission)
        .where(
            Submission.task_id == task.id,
            Submission.status == "approved",
        )
        .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        .limit(1)
    )
    completion, source = harvest_completion(
        task.input_data,
        task.expected_output,
        group.status if group else None,
        group.decided_choice if group else None,
        approved.output_data if approved else None,
    )
    return prompt, completion, source


def _sft_note_error(form: SftExampleForm, source_completion: str) -> bool:
    """Enforce the deviation clause on the completion rewrite.

    A saved completion that differs from the harvested
    source_completion REQUIRES a written edit note, on create and on
    every later edit.

    Returns:
        True when the note is missing (caller re-renders the form).
    """
    if form.completion.data.strip() == source_completion.strip():
        return False
    if (form.edit_note.data or "").strip():
        return False
    form.edit_note.errors.append(
        "A written rewrite note is required whenever the completion"
        " differs from the harvested source."
    )
    return True


@admin_bp.route("/sft", endpoint="sft_examples")
@admin_required
def sft_examples():
    """SFT builder list: status/project filters, counts, export panel."""
    project_id = request.args.get("project", type=int)
    if project_id and db.session.get(Project, project_id) is None:
        project_id = None
    status = request.args.get("status", "")
    if status not in SFT_STATUSES:
        status = ""
    page = max(request.args.get("page", 1, type=int) or 1, 1)

    stmt = select(SftExample)
    if project_id:
        stmt = stmt.where(SftExample.project_id == project_id)
    if status:
        stmt = stmt.where(SftExample.status == status)
    stmt = stmt.order_by(SftExample.updated_at.desc(), SftExample.id.desc())

    try:
        pagination = db.paginate(
            stmt, page=page, per_page=SFT_PER_PAGE, error_out=False
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for(
                    "admin.sft_examples",
                    page=pagination.pages,
                    project=project_id,
                    status=status,
                )
            )
        rows = list(pagination.items)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("SFT list query failed.")
        pagination = None
        rows = []

    # Two batched lookups for the page's rows (no per-row N+1).
    project_ids = {row.project_id for row in rows}
    project_names = (
        {
            pid: name
            for pid, name in db.session.execute(
                select(Project.id, Project.name).where(
                    Project.id.in_(project_ids)
                )
            ).all()
        }
        if project_ids
        else {}
    )
    # Status counts for the summary cards (one GROUP BY, all rows).
    try:
        counts = dict(
            db.session.execute(
                select(SftExample.status, func.count(SftExample.id))
                .group_by(SftExample.status)
            ).all()
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception("SFT counts query failed.")
        counts = {}

    projects = db.session.scalars(
        select(Project).order_by(Project.name)
    ).all()

    export_form = SftExportForm()
    export_form.project.choices = [
        ("", "All projects")
    ] + [(str(p.id), p.name) for p in projects]

    return render_template(
        "admin/sft.html",
        rows=rows,
        pagination=pagination,
        project_names=project_names,
        counts=counts,
        filters={"project": project_id, "status": status},
        projects=projects,
        export_form=export_form,
        sft_statuses=SFT_STATUSES,
    )


class SftDeleteForm(FlaskForm):
    """CSRF carrier for the draft-delete action (no fields)."""

    submit = SubmitField("Delete draft")


class SftExportForm(FlaskForm):
    """Export panel on the SFT list (project scope + corpus status)."""

    project = SelectField("Project", choices=[], validators=[Optional()])
    status = SelectField(
        "Export status",
        choices=[("ready", "Ready for export")],
        validators=[DataRequired(), AnyOf(["ready"])],
    )
    submit = SubmitField("Export JSONL")


@admin_bp.route("/sft/export", methods=["POST"], endpoint="sft_export")
@admin_required
def sft_export():
    """Download the ready corpus as SFT JSONL and freeze it exported.

    POST-only: the flip to ``exported`` is a state change, so a GET
    must never trigger it. Exported rows are terminal — the shipped
    file and the database stay provably in sync.
    """
    form = SftExportForm()
    form.project.choices = [("", "All projects")] + [
        (str(p.id), p.name)
        for p in db.session.scalars(
            select(Project).order_by(Project.name)
        ).all()
    ]
    if not form.validate_on_submit():
        flash("Export request was invalid. Nothing exported.", "danger")
        return redirect(url_for("admin.sft_examples"))

    project_raw = form.project.data or ""
    project_id = int(project_raw) if project_raw.isdigit() else None
    if project_id and db.session.get(Project, project_id) is None:
        project_id = None

    stmt = (
        select(SftExample)
        .where(SftExample.status == "ready")
        .order_by(SftExample.id)
        .limit(SFT_EXPORT_MAX_ROWS + 1)
    )
    if project_id:
        stmt = stmt.where(SftExample.project_id == project_id)
    examples = list(db.session.scalars(stmt).all())
    if len(examples) > SFT_EXPORT_MAX_ROWS:
        flash(
            f"More than {SFT_EXPORT_MAX_ROWS} ready pairs matched."
            " Export in smaller scopes (filter by project).",
            "warning",
        )
        return redirect(url_for("admin.sft_examples"))
    if not examples:
        flash("No ready pairs matched. Nothing exported.", "warning")
        return redirect(url_for("admin.sft_examples"))

    body = export_jsonl(
        [
            {
                "id": ex.id,
                "task_id": ex.task_id,
                "project_id": ex.project_id,
                "prompt": ex.prompt,
                "completion": ex.completion,
                "system_prompt": ex.system_prompt,
                "updated_at": (
                    ex.updated_at.isoformat()
                    if ex.updated_at
                    else ""
                ),
            }
            for ex in examples
        ]
    )
    exported_ids = [ex.id for ex in examples]
    scope = f"project#{project_id}" if project_id else "all"
    try:
        for ex in examples:
            ex.status = "exported"
        log_event(
            "sft.export",
            actor=current_user,
            entity_type="sft",
            entity_id=exported_ids[0],
            detail={
                "count": len(exported_ids),
                "scope": scope,
                "ids": exported_ids[:25],
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("SFT export flip failed.")
        flash("Could not finalize the export. Please try again.", "danger")
        return redirect(url_for("admin.sft_examples"))
    flash(
        f"Exported {len(exported_ids)} pair(s); they are now frozen as"
        " exported.",
        "success",
    )
    filename = (
        f"sft-{scope.replace('#', '-')}-"
        f"{utc_now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    )
    payload = Response(body, mimetype="application/x-ndjson")
    payload.headers["Content-Disposition"] = (
        f"attachment; filename={filename}"
    )
    return payload


@admin_bp.route("/sft/new", methods=["GET", "POST"], endpoint="sft_new")
@admin_required
def sft_new():
    """Create an SFT pair, prefilled from its source task.

    Every pair carries provenance: the form's source task ID is
    required and validated, so a pair can never dangle off a deleted
    or nonexistent task. The GET prefills the harvested prompt /
    completion (consensus ruling > approved submission > gold key);
    the POST re-resolves the harvest server-side before saving.
    """
    task = None
    if request.method == "GET":
        task_id = request.args.get("task_id", type=int)
        if task_id:
            task = db.session.get(Task, task_id)
            if task is None:
                abort(404)

    form = SftExampleForm()
    source = "manual"
    source_completion = ""
    if task is not None:
        prompt, harvested, source = _sft_prefill(task)
        source_completion = harvested or ""
        form.task_id.data = task.id
        if request.method == "GET":
            form.prompt.data = prompt or ""
            form.completion.data = source_completion

    if form.validate_on_submit():
        referenced = db.session.get(Task, form.task_id.data)
        if referenced is None:
            # A posted id that does not exist is a probe, not a typo.
            abort(404)
        # Server-side truth: the harvest is recomputed for the saved
        # row regardless of what the browser prefilled.
        _, harvested, harvest_source = _sft_prefill(referenced)
        source = harvest_source
        source_completion = harvested or form.completion.data.strip()
        if _sft_note_error(form, source_completion):
            return render_template(
                "admin/sft_form.html",
                form=form,
                example=None,
                task=referenced,
                source=source,
                source_completion=source_completion,
                prompt_view=(
                    content_view(form.prompt.data)
                    if form.prompt.data.strip()
                    else None
                ),
                completion_view=(
                    content_view(form.completion.data)
                    if form.completion.data.strip()
                    else None
                ),
            )
        example = SftExample(
            task_id=referenced.id,
            project_id=referenced.project_id,
            prompt=form.prompt.data.strip(),
            source_completion=source_completion,
            completion=form.completion.data.strip(),
            system_prompt=(form.system_prompt.data or "").strip() or None,
            edit_note=(form.edit_note.data or "").strip() or None,
            status=form.status.data,
            source=source,
            created_by=current_user.id,
        )
        try:
            db.session.add(example)
            # Flush so the audit row (same transaction) can carry the id.
            db.session.flush()
            log_event(
                "sft.create",
                actor=current_user,
                entity_type="sft",
                entity_id=example.id,
                detail={
                    "example_id": example.id,
                    "task_id": example.task_id,
                    "source": example.source,
                    "status": example.status,
                    "rewritten": (
                        example.completion != example.source_completion
                    ),
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("SFT pair create failed.")
            flash("Could not save the pair. Please try again.", "danger")
            return redirect(url_for("admin.sft_examples"))
        flash("SFT pair created.", "success")
        return redirect(
            url_for("admin.sft_detail", example_id=example.id)
        )

    return render_template(
        "admin/sft_form.html",
        form=form,
        example=None,
        task=task,
        source=source,
        source_completion=source_completion,
        prompt_view=None,
        completion_view=None,
    )


@admin_bp.route(
    "/sft/<int:example_id>", methods=["GET", "POST"], endpoint="sft_detail"
)
@admin_required
def sft_detail(example_id: int):
    """View/edit one SFT pair. Exported pairs are frozen."""
    example = db.session.get(SftExample, example_id)
    if example is None:
        abort(404)
    frozen = example.status == "exported"

    if frozen and request.method == "POST":
        # Exported pairs are terminal: the shipped corpus and the
        # database must stay provably in sync.
        flash(
            "This pair is exported and frozen. It can no longer be"
            " edited.",
            "warning",
        )
        return redirect(url_for("admin.sft_detail", example_id=example.id))

    form = SftExampleForm(obj=example)
    if form.validate_on_submit():
        if _sft_note_error(form, example.source_completion):
            return render_template(
                "admin/sft_form.html",
                form=form,
                example=example,
                task=example.task,
                source=example.source,
                source_completion=example.source_completion,
                prompt_view=content_view(form.prompt.data),
                completion_view=content_view(form.completion.data),
            )
        changed = (
            form.completion.data.strip() != example.completion
            or form.prompt.data.strip() != example.prompt
            or (form.system_prompt.data or "").strip()
            != (example.system_prompt or "")
            or form.status.data != example.status
        )
        if not changed:
            flash("Nothing changed.", "info")
            return redirect(
                url_for("admin.sft_detail", example_id=example.id)
            )
        example.prompt = form.prompt.data.strip()
        example.completion = form.completion.data.strip()
        example.system_prompt = (
            (form.system_prompt.data or "").strip() or None
        )
        example.edit_note = (form.edit_note.data or "").strip() or None
        example.status = form.status.data
        example.updated_at = utc_now()
        try:
            log_event(
                "sft.update",
                actor=current_user,
                entity_type="sft",
                entity_id=example.id,
                detail={
                    "example_id": example.id,
                    "status": example.status,
                    "rewritten": (
                        example.completion != example.source_completion
                    ),
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("SFT pair update failed.")
            flash("Could not save changes. Please try again.", "danger")
            return redirect(
                url_for("admin.sft_detail", example_id=example.id)
            )
        flash("SFT pair updated.", "success")
        return redirect(
            url_for("admin.sft_detail", example_id=example.id)
        )

    return render_template(
        "admin/sft_form.html",
        form=form,
        example=example,
        task=example.task,
        source=example.source,
        source_completion=example.source_completion,
        frozen=frozen,
        prompt_view=content_view(example.prompt),
        completion_view=content_view(example.completion),
    )


@admin_bp.route(
    "/sft/<int:example_id>/delete",
    methods=["POST"],
    endpoint="sft_delete",
)
@admin_required
def sft_delete(example_id: int):
    """Delete a DRAFT pair. Ready/exported pairs are kept (audit)."""
    example = db.session.get(SftExample, example_id)
    if example is None:
        abort(404)
    if example.status != "draft":
        flash(
            "Only draft pairs can be deleted. Set it back to draft"
            " first.",
            "warning",
        )
        return redirect(url_for("admin.sft_detail", example_id=example.id))
    if not SftDeleteForm().validate_on_submit():
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.sft_detail", example_id=example.id))
    try:
        log_event(
            "sft.delete",
            actor=current_user,
            entity_type="sft",
            entity_id=example_id,
            detail={"example_id": example_id},
        )
        db.session.delete(example)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("SFT pair delete failed.")
        flash("Could not delete the pair. Please try again.", "danger")
        return redirect(url_for("admin.sft_examples"))
    flash("Draft pair deleted.", "success")
    return redirect(url_for("admin.sft_examples"))


@admin_bp.route(
    "/payments/<int:payment_id>/paystack", methods=["POST"]
)
@admin_required
def paystack_pay(payment_id: int):
    """Send one pending payout through Paystack Transfers.

    Admin-only, POST + CSRF. The annotator's bank details are
    decrypted ONLY inside this request (the same trust boundary as
    the bank-batch CSV) and are never logged, audited or flashed —
    audit detail carries the reference, the kobo amount and the
    provider status, never an account number. The transfer reference
    is stored so the Paystack webhook can settle the row
    idempotently. With PAYSTACK_SECRET_KEY unset the CSV batch flow
    remains the payout path (this route flashes and redirects).
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.payments"))
    secret = current_app.config.get("PAYSTACK_SECRET_KEY", "")
    base_url = current_app.config.get(
        "PAYSTACK_BASE_URL", "https://api.paystack.co"
    )
    timeout_seconds = int(current_app.config.get(
        "PAYSTACK_TIMEOUT_SECONDS", 15
    ))
    if not paystack_gateway.is_enabled(secret):
        flash(
            "Paystack is not configured. Use the bank batch CSV (set"
            " PAYSTACK_SECRET_KEY to enable transfers).",
            "warning",
        )
        return redirect(url_for("admin.payments"))
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        abort(404)
    if payment.status != "pending":
        flash("Only pending payouts can be sent via Paystack.", "warning")
        return redirect(url_for("admin.payments"))
    annotator = payment.annotator
    account_number = annotator.get_bank_account_number()
    account_name = (annotator.bank_account_name or "").strip()
    bank_name = (annotator.bank_name or "").strip()
    if not account_number or not account_name or not bank_name:
        flash(
            f"{annotator.name}'s bank details are incomplete. Fix them"
            " on the Team page or pay this batch via CSV.",
            "danger",
        )
        return redirect(url_for("admin.payments"))

    # Recipient: cached on the user after the first successful payout.
    recipient_code = annotator.paystack_recipient_code
    recipient_created = False
    if not recipient_code:
        ok, banks = paystack_gateway.list_banks(
            secret, base_url, timeout_seconds
        )
        if not ok:
            flash(f"Paystack bank lookup failed: {banks}", "danger")
            return redirect(url_for("admin.payments"))
        bank_code = paystack_gateway.resolve_bank_code(banks, bank_name)
        if bank_code is None:
            flash(
                f"Bank “{bank_name}” was not recognised by Paystack."
                " Correct the bank name (Team page) or pay via CSV.",
                "danger",
            )
            return redirect(url_for("admin.payments"))
        ok, payload = paystack_gateway.create_recipient(
            secret, account_name, account_number, bank_code,
            base_url, timeout_seconds,
        )
        if not ok:
            flash(f"Paystack recipient creation failed: {payload}",
                  "danger")
            return redirect(url_for("admin.payments"))
        recipient_code = payload
        recipient_created = True

    # Single money boundary: USD (stored, 2dp) -> NGN -> kobo.
    rate = USD_TO_NGN_RATE
    amount_ngn = (payment.amount * rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    amount_kobo = int(
        (amount_ngn * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    reference = (
        f"brydl-p{payment.id}-"
        f"{utc_now().strftime('%Y%m%d%H%M%S')}"
    )
    ok, data = paystack_gateway.initiate_transfer(
        secret, recipient_code, amount_kobo, reference,
        f"Brydl payout for {annotator.name} ({payment.tasks_count}"
        " task(s))",
        base_url, timeout_seconds,
    )
    if not ok:
        try:
            log_event(
                "payout.paystack_failed",
                actor=current_user,
                entity_type="payment",
                entity_id=payment.id,
                detail={"reason": str(data)},
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash(f"Paystack transfer failed: {data}", "danger")
        return redirect(url_for("admin.payments"))

    payment.paystack_reference = reference
    provider_status = str(data.get("status") or "").lower()
    if provider_status == "success":
        payment.status = "paid"
        payment.payment_method = "paystack"
        payment.paid_at = utc_now()
        flash(
            f"Payout #{payment.id} sent: ₦{amount_ngn:,.2f} to"
            f" {annotator.name} (confirmed by Paystack).",
            "success",
        )
    else:
        flash(
            f"Paystack accepted payout #{payment.id} (₦{amount_ngn:,.2f}"
            " to " + annotator.name + "). It is processing and will be"
            " confirmed automatically by the webhook.",
            "info",
        )
    try:
        if recipient_created:
            annotator.paystack_recipient_code = recipient_code
        log_event(
            "payout.paystack",
            actor=current_user,
            entity_type="payment",
            entity_id=payment.id,
            detail={
                "reference": reference,
                "amount_kobo": amount_kobo,
                "provider_status": provider_status,
                "recipient_created": recipient_created,
                "annotator_id": annotator.id,
            },
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Paystack payout bookkeeping failed for payment %s.",
            payment.id,
        )
        flash(
            "The transfer may have gone through but saving its record"
            " failed. Check Paystack before retrying.",
            "danger",
        )
    return redirect(url_for("admin.payments"))


class ClientTokenForm(FlaskForm):
    """Issue a client-portal link for a project (label only)."""

    label = StringField(
        "Who is this link for",
        validators=[
            DataRequired(message="Write a label (client or contact)."),
            Length(
                max=120,
                message="Label must be 120 characters or less.",
            ),
        ],
    )
    submit = SubmitField("Create client link")


@admin_bp.route(
    "/projects/<int:project_id>/client-tokens", methods=["POST"]
)
@admin_required
def create_client_token(project_id: int):
    """Issue a client-portal link: show the raw token exactly once.

    Only the sha256 of the token is stored; the full link flashes
    once for the admin to hand to the client (the same show-once
    contract as temporary passwords) and is audited as an id, never
    as a value.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    form = ClientTokenForm()
    if not form.validate_on_submit():
        flash("Write a label for the client link.", "warning")
        return redirect(url_for("admin.export_project",
                                project_id=project_id))
    raw = portal_tokens.generate_token()
    row = ClientAccessToken(
        project_id=project.id,
        label=form.label.data.strip(),
        token_hash=portal_tokens.hash_token(raw),
        created_by=current_user.id,
    )
    try:
        db.session.add(row)
        db.session.flush()
        log_event(
            "client_portal.create",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={"token_id": row.id, "label": row.label},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Client token creation failed for project %s.", project_id
        )
        flash("Could not create the link. Please try again.", "danger")
        return redirect(url_for("admin.export_project",
                                project_id=project_id))
    link = url_for("client.dashboard", token=raw, _external=True)
    flash(f"Client link created (shown once): {link}", "success")
    return redirect(url_for("admin.export_project",
                            project_id=project_id))


@admin_bp.route(
    "/client-tokens/<int:token_id>/deactivate", methods=["POST"]
)
@admin_required
def deactivate_client_token(token_id: int):
    """Revoke a client link instantly (POST + CSRF, audited)."""
    row = db.session.get(ClientAccessToken, token_id)
    if row is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("admin.export_project",
                                project_id=row.project_id))
    if not row.is_active:
        flash("That link is already deactivated.", "info")
        return redirect(url_for("admin.export_project",
                                project_id=row.project_id))
    row.is_active = False
    try:
        log_event(
            "client_portal.deactivate",
            actor=current_user,
            entity_type="project",
            entity_id=row.project_id,
            detail={"token_id": row.id, "label": row.label},
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Client token deactivation failed for token %s.", token_id
        )
        flash("Could not deactivate the link. Please try again.",
              "danger")
        return redirect(url_for("admin.export_project",
                                project_id=row.project_id))
    flash(f"Client link “{row.label}” deactivated.", "success")
    return redirect(url_for("admin.export_project",
                            project_id=row.project_id))


# Magic-byte signatures for the media allowlist: the browser-declared
# content type is never trusted on its own (Phase 8 hardening).
_MEDIA_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"RIFF", "image", "image/webp"),      # WEBP only (WAVE below)
    (b"RIFF", "audio", "audio/wav"),       # WAVE only (WEBP above)
    (b"OggS", "audio", "audio/ogg"),
    (b"ID3", "audio", "audio/mpeg"),
    (b"\xff\xfb", "audio", "audio/mpeg"),
    (b"\xff\xf3", "audio", "audio/mpeg"),
    (b"\xff\xf2", "audio", "audio/mpeg"),
)


def _sniff_media(head: bytes) -> tuple[str, str] | None:
    """Identify (kind, content_type) from a file's leading bytes.

    Args:
        head: The first bytes of the uploaded file.

    Returns:
        The allowlisted (kind, content_type), or None when the bytes
        match nothing we accept. RIFF containers are disambiguated
        (WEBP vs WAVE) and MP4/M4A via the ftyp box.
    """
    for magic, kind, content_type in _MEDIA_MAGIC:
        if head.startswith(magic):
            if magic == b"RIFF":  # container: check the form type
                if head[8:12] == b"WEBP":
                    return "image", "image/webp"
                if head[8:12] == b"WAVE":
                    return "audio", "audio/wav"
                return None
            return kind, content_type
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "audio", "audio/mp4"
    return None


class MediaUploadForm(FlaskForm):
    """Upload one media file to a project (file only; type sniffed)."""

    file = FileField(
        "Media file",
        validators=[DataRequired(message="Choose a file to upload.")],
    )
    submit = SubmitField("Upload")


@admin_bp.route("/media/<int:asset_id>")
@role_required("admin", "qa", "auditor")
def media_serve(asset_id: int):
    """Serve media bytes to staff (admin / QA / auditor).

    The stored content_type was allowlisted + sniffed at upload, so
    it is safe to send verbatim with nosniff (global header); SVG was
    never accepted, so the payload cannot carry active content.
    """
    asset = db.session.get(MediaAsset, asset_id)
    if asset is None:
        abort(404)
    return Response(
        asset.data,
        mimetype=asset.content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@admin_bp.route("/projects/<int:project_id>/media", methods=["GET", "POST"])
@admin_required
def project_media(project_id: int):
    """Manage a project's media library (list + upload, admin-only).

    Uploads are validated by size knob, magic-byte sniffing and the
    content-type allowlist; identical files reuse the existing row
    (unique project+sha256). The task import paste-in JSON for an
    asset is shown on the list so admins can wire media into tasks
    without guessing ids.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    form = MediaUploadForm()
    if form.validate_on_submit():
        # WTForms >= 3.2 reduces FileField.data to a str repr of the
        # FileStorage, so the bytes are read from request.files (the
        # parsed multipart source) instead; the form still owns CSRF
        # and the presence check.
        storage = request.files.get(form.file.name)
        if storage is None:
            flash("Choose a file to upload.", "danger")
            return redirect(
                url_for("admin.project_media", project_id=project.id)
            )
        raw = storage.read()
        if not raw:
            flash("The uploaded file is empty.", "danger")
        elif len(raw) > MEDIA_MAX_BYTES:
            flash(
                f"File exceeds the {MEDIA_MAX_BYTES // (1024 * 1024)}"
                " MB limit.",
                "danger",
            )
        else:
            sniffed = _sniff_media(raw[:16])
            if sniffed is None:
                flash(
                    "Unsupported file type. Allowed: PNG, JPEG, WebP,"
                    " GIF images and MP3, WAV, OGG, M4A audio.",
                    "danger",
                )
            else:
                kind, content_type = sniffed
                digest = hashlib.sha256(raw).hexdigest()
                existing = db.session.scalar(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project.id,
                        MediaAsset.sha256 == digest,
                    )
                )
                if existing is not None:
                    flash(
                        "That exact file was already uploaded"
                        f" (media #{existing.id}) — reusing it.",
                        "info",
                    )
                    return redirect(
                        url_for("admin.project_media",
                                project_id=project.id)
                    )
                asset = MediaAsset(
                    project_id=project.id,
                    kind=kind,
                    content_type=content_type,
                    filename=storage.filename[:255] or "upload",
                    byte_size=len(raw),
                    sha256=digest,
                    data=raw,
                    uploaded_by=current_user.id,
                )
                try:
                    db.session.add(asset)
                    db.session.flush()
                    log_event(
                        "media.upload",
                        actor=current_user,
                        entity_type="project",
                        entity_id=project.id,
                        detail={
                            "asset_id": asset.id,
                            "kind": kind,
                            "content_type": content_type,
                            "byte_size": len(raw),
                            "sha256": digest[:12],
                        },
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    current_app.logger.exception(
                        "Media upload failed for project %s.",
                        project.id,
                    )
                    flash("Could not save the upload. Please try"
                          " again.", "danger")
                    return redirect(
                        url_for("admin.project_media",
                                project_id=project.id)
                    )
                flash(f"Media #{asset.id} uploaded.", "success")
                return redirect(
                    url_for("admin.project_media", project_id=project.id)
                )
    assets = db.session.scalars(
        select(MediaAsset)
        .where(MediaAsset.project_id == project.id)
        .order_by(MediaAsset.id.desc())
    ).all()
    return render_template(
        "admin/project_media.html",
        project=project,
        assets=assets,
        form=form,
    )


# ===== Qualification quizzes (Phase 9) ================================

class QuizForm(FlaskForm):
    """Author a project's qualification quiz (paste format)."""

    title = StringField(
        "Quiz title",
        validators=[
            DataRequired(message="Give the quiz a title."),
            Length(
                max=200,
                message="Title must be 200 characters or less.",
            ),
        ],
    )
    questions_text = TextAreaField(
        "Questions",
        validators=[DataRequired(message="Write the questions.")],
    )
    pass_score = IntegerField(
        "Pass score (%)",
        default=80,
        validators=[
            DataRequired(message="Set a pass score."),
            NumberRange(min=1, max=100,
                        message="Pass score must be 1-100."),
        ],
    )
    max_attempts = IntegerField(
        "Attempts allowed",
        default=3,
        validators=[
            DataRequired(message="Set the number of attempts."),
            NumberRange(min=1, max=20,
                        message="Attempts must be 1-20."),
        ],
    )
    submit = SubmitField("Save quiz")


def _qualified_annotator_ids(quiz_id: int) -> list[int]:
    """Annotator ids holding a passed or exempt attempt on a quiz.

    Args:
        quiz_id: Primary key of the quiz.

    Returns:
        Distinct annotator ids, smallest first (stable display).
    """
    return list(db.session.scalars(
        select(QuizAttempt.annotator_id)
        .where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.status.in_(("passed", "exempt")),
        )
        .distinct()
        .order_by(QuizAttempt.annotator_id.asc())
    ).all())


def _project_quiz_or_404(
    project_id: int,
) -> tuple[Project | None, QualificationQuiz | None]:
    """Load the project and its (optional) single quiz.

    Args:
        project_id: Primary key of the project.

    Returns:
        (project, quiz); project is None when the id is unknown.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        return None, None
    quiz = db.session.scalar(
        select(QualificationQuiz).where(
            QualificationQuiz.project_id == project.id
        )
    )
    return project, quiz


@admin_bp.route(
    "/projects/<int:project_id>/quiz", methods=["GET", "POST"]
)
@admin_required
def project_quiz(project_id: int):
    """Manage the project's qualification quiz (admin-only).

    GET renders the author form (draft/new), the attempt history and
    the qualified roster. POST saves or updates the quiz while it is
    still a draft — an active quiz never changes under people who are
    mid-attempt (retire first, edit, reactivate).

    Args:
        project_id: Primary key of the project.

    Returns:
        The rendered quiz console or a redirect with flash feedback.
        404 for unknown projects.
    """
    project, quiz = _project_quiz_or_404(project_id)
    if project is None:
        abort(404)
    form = QuizForm()
    if form.validate_on_submit():
        if quiz is not None and quiz.status != "draft":
            flash(
                "Only a draft quiz can be edited — retire it first,"
                " then edit and reactivate.",
                "warning",
            )
            return redirect(
                url_for("admin.project_quiz", project_id=project.id)
            )
        questions, errors = quiz_engine.parse_quiz_text(
            form.questions_text.data
        )
        if errors:
            for message in errors[:6]:
                flash(message, "danger")
            return render_template(
                "admin/project_quiz.html",
                project=project,
                quiz=quiz,
                form=form,
                attempts=_quiz_attempt_rows(quiz),
                roster=_quiz_roster_rows(quiz),
                annotators=db.session.scalars(
                    select(User)
                    .where(
                        User.role == "annotator",
                        User.is_active.is_(True),
                    )
                    .order_by(User.name.asc(), User.id.asc())
                ).all(),
            )
        try:
            if quiz is None:
                quiz = QualificationQuiz(
                    project_id=project.id,
                    created_by=current_user.id,
                )
                db.session.add(quiz)
            quiz.title = form.title.data.strip()
            quiz.questions_json = quiz_engine.questions_payload(
                questions
            )
            quiz.pass_score = int(form.pass_score.data)
            quiz.max_attempts = int(form.max_attempts.data)
            quiz.updated_at = utc_now()
            log_event(
                "quiz.save",
                actor=current_user,
                entity_type="qualification_quiz",
                entity_id=quiz.id,
                detail={
                    "project_id": project.id,
                    "questions": len(questions),
                    "pass_score": quiz.pass_score,
                    "max_attempts": quiz.max_attempts,
                },
            )
            db.session.commit()
            flash(
                f"Quiz saved as a draft ({len(questions)} questions)."
                " Activate it to start gating this project.",
                "success",
            )
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Quiz save failed for project %s.", project.id
            )
            flash("Could not save the quiz. Please try again.",
                  "danger")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    elif request.method == "GET" and quiz is not None:
        form.title.data = quiz.title
        form.pass_score.data = quiz.pass_score
        form.max_attempts.data = quiz.max_attempts
        lines = []
        for question in quiz_engine.load_questions(
            quiz.questions_json
        ):
            lines.append(f"Q: {question['text']}")
            for option in question["options"]:
                marker = "* " if option["correct"] else "- "
                lines.append(f"{marker}{option['text']}")
        form.questions_text.data = "\n".join(lines)
    annotators = db.session.scalars(
        select(User)
        .where(User.role == "annotator", User.is_active.is_(True))
        .order_by(User.name.asc(), User.id.asc())
    ).all()
    return render_template(
        "admin/project_quiz.html",
        project=project,
        quiz=quiz,
        form=form,
        attempts=_quiz_attempt_rows(quiz),
        roster=_quiz_roster_rows(quiz),
        annotators=annotators,
    )


def _quiz_attempt_rows(quiz: QualificationQuiz | None) -> list[dict]:
    """Attempt history rows for the quiz console (zero N+1).

    Args:
        quiz: The quiz (or None).

    Returns:
        Newest-first dicts: annotator name, status, score, when.
    """
    if quiz is None:
        return []
    rows = db.session.execute(
        select(QuizAttempt, User.name)
        .join(User, User.id == QuizAttempt.annotator_id)
        .where(QuizAttempt.quiz_id == quiz.id)
        .order_by(QuizAttempt.id.desc())
        .limit(200)
    ).all()
    return [
        {
            "annotator": name,
            "status": attempt.status,
            "score": attempt.score_percent,
            "created_at": attempt.created_at,
        }
        for attempt, name in rows
    ]


def _quiz_roster_rows(quiz: QualificationQuiz | None) -> list[dict]:
    """Qualified (passed/exempt) roster rows for the quiz console.

    Args:
        quiz: The quiz (or None).

    Returns:
        One row per qualified annotator: name, how they qualified,
        when, and the annotator id for the revoke button.
    """
    if quiz is None:
        return []
    rows = db.session.execute(
        select(QuizAttempt, User.name)
        .join(User, User.id == QuizAttempt.annotator_id)
        .where(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.status.in_(("passed", "exempt")),
        )
        .order_by(QuizAttempt.id.asc())
    ).all()
    seen: set[int] = set()
    roster: list[dict] = []
    for attempt, name in rows:
        if attempt.annotator_id in seen:
            continue
        seen.add(attempt.annotator_id)
        roster.append({
            "annotator_id": attempt.annotator_id,
            "annotator": name,
            "how": ("exempt" if attempt.status == "exempt"
                    else "passed"),
            "created_at": attempt.created_at,
        })
    return roster


@admin_bp.route(
    "/projects/<int:project_id>/quiz/activate", methods=["POST"]
)
@admin_required
def activate_quiz(project_id: int):
    """Activate the project's draft quiz (admin-only, CSRF, audited).

    Activation starts the gate: claim, assignment and the task page
    all demand a passed (or exempt) attempt from here on. Every
    active annotator is notified once so nobody discovers the gate by
    being refused.

    Args:
        project_id: Primary key of the project.

    Returns:
        Redirect back to the quiz console.
    """
    project, quiz = _project_quiz_or_404(project_id)
    if project is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    if quiz is None or quiz.status != "draft":
        flash("Only a draft quiz can be activated.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    if not quiz_engine.load_questions(quiz.questions_json):
        flash("The quiz has no valid questions yet.", "danger")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    try:
        quiz.status = "active"
        quiz.updated_at = utc_now()
        log_event(
            "quiz.activated",
            actor=current_user,
            entity_type="qualification_quiz",
            entity_id=quiz.id,
            detail={"project_id": project.id},
        )
        for annotator in db.session.scalars(
            select(User).where(
                User.role == "annotator", User.is_active.is_(True)
            )
        ).all():
            notify(
                annotator,
                "quiz.activated",
                payload={
                    "project": project.name,
                    "quiz_id": quiz.id,
                },
                subject=(
                    f"Qualification quiz live: {project.name}"
                ),
                body=(
                    "A qualification quiz is now required before you"
                    f" can work on {project.name}. Open your dashboard"
                    " to take it."
                ),
            )
        db.session.commit()
        flash(
            "Quiz activated — this project now requires a passing"
            " score.",
            "success",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Quiz activation failed for project %s.", project.id
        )
        flash("Could not activate the quiz. Please try again.",
              "danger")
    return redirect(url_for("admin.project_quiz", project_id=project.id))


@admin_bp.route(
    "/projects/<int:project_id>/quiz/retire", methods=["POST"]
)
@admin_required
def retire_quiz(project_id: int):
    """Retire the project's active quiz (admin-only, CSRF, audited).

    Retiring lifts the gate (history and qualifications are kept);
    the quiz can be edited as a draft and reactivated later.

    Args:
        project_id: Primary key of the project.

    Returns:
        Redirect back to the quiz console.
    """
    project, quiz = _project_quiz_or_404(project_id)
    if project is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    if quiz is None or quiz.status != "active":
        flash("Only an active quiz can be retired.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    try:
        quiz.status = "retired"
        quiz.updated_at = utc_now()
        log_event(
            "quiz.retired",
            actor=current_user,
            entity_type="qualification_quiz",
            entity_id=quiz.id,
            detail={"project_id": project.id},
        )
        db.session.commit()
        flash("Quiz retired — the gate is lifted.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Quiz retirement failed for project %s.", project.id
        )
        flash("Could not retire the quiz. Please try again.",
              "danger")
    return redirect(url_for("admin.project_quiz", project_id=project.id))


@admin_bp.route(
    "/projects/<int:project_id>/quiz/grant", methods=["POST"]
)
@admin_required
def grant_quiz_exemption(project_id: int):
    """Grant one annotator an audited exemption (admin-only, CSRF).

    The exempt attempt carries no answer sheet — granted_by records
    the admin who vouched. Granting is refused when the annotator
    already qualifies.

    Args:
        project_id: Primary key of the project.

    Returns:
        Redirect back to the quiz console.
    """
    project, quiz = _project_quiz_or_404(project_id)
    if project is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    if quiz is None:
        flash("Save the quiz first.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    annotator_id = request.form.get("annotator_id", type=int)
    annotator = db.session.get(User, annotator_id) if annotator_id \
        else None
    if annotator is None or annotator.role != "annotator":
        flash("Choose an annotator to exempt.", "danger")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    already = db.session.scalar(
        select(QuizAttempt.id).where(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.annotator_id == annotator.id,
            QuizAttempt.status.in_(("passed", "exempt")),
        )
    )
    if already is not None:
        flash(f"{annotator.name} already qualifies.", "info")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    try:
        db.session.add(QuizAttempt(
            quiz_id=quiz.id,
            annotator_id=annotator.id,
            status="exempt",
            granted_by=current_user.id,
        ))
        log_event(
            "quiz.exempt",
            actor=current_user,
            entity_type="qualification_quiz",
            entity_id=quiz.id,
            detail={"project_id": project.id,
                    "annotator_id": annotator.id},
        )
        notify(
            annotator,
            "quiz.exempt",
            payload={"project": project.name},
            subject=f"You are pre-approved for {project.name}",
            body=(
                "An admin pre-approved you for "
                f"{project.name} — no quiz needed."
            ),
        )
        db.session.commit()
        flash(f"{annotator.name} is exempt from this quiz.",
              "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Quiz exemption failed for project %s.", project.id
        )
        flash("Could not grant the exemption. Please try again.",
              "danger")
    return redirect(url_for("admin.project_quiz", project_id=project.id))


@admin_bp.route(
    "/projects/<int:project_id>/quiz/revoke", methods=["POST"]
)
@admin_required
def revoke_quiz_qualification(project_id: int):
    """Revoke one annotator's qualification (admin-only, CSRF).

    Removes their passed/exempt attempts on this quiz (failed history
    is kept) so the gate applies to them again immediately.

    Args:
        project_id: Primary key of the project.

    Returns:
        Redirect back to the quiz console.
    """
    project, quiz = _project_quiz_or_404(project_id)
    if project is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    if quiz is None:
        flash("This project has no quiz.", "warning")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    annotator_id = request.form.get("annotator_id", type=int)
    annotator = db.session.get(User, annotator_id) if annotator_id \
        else None
    if annotator is None:
        flash("Choose an annotator to revoke.", "danger")
        return redirect(
            url_for("admin.project_quiz", project_id=project.id)
        )
    try:
        removed = db.session.execute(
            sa_delete(QuizAttempt).where(
                QuizAttempt.quiz_id == quiz.id,
                QuizAttempt.annotator_id == annotator.id,
                QuizAttempt.status.in_(("passed", "exempt")),
            )
        ).rowcount
        log_event(
            "quiz.revoke",
            actor=current_user,
            entity_type="qualification_quiz",
            entity_id=quiz.id,
            detail={"project_id": project.id,
                    "annotator_id": annotator.id,
                    "removed": removed},
        )
        db.session.commit()
        if removed:
            flash(
                f"{annotator.name} must re-qualify for this project.",
                "success",
            )
        else:
            flash(f"{annotator.name} held no qualification.", "info")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Quiz revoke failed for project %s.", project.id
        )
        flash("Could not revoke. Please try again.", "danger")
    return redirect(url_for("admin.project_quiz", project_id=project.id))


# ===== Machine API keys (Phase 10) ====================================

class ApiKeyForm(FlaskForm):
    """Issue one project-scoped machine API key (label only)."""

    label = StringField(
        "What is this key for",
        validators=[
            DataRequired(message="Write a label (system or contact)."),
            Length(
                max=120,
                message="Label must be 120 characters or less.",
            ),
        ],
    )
    submit = SubmitField("Create API key")


@admin_bp.route(
    "/projects/<int:project_id>/api-keys", methods=["POST"]
)
@admin_required
def create_api_key(project_id: int):
    """Issue a project-scoped API key: show the raw key exactly once.

    Only the sha256 is stored (temp-password contract); the raw key
    flashes once and is audited as a prefix id, never as a value.

    Args:
        project_id: Primary key of the project.

    Returns:
        Redirect back to the project export page.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    form = ApiKeyForm()
    if not form.validate_on_submit():
        for field_errors in form.errors.values():
            for message in field_errors:
                flash(message, "danger")
        return redirect(
            url_for("admin.export_project", project_id=project.id)
        )
    raw = api_keys.generate_key()
    try:
        token_row = ApiToken(
            project_id=project.id,
            label=form.label.data.strip(),
            key_hash=api_keys.hash_key(raw),
            key_prefix=(raw[:11] + "…"),
            created_by=current_user.id,
        )
        db.session.add(token_row)
        log_event(
            "apikey.issue",
            actor=current_user,
            entity_type="api_token",
            entity_id=token_row.id,
            detail={"project_id": project.id,
                    "prefix": token_row.key_prefix},
        )
        db.session.commit()
        flash(
            "API key created — copy it now, it is shown only once:"
            f" {raw}",
            "success",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "API key issue failed for project %s.", project.id
        )
        flash("Could not create the key. Please try again.", "danger")
    return redirect(
        url_for("admin.export_project", project_id=project.id)
    )


@admin_bp.route("/api-keys/<int:token_id>/revoke", methods=["POST"])
@admin_required
def revoke_api_key(token_id: int):
    """Revoke one API key instantly (CSRF, audited).

    Args:
        token_id: Primary key of the ApiToken.

    Returns:
        Redirect back to the owning project's export page.
    """
    token_row = db.session.get(ApiToken, token_id)
    if token_row is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(
            url_for("admin.export_project",
                    project_id=token_row.project_id)
        )
    try:
        if token_row.revoked_at is None:
            token_row.revoked_at = utc_now()
            log_event(
                "apikey.revoke",
                actor=current_user,
                entity_type="api_token",
                entity_id=token_row.id,
                detail={"project_id": token_row.project_id,
                        "prefix": token_row.key_prefix},
            )
            db.session.commit()
            flash("API key revoked — calls with it now get 401.",
                  "success")
        else:
            flash("That key was already revoked.", "info")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "API key revoke failed for token %s.", token_row.id
        )
        flash("Could not revoke the key. Please try again.", "danger")
    return redirect(
        url_for("admin.export_project", project_id=token_row.project_id)
    )


# ======================================================================


# ======================================================================


# ======================================================================
# Phase 11+ — PM approvals, site settings, PM assignments, broadcasts.
# ======================================================================


@admin_bp.route("/approvals")
@admin_required
def approvals():
    """Pending project-approval queue from PMs."""
    page = request.args.get("page", 1, type=int)
    pending = db.paginate(
        select(Project)
        .options(joinedload(Project.creator))
        .where(Project.status == "pending_approval")
        .order_by(Project.created_at.asc()),
        page=page, per_page=20, error_out=False,
    )
    return render_template("admin/approvals.html", pending=pending)


@admin_bp.route("/approvals/<int:project_id>/approve", methods=["POST"])
@admin_required
def approve_project(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(url_for("admin.approvals"))
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    if project.status != "pending_approval":
        flash("That project is not awaiting approval.", "warning")
        return redirect(url_for("admin.approvals"))
    rate_raw = (request.form.get("pay_per_task") or "").strip()
    try:
        from decimal import Decimal as D
        rate = money(D(rate_raw)) if rate_raw else money(project.proposed_pay_per_task or 0)
    except Exception:
        rate = money(project.proposed_pay_per_task or 0)
    if rate <= 0:
        flash("Set a pay rate greater than $0.00 before approving.", "danger")
        return redirect(url_for("admin.approvals"))
    project.pay_per_task = rate
    project.proposed_pay_per_task = None
    project.approved_by = current_user.id
    project.approved_at = utc_now()
    project.status = "active"
    project.approval_note = None
    notify(
        project.creator,
        "project.approved",
        payload={"project_id": project.id, "name": project.name, "pay_per_task": str(rate)},
    )
    log_event(
        "admin.project.approve",
        actor=current_user,
        entity_type="project",
        entity_id=project.id,
        detail={"pay_per_task": str(rate)},
    )
    db.session.commit()
    flash(f'Project "{project.name}" approved and is now live.', "success")
    return redirect(url_for("admin.approvals"))


@admin_bp.route("/approvals/<int:project_id>/reject", methods=["POST"])
@admin_required
def reject_project(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(url_for("admin.approvals"))
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    note = (request.form.get("note") or "").strip()
    if not note:
        flash("Include a reason so the PM knows what to fix.", "danger")
        return redirect(url_for("admin.approvals"))
    project.status = "rejected"
    project.approval_note = note[:1000]
    project.proposed_pay_per_task = None
    notify(
        project.creator,
        "project.rejected",
        payload={"project_id": project.id, "name": project.name, "note": note[:300]},
    )
    log_event(
        "admin.project.reject",
        actor=current_user,
        entity_type="project",
        entity_id=project.id,
        detail={"note": note[:300]},
    )
    db.session.commit()
    flash("Project returned to PM with feedback.", "info")
    return redirect(url_for("admin.approvals"))


@admin_bp.route("/projects/<int:project_id>/assign-pm", methods=["POST"])
@admin_required
def assign_pm(project_id: int):
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(url_for("admin.export_project", project_id=project_id))
    pm_id = request.form.get("pm_id", type=int)
    pm = db.session.get(User, pm_id) if pm_id else None
    if pm is None or pm.role != "project_manager" or not pm.is_active:
        flash("Choose an active Project Manager.", "danger")
        return redirect(url_for("admin.export_project", project_id=project_id))
    existing = db.session.scalar(
        select(ProjectAssignment).where(
            ProjectAssignment.project_id == project.id,
            ProjectAssignment.user_id == pm.id,
        )
    )
    if existing is None:
        db.session.add(
            ProjectAssignment(
                project_id=project.id,
                user_id=pm.id,
                role_in_project="project_manager",
                assigned_by=current_user.id,
            )
        )
        notify(
            pm,
            "pm.assigned",
            payload={"project_id": project.id, "name": project.name},
        )
        log_event(
            "admin.pm.assign", actor=current_user,
            entity_type="project", entity_id=project.id,
            detail={"pm_id": pm.id},
        )
        db.session.commit()
        flash(f"{pm.name} assigned to project.", "success")
    else:
        flash(f"{pm.name} is already assigned.", "info")
    return redirect(url_for("admin.export_project", project_id=project_id))


@admin_bp.route("/projects/<int:project_id>/unassign-pm", methods=["POST"])
@admin_required
def unassign_pm(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(url_for("admin.projects"))
    assignment_id = request.form.get("assignment_id", type=int)
    a = db.session.get(ProjectAssignment, assignment_id)
    if a is None or a.project_id != project_id:
        abort(404)
    log_event(
        "admin.pm.unassign", actor=current_user,
        entity_type="project", entity_id=a.project_id,
        detail={"pm_id": a.user_id},
    )
    db.session.delete(a)
    db.session.commit()
    flash("Project Manager removed.", "info")
    return redirect(url_for("admin.export_project", project_id=project_id))


# ----- Site settings -------------------------------------------------------
_SETTING_KEYS = [
    ("usd_to_ngn_rate", "USD → Naira rate", "decimal",
     "Display conversion rate (does not change stored USD values).", "1600"),
    ("site_name", "Site name", "string", "Brand label in the top nav.", "Brydl Dashboard"),
    ("support_email", "Support email", "string",
     "Email shown to staff for urgent contact.", "support@brydl.ng"),
    ("pagination_per_page", "Rows per list page", "integer",
     "Default list pagination.", "20"),
    ("upload_batch_limit", "Max tasks per CSV upload", "integer",
     "Cap on a single CSV paste / file upload.", "500"),
    ("claim_daily_cap", "Daily self-claim cap", "integer",
     "Max tasks an annotator may self-claim per UTC day.", "20"),
    ("task_max_rejections", "Rejections before revision", "integer",
     "Rejections before a task routes to admin/QA.", "3"),
    ("preference_min_justification", "Min preference-justification chars",
     "integer",
     "Minimum characters for a preference-ranking reason.", "40"),
    ("api_rate_limit_per_minute", "API calls/min per key", "integer",
     "Per-key rate limit on /api/v1.", "60"),
]


def _coerce_setting(raw: str, kind: str):
    if kind == "integer":
        return int(str(raw).strip())
    if kind == "decimal":
        from decimal import Decimal as D
        return D(str(raw).strip())
    return str(raw)


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def site_settings():
    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please reload and try again.", "warning")
            return redirect(url_for("admin.site_settings"))
        for key, label, kind, desc, default in _SETTING_KEYS:
            raw = (request.form.get(key) or "").strip()
            if not raw:
                raw = default
            try:
                val = _coerce_setting(raw, kind)
            except Exception:
                flash(f"Invalid value for {label}.", "danger")
                return redirect(url_for("admin.site_settings"))
            # Bounds-check numeric knobs so they can't set DoS-inducing values.
            if kind == "integer":
                if val < 1 or val > 100000:
                    flash(f"{label} must be between 1 and 100000.", "danger")
                    return redirect(url_for("admin.site_settings"))
            if kind == "decimal":
                from decimal import Decimal as D
                if val <= D("0") or val > D("1000000"):
                    flash(f"{label} must be positive and below 1,000,000.", "danger")
                    return redirect(url_for("admin.site_settings"))
            if kind == "string" and key.endswith("_email"):
                import re as _re
                if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw):
                    flash(f"{label} must be a valid email address.", "danger")
                    return redirect(url_for("admin.site_settings"))
            row = db.session.get(SiteSetting, key)
            old = row.value if row else None
            if row is None:
                row = SiteSetting(key=key, value=raw, description=desc)
                db.session.add(row)
            else:
                row.value = raw
                row.description = desc
            if old != raw:
                log_event(
                    "settings.update", actor=current_user,
                    detail={"key": key, "old": old, "new": raw},
                )
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.site_settings"))
    rows = {}
    for key, label, kind, desc, default in _SETTING_KEYS:
        row = db.session.get(SiteSetting, key)
        rows[key] = {
            "label": label, "kind": kind, "desc": desc,
            "value": (row.value if row else default), "default": default,
        }
    return render_template("admin/settings.html", rows=rows)


# ----- Broadcasts ---------------------------------------------------------


@admin_bp.route("/messages/broadcast", methods=["POST"])
@admin_required
def send_broadcast():
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(url_for("staff.feedback_inbox"))
    subject = (request.form.get("subject") or "").strip()[:200]
    body = (request.form.get("body") or "").strip()[:4000]
    if not subject or not body:
        flash("Subject and body are required.", "danger")
        return redirect(url_for("staff.feedback_inbox"))
    thread = FeedbackThread(
        kind="broadcast",
        subject=subject,
        body=body,
        author_id=current_user.id,
        status="new",
    )
    db.session.add(thread)
    db.session.flush()
    all_users = db.session.scalars(
        select(User).where(User.is_active.is_(True))
    ).all()
    for u in all_users:
        if u.id == current_user.id:
            continue
        notify(
            u,
            "broadcast.new",
            payload={"thread_id": thread.id, "subject": subject},
        )
    log_event(
        "broadcast.send", actor=current_user,
        detail={"subject": subject, "recipients": len(all_users) - 1},
    )
    db.session.commit()
    flash(f"Broadcast sent to {len(all_users) - 1} users.", "success")
    return redirect(url_for("staff.feedback_inbox"))
