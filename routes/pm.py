"""PM (Project Manager) workspace.

/project-manager-only blueprint at /pm/. PMs run assigned projects:
they can edit instructions/deadline/rubric, upload/assign/export tasks,
see submissions (read-only) and the project roster, manage media, edit
draft quizzes, and create client-portal links. They CANNOT:
- touch staff, payouts, secrets, audit log, API keys
- activate/retire quizzes (admin-only gate change)
- approve/reject submissions (QA role)
- change pay_per_task (set at admin approval)
- delete projects
"""

import csv
import io


from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from flask_wtf import FlaskForm
from flask_wtf.csrf import validate_csrf, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from wtforms import (
    DateField,
    HiddenField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, NumberRange, Optional
from wtforms.fields import DecimalField
from decimal import Decimal

from audit import log_event
from notifications import notify
from config import (
    PAGINATION_PER_PAGE,
    UPLOAD_BATCH_LIMIT as UPLOAD_BATCH_LIMIT_CFG,
    MAX_TEXTAREA_CHARS,
    MAX_RUBRIC_DIMENSIONS,
    MAX_RUBRIC_DIM_NAME,
)
import client_portal as portal_tokens
from extensions import db
from models import (
    ClientAccessToken,
    MediaAsset,
    Notification,
    Project,
    ProjectAssignment,
    QualificationQuiz,
    QuizAttempt,
    Submission,
    TASK_STATUSES,
    TASK_TYPES,
    Task,
    User,
    utc_now,
)
from models import money
from routes import (
    pm_can_access_project,
    pm_required,
)
from rubric import parse_dimensions

pm_bp = Blueprint("pm", __name__, url_prefix="/pm")

PM_PER_PAGE = PAGINATION_PER_PAGE


# ---------- Forms ----------------------------------------------------------


class PMProjectCreateForm(FlaskForm):
    """PM project-creation form (proposal). pay_per_task becomes
    proposed_pay_per_task; admin finalises it at approval."""

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
            Length(max=200, message="Client name must be 200 characters or less."),
        ],
    )
    task_type = SelectField(
        "Task type",
        choices=[(v, v.replace("_", " ").title()) for v in TASK_TYPES],
        validators=[DataRequired(message="Choose a task type.")],
    )
    proposed_pay_per_task = DecimalField(
        "Proposed pay per task (USD)",
        places=2,
        validators=[
            DataRequired(message="Proposed pay is required."),
            NumberRange(
                min=Decimal("0.01"),
                max=Decimal("10000"),
                message="Pay per task must be greater than 0.",
            ),
        ],
        description="Admin will confirm or revise this before going live.",
    )
    instructions = TextAreaField(
        "Annotator instructions",
        validators=[
            DataRequired(message="Instructions are required."),
            Length(max=MAX_TEXTAREA_CHARS),
        ],
    )
    rubric_dimensions = TextAreaField(
        "Rubric dimensions (optional, one per line)",
        validators=[Optional(), Length(max=MAX_TEXTAREA_CHARS)],
    )
    required_annotations = SelectField(
        "Annotations per task (consensus)",
        choices=[
            (1, "1 — single annotation"),
            (2, "2 — blind consensus (RLHF)"),
            (3, "3 — blind consensus with arbitration"),
        ],
        coerce=int,
        validators=[DataRequired()],
    )
    deadline = DateField("Deadline (optional)", validators=[Optional()])
    submit = SubmitField("Submit for approval")


class PMProjectEditForm(FlaskForm):
    """PM can edit instructions/deadline/rubric/status (pause/resume) but
    NOT pay_per_task. Activate-from-draft is also restricted: PM can
    only submit for approval."""

    name = StringField(
        "Project name",
        validators=[DataRequired(), Length(max=200)],
    )
    client_name = StringField(
        "Client name",
        validators=[DataRequired(), Length(max=200)],
    )
    instructions = TextAreaField(
        "Annotator instructions",
        validators=[DataRequired(), Length(max=MAX_TEXTAREA_CHARS)],
    )
    rubric_dimensions = TextAreaField(
        "Rubric dimensions (one per line)",
        validators=[Optional(), Length(max=MAX_TEXTAREA_CHARS)],
    )
    required_annotations = SelectField(
        "Annotations per task (consensus)",
        choices=[(1, "1"), (2, "2"), (3, "3")],
        coerce=int,
        validators=[DataRequired()],
    )
    deadline = DateField("Deadline (optional)", validators=[Optional()])
    # Pause/resume only — only active projects can be paused, only paused
    # can be resumed. draft/pending/rejected cannot be toggled here.
    status_action = HiddenField("status_action", default="")
    submit = SubmitField("Save changes")


class TaskPasteForm(FlaskForm):
    tasks_data = TextAreaField(
        "Tasks (one per line — gold answer after TAB is optional)",
        validators=[
            DataRequired(message="Paste at least one task."),
            Length(max=200000),
        ],
    )
    submit = SubmitField("Upload tasks")


class FeedbackForm(FlaskForm):
    subject = StringField(
        "Subject",
        validators=[DataRequired(message="Enter a subject."), Length(max=200)],
    )
    body = TextAreaField(
        "Your message",
        validators=[
            DataRequired(message="Write your message."),
            Length(max=4000),
        ],
    )
    submit = SubmitField("Send to admin")


class ReplyForm(FlaskForm):
    body = TextAreaField(
        "Reply",
        validators=[DataRequired(message="Reply cannot be empty."), Length(max=4000)],
    )
    submit = SubmitField("Send reply")


# ---------- Helpers --------------------------------------------------------


def _pm_projects_query():
    """Return a select() over projects assigned to the current PM.

    Admins see everything (for fire-fighting). PMs see only their
    assigned projects plus projects they created that are still in
    draft/pending_approval (so they can follow their own proposal even
    before admin formally assigns them).
    """
    if current_user.role == "admin":
        return select(Project)
    return select(Project).where(
        (Project.id.in_(
            select(ProjectAssignment.project_id).where(
                ProjectAssignment.user_id == current_user.id
            )
        ))
        | (
            (Project.created_by == current_user.id)
            & (Project.status.in_(("draft", "pending_approval", "rejected")))
        )
    )


def _load_pm_project(project_id: int) -> Project:
    """Load a project or 404; 403 if current PM isn't assigned."""
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    if not pm_can_access_project(project):
        # PMs can always see their own proposals even before assignment.
        is_own_proposal = (
            current_user.role == "project_manager"
            and project.created_by == current_user.id
            and project.status in ("draft", "pending_approval", "rejected")
        )
        if not is_own_proposal:
            abort(403)
    return project


def _can_edit_project(project: Project) -> bool:
    """Can PM edit this project? (active/paused/draft/rejected; not pending_approval locked)."""
    if current_user.role == "admin":
        return True
    return project.status in ("draft", "active", "paused", "rejected")


def _project_stats(project: Project) -> dict:
    """Aggregate task counts for the project detail header."""
    totals = db.session.execute(
        select(
            func.count(Task.id).label("total"),
            func.coalesce(
                func.sum(
                    (Task.status == "pending").cast(db.Integer)
                ),
                0,
            ).label("pending"),
            func.coalesce(
                func.sum(
                    Task.status.in_(("assigned", "in_progress")).cast(db.Integer)
                ),
                0,
            ).label("open"),
            func.coalesce(
                func.sum((Task.status == "submitted").cast(db.Integer)),
                0,
            ).label("submitted"),
            func.coalesce(
                func.sum((Task.status == "approved").cast(db.Integer)),
                0,
            ).label("approved"),
            func.coalesce(
                func.sum((Task.status == "rejected").cast(db.Integer)),
                0,
            ).label("rejected"),
        ).where(Task.project_id == project.id)
    ).one()
    total, pending, open_, submitted, approved, rejected = totals
    return {
        "total": total or 0,
        "pending": pending or 0,
        "open": open_ or 0,
        "submitted": submitted or 0,
        "approved": approved or 0,
        "rejected": rejected or 0,
        "pct": round((approved / total) * 100, 1) if total else 0.0,
    }


def _parse_task_lines(lines: str, project: Project) -> tuple[list[Task], list[str]]:
    """Parse one-task-per-line text (TAB-separated optional gold)."""
    new_tasks = []
    errors = []
    for i, raw in enumerate(lines.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if "\t" in line:
            text, gold = line.split("\t", 1)
            text, gold = text.strip(), gold.strip()
        else:
            text, gold = line, None
        if len(text) < 2 or len(text) > 10000:
            errors.append(f"Line {i}: text length must be 2-10000 chars.")
            continue
        new_tasks.append(
            Task(
                project_id=project.id,
                input_data=text,
                expected_output=gold,
                status="pending",
            )
        )
    return new_tasks, errors


# ---------- Dashboard ------------------------------------------------------


@pm_bp.route("/")
@pm_required
def dashboard():
    """PM dashboard — my projects, headline numbers, open feedback, action items.

    Aggregates are computed in a single grouped query per status so there is
    no N+1 and the page stays snappy even with 50+ assigned projects.
    """
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    projects_pagination = db.paginate(
        _pm_projects_query()
        .options(joinedload(Project.creator))
        .order_by(Project.created_at.desc()),
        page=page,
        per_page=PM_PER_PAGE,
        error_out=False,
    )
    project_ids = [p.id for p in projects_pagination.items]
    project_rows = list(projects_pagination.items)

    # ---- Aggregate counts (single queries, never N+1) ----
    active_count = sum(1 for p in project_rows if p.status == "active")
    pending_approval = sum(1 for p in project_rows if p.status == "pending_approval")
    rejected_count = sum(1 for p in project_rows if p.status == "rejected")
    paused_count = sum(1 for p in project_rows if p.status == "paused")

    # Per-project task-status breakdown, plus annotator assignment count.
    per_project_stats: dict[int, dict] = {}
    if project_ids:
        status_rows = db.session.execute(
            select(Task.project_id, Task.status, func.count(Task.id))
            .where(Task.project_id.in_(project_ids))
            .group_by(Task.project_id, Task.status)
        ).all()
        for pid_, status_, cnt in status_rows:
            s = per_project_stats.setdefault(pid_, {
                "total": 0, "pending": 0, "assigned": 0, "submitted": 0,
                "approved": 0, "rejected": 0, "needs_review": 0,
            })
            s[status_] = s.get(status_, 0) + cnt
            s["total"] += cnt
        ann_counts = db.session.execute(
            select(ProjectAssignment.project_id, func.count(ProjectAssignment.id))
            .where(
                ProjectAssignment.project_id.in_(project_ids),
                ProjectAssignment.role_in_project != "project_manager",
            )
            .group_by(ProjectAssignment.project_id)
        ).all()
        ann_map = {pid_: cnt for pid_, cnt in ann_counts}
        for pid_ in project_ids:
            per_project_stats.setdefault(pid_, {
                "total": 0, "pending": 0, "assigned": 0, "submitted": 0,
                "approved": 0, "rejected": 0, "needs_review": 0,
            })["ann_count"] = ann_map.get(pid_, 0)
        # Earned-so-far: sum of pay_per_task * approved_tasks (display estimate).
        # Stays in Python Decimal; never converted to float.
        for p in project_rows:
            s = per_project_stats.get(p.id, {})
            approved_n = s.get("approved", 0)
            rate = p.pay_per_task or 0
            s["earned_usd"] = money(rate) * approved_n if approved_n else money(0)
            pct = 0
            if s.get("total", 0):
                pct = int(round(100 * s.get("approved", 0) / s["total"]))
            s["pct_complete"] = min(pct, 100)
    else:
        for p in project_rows:
            per_project_stats[p.id] = {
                "total": 0, "pending": 0, "assigned": 0, "submitted": 0,
                "approved": 0, "rejected": 0, "needs_review": 0,
                "ann_count": 0, "earned_usd": money(0), "pct_complete": 0,
            }

    # Totals across ALL assigned projects (not just current page) — used for
    # the KPI tiles at the top. Two aggregate queries; never N+1.
    all_pids: list[int] = []
    proj_pay: dict[int, object] = {}
    for p in db.session.scalars(_pm_projects_query()).all():
        all_pids.append(p.id)
        proj_pay[p.id] = money(p.pay_per_task or 0)
    totals = {"total_tasks": 0, "total_approved": 0, "total_pending": 0,
              "total_submitted": 0, "total_rejected": 0, "total_earned": money(0)}
    if all_pids:
        t_rows = db.session.execute(
            select(Task.project_id, Task.status, func.count(Task.id))
            .where(Task.project_id.in_(all_pids))
            .group_by(Task.project_id, Task.status)
        ).all()
        for pid_, status_, cnt in t_rows:
            totals["total_tasks"] += cnt
            if status_ == "approved":
                totals["total_approved"] += cnt
                totals["total_earned"] += proj_pay.get(pid_, money(0)) * cnt
            elif status_ == "pending":
                totals["total_pending"] += cnt
            elif status_ == "submitted":
                totals["total_submitted"] += cnt
            elif status_ == "rejected":
                totals["total_rejected"] += cnt

    # ---- Awaiting action widget ----
    action_items: list[dict] = []
    for p in project_rows:
        if p.status == "rejected":
            action_items.append({
                "kind": "rejected", "project": p,
                "label": "Returned with note",
                "url": url_for("pm.project_detail", project_id=p.id),
            })
        elif p.status == "pending_approval":
            action_items.append({
                "kind": "pending", "project": p,
                "label": "Awaiting admin approval",
                "url": url_for("pm.project_detail", project_id=p.id),
            })
        s = per_project_stats.get(p.id, {})
        if s.get("rejected", 0) > 0:
            action_items.append({
                "kind": "rework", "project": p,
                "label": f"{s['rejected']} task(s) need revision",
                "url": url_for("pm.tasks", project_id=p.id, status="rejected"),
            })
    action_items = action_items[:8]

    from models import FeedbackThread
    my_feedback = db.session.scalars(
        select(FeedbackThread)
        .where(FeedbackThread.author_id == current_user.id)
        .order_by(FeedbackThread.created_at.desc())
        .limit(5)
    ).all()
    unread_bell = db.session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == current_user.id,
            Notification.read_at.is_(None),
        )
    ) or 0
    return render_template(
        "pm/dashboard.html",
        projects=project_rows,
        pagination=projects_pagination,
        stats=per_project_stats,
        totals=totals,
        active_count=active_count,
        pending_approval=pending_approval,
        rejected_count=rejected_count,
        paused_count=paused_count,
        my_feedback=my_feedback,
        action_items=action_items,
        unread_notifications=unread_bell,
    )


# ---------- Projects list --------------------------------------------------


@pm_bp.route("/projects")
@pm_required
def projects():
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    pagination = db.paginate(
        _pm_projects_query().order_by(Project.created_at.desc()),
        page=page,
        per_page=PM_PER_PAGE,
        error_out=False,
    )
    return render_template(
        "pm/projects.html",
        pagination=pagination,
        projects=pagination.items,
    )


@pm_bp.route("/projects/new", methods=["GET", "POST"])
@pm_required
def new_project():
    """PM drafts a project and submits it for admin approval."""
    form = PMProjectCreateForm()
    if form.validate_on_submit():
        rubric_dims, problems = parse_dimensions(
            form.rubric_dimensions.data,
            MAX_RUBRIC_DIMENSIONS,
            MAX_RUBRIC_DIM_NAME,
        )
        if problems:
            for p in problems:
                flash(p, "danger")
            return render_template(
                "pm/project_form.html",
                form=form, is_new=True,
                form_action=url_for("pm.new_project"),
            )
        project = Project(
            name=form.name.data.strip(),
            client_name=form.client_name.data.strip(),
            task_type=form.task_type.data,
            pay_per_task=None,
            proposed_pay_per_task=money(form.proposed_pay_per_task.data),
            instructions=form.instructions.data.strip(),
            rubric_dimensions=("\n".join(rubric_dims) if rubric_dims else None),
            deadline=form.deadline.data,
            required_annotations=form.required_annotations.data,
            created_by=current_user.id,
            status="draft",
        )
        db.session.add(project)
        # Auto-assign the creating PM so they land in the project.
        db.session.flush()
        db.session.add(
            ProjectAssignment(
                project_id=project.id,
                user_id=current_user.id,
                role_in_project="project_manager",
                assigned_by=current_user.id,
            )
        )
        log_event(
            "pm.project.draft",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={"name": project.name, "proposed_pay": str(project.proposed_pay_per_task)},
        )
        db.session.commit()
        flash(f'Project "{project.name}" saved as draft.', "success")
        return redirect(url_for("pm.project_detail", project_id=project.id))
    return render_template(
        "pm/project_form.html",
        form=form, is_new=True,
        form_action=url_for("pm.new_project"),
    )


@pm_bp.route("/projects/<int:project_id>/submit", methods=["POST"])
@pm_required
def submit_for_approval(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(request.referrer or url_for("pm.dashboard"))
    project = _load_pm_project(project_id)
    if project.status not in ("draft", "rejected"):
        flash("This project is already submitted or live.", "warning")
        return redirect(url_for("pm.project_detail", project_id=project.id))
    project.status = "pending_approval"
    project.approval_note = None
    # Notify every admin.
    admins = db.session.scalars(
        select(User).where(User.role == "admin", User.is_active.is_(True))
    ).all()
    for a in admins:
        notify(
            a,
            "project.pending_approval",
            payload={"project_id": project.id, "name": project.name, "by": current_user.name},
        )
    log_event(
        "pm.project.submit",
        actor=current_user,
        entity_type="project",
        entity_id=project.id,
        detail={"status": "pending_approval"},
    )
    db.session.commit()
    flash("Project submitted to admin for approval.", "success")
    return redirect(url_for("pm.project_detail", project_id=project.id))


@pm_bp.route("/projects/<int:project_id>")
@pm_required
def project_detail(project_id: int):
    project = _load_pm_project(project_id)
    stats = _project_stats(project)
    pms = [a.user for a in project.assignments if a.role_in_project == "project_manager"]
    client_tokens = db.session.scalars(
        select(ClientAccessToken)
        .where(ClientAccessToken.project_id == project.id)
        .order_by(ClientAccessToken.created_at.desc())
    ).all()
    return render_template(
        "pm/project_detail.html",
        project=project,
        stats=stats,
        pms=pms,
        client_tokens=client_tokens,
        can_edit=_can_edit_project(project),
    )


@pm_bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@pm_required
def edit_project(project_id: int):
    project = _load_pm_project(project_id)
    if not _can_edit_project(project):
        flash(
            "Project is pending approval; edits are locked until admin responds.",
            "warning",
        )
        return redirect(url_for("pm.project_detail", project_id=project.id))
    form = PMProjectEditForm(obj=project)
    if form.validate_on_submit():
        project.name = form.name.data.strip()
        project.client_name = form.client_name.data.strip()
        project.instructions = form.instructions.data.strip()
        dims, problems = parse_dimensions(
            form.rubric_dimensions.data,
            MAX_RUBRIC_DIMENSIONS, MAX_RUBRIC_DIM_NAME,
        )
        if problems:
            for p in problems:
                flash(p, "danger")
            return render_template(
                "pm/project_edit.html",
                project=project, form=form,
                form_action=url_for("pm.edit_project", project_id=project.id),
            )
        project.rubric_dimensions = "\n".join(dims) if dims else None
        project.required_annotations = form.required_annotations.data
        project.deadline = form.deadline.data
        # Pause/resume only when project is live.
        action = (form.status_action.data or "").strip()
        if project.status == "active" and action == "pause":
            project.status = "paused"
        elif project.status == "paused" and action == "resume":
            project.status = "active"
        log_event(
            "pm.project.edit",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={"action": action or "edit"},
        )
        db.session.commit()
        flash("Project updated.", "success")
        return redirect(url_for("pm.project_detail", project_id=project.id))
    return render_template(
        "pm/project_edit.html",
        project=project, form=form,
        form_action=url_for("pm.edit_project", project_id=project.id),
    )


# ---------- Tasks ----------------------------------------------------------


@pm_bp.route("/projects/<int:project_id>/tasks")
@pm_required
def tasks(project_id: int):
    project = _load_pm_project(project_id)
    status_filter = request.args.get("status", "", type=str)
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    stmt = select(Task).where(Task.project_id == project.id)
    if status_filter in TASK_STATUSES:
        stmt = stmt.where(Task.status == status_filter)
    stmt = stmt.order_by(Task.id.desc())
    pagination = db.paginate(
        stmt, page=page, per_page=PM_PER_PAGE, error_out=False
    )
    stats = _project_stats(project)
    annotators = db.session.scalars(
        select(User).where(User.role == "annotator", User.is_active.is_(True))
        .order_by(User.name)
    ).all()
    return render_template(
        "pm/tasks.html",
        project=project,
        tasks=pagination.items,
        pagination=pagination,
        status_filter=status_filter,
        stats=stats,
        annotators=annotators,
    )


@pm_bp.route("/projects/<int:project_id>/upload", methods=["GET", "POST"])
@pm_required
def upload_tasks(project_id: int):
    project = _load_pm_project(project_id)
    if project.status not in ("draft", "active", "paused"):
        flash("Tasks can only be added to draft, active, or paused projects.", "warning")
        return redirect(url_for("pm.project_detail", project_id=project.id))
    form = TaskPasteForm()
    if form.validate_on_submit():
        new_tasks, errors = _parse_task_lines(form.tasks_data.data or "", project)
        if errors:
            for e in errors[:50]:
                flash(e, "danger")
            return render_template("pm/task_upload.html", project=project, form=form)
        if not new_tasks:
            flash("No tasks parsed from the input.", "warning")
            return render_template("pm/task_upload.html", project=project, form=form)
        if len(new_tasks) > UPLOAD_BATCH_LIMIT_CFG:
            flash(
                f"Batch too large (max {UPLOAD_BATCH_LIMIT_CFG} tasks per upload).",
                "danger",
            )
            return render_template("pm/task_upload.html", project=project, form=form)
        db.session.add_all(new_tasks)
        log_event(
            "pm.tasks.upload",
            actor=current_user,
            entity_type="project",
            entity_id=project.id,
            detail={"count": len(new_tasks)},
        )
        db.session.commit()
        flash(f"{len(new_tasks)} tasks added.", "success")
        return redirect(url_for("pm.tasks", project_id=project.id))
    return render_template("pm/task_upload.html", project=project, form=form)


@pm_bp.route("/projects/<int:project_id>/tasks/assign", methods=["POST"])
@pm_required
def bulk_assign(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(request.referrer or url_for("pm.dashboard"))
    project = _load_pm_project(project_id)
    if project.status not in ("active", "paused"):
        flash("Assignments only on live projects.", "warning")
        return redirect(url_for("pm.tasks", project_id=project.id))
    annotator_id = request.form.get("annotator_id", type=int)
    task_ids = request.form.getlist("task_id", type=int)
    annotator = db.session.get(User, annotator_id) if annotator_id else None
    if annotator is None or annotator.role != "annotator" or not annotator.is_active:
        flash("Choose an active annotator.", "danger")
        return redirect(url_for("pm.tasks", project_id=project.id))
    if not task_ids:
        flash("Select at least one task.", "warning")
        return redirect(url_for("pm.tasks", project_id=project.id))
    assigned = 0
    for tid in task_ids:
        task = db.session.get(Task, tid)
        if task is None or task.project_id != project.id:
            continue
        if task.status in ("assigned", "in_progress", "submitted", "approved"):
            continue
        task.status = "assigned"
        task.assigned_to = annotator.id
        task.claimed_at = None  # PM-assigned, not self-claimed
        assigned += 1
    if assigned:
        notify(
            annotator, "task.assigned",
            payload={"project_id": project.id, "project": project.name, "count": assigned},
        )
        log_event(
            "pm.tasks.assign",
            actor=current_user, entity_type="project", entity_id=project.id,
            detail={"annotator_id": annotator.id, "count": assigned},
        )
    db.session.commit()
    flash(f"{assigned} task(s) assigned to {annotator.name}.", "success")
    return redirect(url_for("pm.tasks", project_id=project.id))


# ---------- Submissions (read-only) ----------------------------------------


@pm_bp.route("/projects/<int:project_id>/submissions")
@pm_required
def submissions(project_id: int):
    project = _load_pm_project(project_id)
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    stmt = (
        select(Submission)
        .join(Task, Task.id == Submission.task_id)
        .where(Task.project_id == project.id)
        .order_by(Submission.submitted_at.desc())
    )
    pagination = db.paginate(stmt, page=page, per_page=PM_PER_PAGE, error_out=False)
    return render_template(
        "pm/submissions.html",
        project=project,
        pagination=pagination,
        submissions=pagination.items,
    )


# ---------- Export ---------------------------------------------------------


@pm_bp.route("/projects/<int:project_id>/export")
@pm_required
def export(project_id: int):
    project = _load_pm_project(project_id)
    fmt = request.args.get("format", "jsonl")
    include = request.args.get("include", "approved")
    query = select(Task).where(Task.project_id == project.id)
    if include == "approved":
        query = query.where(Task.status == "approved")
    tasks = db.session.scalars(query.order_by(Task.id)).all()

    def generate_jsonl():
        import json as _json
        yield _json.dumps({"project": project.name, "task_type": project.task_type,
                           "exported_at": utc_now().isoformat() + "Z"},
                          ensure_ascii=False) + "\n"
        for t in tasks:
            row = {"id": t.id, "text": t.input_data}
            sub = (
                db.session.scalars(
                    select(Submission)
                    .where(Submission.task_id == t.id, Submission.status == "approved")
                    .order_by(Submission.approved_at.desc())
                    .limit(1)
                ).first()
            )
            if sub is not None:
                row["answer"] = sub.data_json and (
                    __import__("json").loads(sub.data_json).get("answer", "")
                )
            yield _json.dumps(row, ensure_ascii=False) + "\n"

    if fmt == "jsonl":
        log_event(
            "pm.export.jsonl", actor=current_user,
            entity_type="project", entity_id=project.id,
            detail={"rows": len(tasks), "include": include},
        )
        db.session.commit()
        return Response(
            generate_jsonl(), mimetype="application/x-jsonlines",
            headers={
                "Content-Disposition":
                    f'attachment; filename="project_{project.id}_{include}.jsonl"'
            },
        )
    # CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["task_id", "text", "answer"])
    for t in tasks:
        sub = db.session.scalars(
            select(Submission)
            .where(Submission.task_id == t.id, Submission.status == "approved")
            .order_by(Submission.approved_at.desc()).limit(1)
        ).first()
        ans = ""
        if sub and sub.data_json:
            try:
                ans = __import__("json").loads(sub.data_json).get("answer", "")
            except Exception:
                ans = ""
        writer.writerow([t.id, t.input_data, ans])
    log_event(
        "pm.export.csv", actor=current_user,
        entity_type="project", entity_id=project.id,
        detail={"rows": len(tasks), "include": include},
    )
    db.session.commit()
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="project_{project.id}_{include}.csv"'},
    )


# ---------- Team / roster --------------------------------------------------


@pm_bp.route("/projects/<int:project_id>/team")
@pm_required
def team(project_id: int):
    project = _load_pm_project(project_id)
    # Annotators on this project = anyone who has a submission or assigned task.
    ann_ids_with_tasks = db.session.scalars(
        select(Task.assigned_to).where(
            Task.project_id == project.id, Task.assigned_to.is_not(None)
        )
    ).all()
    sub_ids = db.session.scalars(
        select(Submission.annotator_id)
        .join(Task, Task.id == Submission.task_id)
        .where(Task.project_id == project.id)
    ).all()
    all_ids = set(ann_ids_with_tasks) | set(sub_ids)
    annotators = []
    for uid in all_ids:
        if uid is None:
            continue
        u = db.session.get(User, uid)
        if u is None:
            continue
        done = db.session.scalar(
            select(func.count(Submission.id))
            .join(Task, Task.id == Submission.task_id)
            .where(Submission.annotator_id == uid, Task.project_id == project.id)
        ) or 0
        approved = db.session.scalar(
            select(func.count(Submission.id))
            .join(Task, Task.id == Submission.task_id)
            .where(
                Submission.annotator_id == uid,
                Task.project_id == project.id,
                Submission.status == "approved",
            )
        ) or 0
        annotators.append({
            "user": u,
            "submitted": done,
            "approved": approved,
            "avg": round((approved / done) * 100, 1) if done else 0.0,
        })
    annotators.sort(key=lambda r: (-r["submitted"], r["user"].name))
    pms = [a.user for a in project.assignments if a.role_in_project == "project_manager"]
    # Quiz status per annotator
    quiz = db.session.scalar(
        select(QualificationQuiz).where(
            QualificationQuiz.project_id == project.id
        )
    )
    quiz_status = {}
    if quiz is not None:
        attempts = db.session.scalars(
            select(QuizAttempt).where(QuizAttempt.quiz_id == quiz.id)
        ).all()
        for att in attempts:
            quiz_status[att.annotator_id] = att.status
    return render_template(
        "pm/team.html",
        project=project,
        annotators=annotators,
        pms=pms,
        quiz=quiz,
        quiz_status=quiz_status,
    )


@pm_bp.route("/projects/<int:project_id>/request-exemption", methods=["POST"])
@pm_required
def request_exemption(project_id: int):
    """PM cannot grant exemptions directly; they ask admin."""
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(request.referrer or url_for("pm.dashboard"))
    project = _load_pm_project(project_id)
    annotator_id = request.form.get("annotator_id", type=int)
    reason = (request.form.get("reason") or "").strip()
    ann = db.session.get(User, annotator_id) if annotator_id else None
    if ann is None or ann.role != "annotator":
        flash("Pick an annotator.", "danger")
        return redirect(url_for("pm.team", project_id=project.id))
    for admin in db.session.scalars(
        select(User).where(User.role == "admin", User.is_active.is_(True))
    ).all():
        notify(
            admin, "pm.exemption_request",
            payload={
                "project_id": project.id,
                "project": project.name,
                "annotator_id": ann.id,
                "annotator": ann.name,
                "reason": reason[:300],
                "requester": current_user.name,
            },
        )
    log_event(
        "pm.exemption.request", actor=current_user,
        entity_type="project", entity_id=project.id,
        detail={"annotator_id": ann.id, "reason": reason[:300]},
    )
    db.session.commit()
    flash(f"Exemption request for {ann.name} sent to admin.", "info")
    return redirect(url_for("pm.team", project_id=project.id))


# ---------- Client links (PM can issue for delivery) -----------------------


@pm_bp.route("/projects/<int:project_id>/client-links", methods=["POST"])
@pm_required
def create_client_link(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(request.referrer or url_for("pm.dashboard"))
    project = _load_pm_project(project_id)
    label = (request.form.get("label") or "").strip()[:120] or f"{project.name} delivery"
    raw = portal_tokens.generate_token()
    row = ClientAccessToken(
        project_id=project.id,
        label=label,
        token_hash=portal_tokens.hash_token(raw),
        created_by=current_user.id,
    )
    db.session.add(row)
    db.session.flush()
    # Expose the raw token once via flash; we cannot recover it later.
    from urllib.parse import urljoin
    link = urljoin(
        request.host_url.rstrip("/") + "/",
        url_for("client.dashboard", token=raw),
    )
    log_event(
        "pm.client_token.create", actor=current_user,
        entity_type="project", entity_id=project.id,
        detail={"token_id": row.id, "label": label},
    )
    db.session.commit()
    flash(
        f"Client link created (save it now — it is shown only once): {link}",
        "success",
    )
    return redirect(url_for("pm.project_detail", project_id=project.id))


# ---------- Quiz (draft only) ----------------------------------------------


@pm_bp.route("/projects/<int:project_id>/quiz", methods=["GET", "POST"])
@pm_required
def quiz(project_id: int):
    project = _load_pm_project(project_id)
    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please reload and try again.", "warning")
            return redirect(request.referrer or url_for("pm.dashboard"))
    quiz_row = db.session.scalar(
        select(QualificationQuiz).where(QualificationQuiz.project_id == project.id)
    )
    if request.method == "POST":
        if quiz_row and quiz_row.status == "active":
            abort(403)
        title = (request.form.get("title") or "").strip()[:200]
        pass_score = request.form.get("pass_score", type=int) or 0
        max_attempts = request.form.get("max_attempts", type=int) or 0
        questions_json = (request.form.get("questions_json") or "").strip()
        if not title:
            flash("Quiz title is required.", "danger")
        elif not (1 <= pass_score <= 100):
            flash("Pass score must be 1-100.", "danger")
        elif not (1 <= max_attempts <= 20):
            flash("Max attempts must be 1-20.", "danger")
        else:
            try:
                import json as _json
                parsed = _json.loads(questions_json) if questions_json else []
                if not isinstance(parsed, list) or len(parsed) == 0:
                    raise ValueError("need at least one question")
            except Exception:
                flash("Questions must be valid JSON (array of questions).", "danger")
            else:
                if quiz_row is None:
                    quiz_row = QualificationQuiz(
                        project_id=project.id,
                        title=title,
                        questions_json=questions_json,
                        pass_score=pass_score,
                        max_attempts=max_attempts,
                        status="draft",
                        created_by=current_user.id,
                        created_at=utc_now(), updated_at=utc_now(),
                    )
                    db.session.add(quiz_row)
                else:
                    quiz_row.title = title
                    quiz_row.questions_json = questions_json
                    quiz_row.pass_score = pass_score
                    quiz_row.max_attempts = max_attempts
                    quiz_row.updated_at = utc_now()
                log_event(
                    "pm.quiz.save_draft", actor=current_user,
                    entity_type="project", entity_id=project.id,
                )
                db.session.commit()
                flash("Quiz draft saved. Ask admin to activate it.", "success")
                return redirect(url_for("pm.quiz", project_id=project.id))
    # Parse questions for preview (best-effort; fall back to empty on bad JSON).
    preview_questions: list[dict] = []
    if quiz_row is not None:
        try:
            import json as _json
            parsed = _json.loads(quiz_row.questions_json or "[]")
            if isinstance(parsed, list):
                preview_questions = [
                    q for q in parsed
                    if isinstance(q, dict) and "q" in q and "choices" in q
                ]
        except Exception:
            preview_questions = []

    # Qualified roster (read-only for PMs) — annotators who passed or were
    # exempted, newest first by their qualifying attempt.
    roster: list[dict] = []
    if quiz_row is not None:
        rows = db.session.execute(
            select(QuizAttempt, User.name)
            .join(User, User.id == QuizAttempt.annotator_id)
            .where(
                QuizAttempt.quiz_id == quiz_row.id,
                QuizAttempt.status.in_(("passed", "exempt")),
            )
            .order_by(QuizAttempt.id.desc())
        ).all()
        seen: set[int] = set()
        for attempt, name in rows:
            if attempt.annotator_id in seen:
                continue
            seen.add(attempt.annotator_id)
            roster.append({
                "annotator": name,
                "how": ("exempt" if attempt.status == "exempt" else "passed"),
                "score": attempt.score_percent,
                "when": attempt.created_at,
            })

    # Recent attempts (read-only) so PMs can see who's trying/struggling.
    attempts: list[dict] = []
    if quiz_row is not None:
        rows = db.session.execute(
            select(QuizAttempt, User.name)
            .join(User, User.id == QuizAttempt.annotator_id)
            .where(QuizAttempt.quiz_id == quiz_row.id)
            .order_by(QuizAttempt.id.desc())
            .limit(25)
        ).all()
        for attempt, name in rows:
            attempts.append({
                "annotator": name,
                "status": attempt.status,
                "score": attempt.score_percent,
                "when": attempt.created_at,
            })

    return render_template(
        "pm/quiz.html",
        project=project,
        quiz=quiz_row,
        preview_questions=preview_questions,
        roster=roster,
        attempts=attempts,
    )


# ---------- Media (list + simple upload) -----------------------------------


@pm_bp.route("/projects/<int:project_id>/media", methods=["GET", "POST"])
@pm_required
def media(project_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(request.referrer or url_for("pm.dashboard"))
    project = _load_pm_project(project_id)
    if request.method == "POST":
        import hashlib
        from werkzeug.utils import secure_filename
        f = request.files.get("file")
        kind = (request.form.get("kind") or "image")[:20]
        allowed_kinds = {"image", "audio", "document", "other"}
        if kind not in allowed_kinds:
            kind = "other"
        if not f or not f.filename:
            flash("Pick a file to upload.", "danger")
        else:
            fname = secure_filename(f.filename)
            data = f.read()
            if len(data) > 10 * 1024 * 1024:
                flash("File too large (max 10 MB).", "danger")
            elif len(data) == 0:
                flash("Empty file rejected.", "danger")
            else:
                digest = hashlib.sha256(data).hexdigest()
                existing = db.session.scalar(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project.id,
                        MediaAsset.sha256 == digest,
                    )
                )
                if existing is not None:
                    flash(f"Already uploaded as {existing.filename}.", "info")
                else:
                    asset = MediaAsset(
                        project_id=project.id,
                        filename=fname,
                        content_type=f.mimetype or "application/octet-stream",
                        data=data, byte_size=len(data), kind=kind,
                        sha256=digest,
                        uploaded_by=current_user.id,
                    )
                    db.session.add(asset)
                    log_event(
                        "pm.media.upload", actor=current_user,
                        entity_type="project", entity_id=project.id,
                        detail={"filename": fname, "size": len(data)},
                    )
                    db.session.commit()
                    flash(f"Uploaded {fname}.", "success")
                return redirect(url_for("pm.media", project_id=project.id))
    page = request.args.get("page", 1, type=int)
    assets = db.paginate(
        select(MediaAsset)
        .where(MediaAsset.project_id == project.id)
        .order_by(MediaAsset.id.desc()),
        page=page, per_page=20, error_out=False,
    )
    return render_template("pm/media.html",
                           project=project, assets=assets.items, pagination=assets)


@pm_bp.route("/applicants", methods=["GET"])
@pm_required
def applicants():
    """PM view of the applicant directory and pod talent pool."""
    from models import Applicant, User
    import json

    lang_filter = request.args.get("language", "").strip().lower()
    stage_filter = request.args.get("stage", "").strip().lower()
    decision_filter = request.args.get("decision", "").strip().lower()
    q = request.args.get("q", "").strip()

    query = db.session.query(Applicant).join(User, Applicant.user_id == User.id)

    if lang_filter:
        query = query.filter(Applicant.primary_language == lang_filter)
    if stage_filter:
        query = query.filter(Applicant.stage == stage_filter)
    if decision_filter:
        query = query.filter(Applicant.admin_decision == decision_filter)
    if q:
        query = query.filter(
            (User.name.ilike(f"%{q}%")) | (User.email.ilike(f"%{q}%")) | (Applicant.dialect.ilike(f"%{q}%"))
        )

    all_applicants = query.order_by(Applicant.created_at.desc()).all()

    metrics = {
        "total": db.session.query(Applicant).count(),
        "testing": db.session.query(Applicant).filter(Applicant.stage.in_(("apply", "guidelines", "test"))).count(),
        "pending": db.session.query(Applicant).filter_by(admin_decision="pending").count(),
        "approved": db.session.query(Applicant).filter_by(admin_decision="approved").count(),
        "waitlisted": db.session.query(Applicant).filter_by(admin_decision="waitlisted").count(),
        "active": db.session.query(Applicant).filter_by(stage="active").count(),
    }

    return render_template(
        "admin/applicants.html",
        applicants=all_applicants,
        metrics=metrics,
        lang_filter=lang_filter,
        stage_filter=stage_filter,
        decision_filter=decision_filter,
        q=q,
    )


@pm_bp.route("/applicants/<int:applicant_id>", methods=["GET"])
@pm_required
def applicant_detail(applicant_id: int):
    """PM view of an applicant's complete profile and trial submissions."""
    from models import Applicant, ApplicantTrialSubmission
    import json

    applicant = db.session.get(Applicant, applicant_id)
    if applicant is None:
        abort(404)

    test_answers = {}
    if applicant.test_answers_json:
        try:
            test_answers = json.loads(applicant.test_answers_json)
        except Exception:
            test_answers = {}

    trial_subs = (
        db.session.query(ApplicantTrialSubmission)
        .filter_by(applicant_id=applicant.id)
        .order_by(ApplicantTrialSubmission.task_index.asc())
        .all()
    )

    return render_template(
        "admin/applicant_detail.html",
        applicant=applicant,
        test_answers=test_answers,
        trial_subs=trial_subs,
    )


@pm_bp.route("/applicants/<int:applicant_id>/decision", methods=["POST"])
@pm_required
def applicant_decision(applicant_id: int):
    """PM records an applicant decision."""
    from models import Applicant, utc_now

    applicant = db.session.get(Applicant, applicant_id)
    if applicant is None:
        abort(404)

    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("pm.applicant_detail", applicant_id=applicant.id))

    decision = request.form.get("decision", "").strip().lower()
    notes = request.form.get("notes", "").strip()

    if decision not in ("approved", "waitlisted", "declined"):
        flash("Invalid decision choice.", "danger")
        return redirect(url_for("pm.applicant_detail", applicant_id=applicant.id))

    applicant.admin_decision = decision
    applicant.admin_decision_by = current_user.id
    applicant.admin_decision_at = utc_now()
    applicant.admin_notes = notes

    if decision == "approved":
        applicant.stage = "agreement"
        flash(f"Applicant {applicant.user.name} approved! Advanced to Stage 7 (Contractor Agreement).", "success")
    elif decision == "waitlisted":
        applicant.stage = "waitlisted"
        flash(f"Applicant {applicant.user.name} moved to the Waitlist talent pool.", "info")
    elif decision == "declined":
        applicant.stage = "declined"
        flash(f"Applicant {applicant.user.name} declined.", "warning")

    db.session.commit()
    log_event(
        "applicant.decision",
        actor=current_user,
        entity_type="applicant",
        entity_id=applicant.id,
        detail={"decision": decision, "notes": notes},
    )
    return redirect(url_for("pm.applicant_detail", applicant_id=applicant.id))
