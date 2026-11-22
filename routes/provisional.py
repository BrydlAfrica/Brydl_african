"""Provisional Annotator Workspace blueprint.

Provides the dedicated workspace for unverified/provisional annotators who have
completed initial onboarding and are actively completing eligible projects,
accumulating earnings, and building their performance record toward official verification.

Invariants enforced here:
- Strict access boundary: only role='annotator' with stage in ('trial', 'active').
- Excluded from official verified workforce headcount queries.
- Tasks are restricted to projects with Project.allows_provisional == True.
- Full parity with standard annotation forms, QA grading, and earnings ledger.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from audit import log_event
from cutover import compute_provisional_performance
from extensions import db
from models import (
    OPEN_TASK_STATUSES,
    Applicant,
    Payment,
    Project,
    Submission,
    Task,
    User,
    money,
    utc_now,
)
from routes import provisional_required
from routes.annotator import (
    _media_allowed_labels,
    _media_input_document,
    _preference_flipped,
    _preference_input_document,
    _submission_form_class,
)
from rubric import parse_dimensions

provisional_bp = Blueprint("provisional", __name__, url_prefix="/onboard/workspace")


@provisional_bp.route("/")
@provisional_bp.route("/dashboard")
@provisional_required
def dashboard():
    """Provisional annotator landing dashboard with verification readiness meter."""
    applicant = getattr(current_user, "applicant_profile", None)
    if applicant is None:
        # Fall back or create basic profile if missing
        applicant = db.session.scalar(
            select(Applicant).where(Applicant.user_id == current_user.id)
        )

    applicant_id = applicant.id if applicant else 0
    perf = compute_provisional_performance(applicant_id)

    # Fetch assigned open tasks on eligible provisional projects
    stmt_tasks = (
        select(Task)
        .join(Task.project)
        .where(
            Task.assigned_to == current_user.id,
            Task.status.in_(OPEN_TASK_STATUSES),
            Project.allows_provisional == True,
        )
        .options(joinedload(Task.project))
        .order_by(Task.id.desc())
        .limit(10)
    )
    open_tasks = db.session.scalars(stmt_tasks).all()

    # Fetch recent submissions
    stmt_subs = (
        select(Submission)
        .where(Submission.annotator_id == current_user.id)
        .options(joinedload(Submission.task).joinedload(Task.project))
        .order_by(Submission.submitted_at.desc())
        .limit(10)
    )
    recent_submissions = db.session.scalars(stmt_subs).all()

    # Available provisional projects
    available_projects = db.session.scalars(
        select(Project)
        .where(Project.status == "active", Project.allows_provisional == True)
        .order_by(Project.created_at.desc())
        .limit(5)
    ).all()

    return render_template(
        "onboarding/provisional_workspace.html",
        applicant=applicant,
        perf=perf,
        open_tasks=open_tasks,
        recent_submissions=recent_submissions,
        available_projects=available_projects,
    )


@provisional_bp.route("/tasks")
@provisional_required
def tasks():
    """List all open tasks assigned to the provisional annotator."""
    applicant = getattr(current_user, "applicant_profile", None)
    stmt = (
        select(Task)
        .join(Task.project)
        .where(
            Task.assigned_to == current_user.id,
            Task.status.in_(OPEN_TASK_STATUSES),
            Project.allows_provisional == True,
        )
        .options(joinedload(Task.project))
        .order_by(Task.id.asc())
    )
    assigned_tasks = db.session.scalars(stmt).all()
    return render_template(
        "onboarding/provisional_tasks.html",
        applicant=applicant,
        tasks=assigned_tasks,
    )


@provisional_bp.route("/task/<int:task_id>", methods=["GET", "POST"])
@provisional_required
def task_detail(task_id: int):
    """Execute and submit a provisional task."""
    task = db.session.get(Task, task_id)
    if not task or task.assigned_to != current_user.id:
        abort(404)

    project = task.project
    if not project.allows_provisional:
        flash(
            "This project requires official verified annotator status.",
            "warning",
        )
        return redirect(url_for("provisional.dashboard"))

    if task.status not in OPEN_TASK_STATUSES:
        flash("This task has already been submitted or completed.", "info")
        return redirect(url_for("provisional.tasks"))

    rubric_dims = parse_dimensions(project.rubric_dimensions) if project.rubric_dimensions else []
    media_doc = _media_input_document(task.input_data)
    label_choices = _media_allowed_labels(media_doc)

    FormClass = _submission_form_class(
        task_type=project.task_type,
        rubric_dims=rubric_dims,
        label_choices=label_choices,
    )
    form = FormClass()

    pref_doc = _preference_input_document(task.input_data)
    is_flipped = _preference_flipped(task.id, current_user.id) if pref_doc else False

    if form.validate_on_submit():
        # Package output payload based on task type
        output_payload = {}
        if project.task_type == "classification":
            output_payload["label"] = form.label_choice.data
        elif project.task_type == "translation":
            output_payload["translation"] = form.translated_text.data.strip()
        elif project.task_type == "ner":
            output_payload["entities"] = form.entities_json.data.strip()
        elif project.task_type in ("rlhf", "preference"):
            choice = form.preference_choice.data
            if is_flipped and choice in ("response_a", "response_b"):
                choice = "response_b" if choice == "response_a" else "response_a"
            output_payload["preferred"] = choice
            output_payload["rationale"] = getattr(form, "rationale", form.submit).data or ""
        elif project.task_type == "audio_transcription":
            output_payload["transcript"] = form.transcript_text.data.strip()
        elif project.task_type == "image_labeling":
            output_payload["labels"] = form.image_labels.data.strip()
        else:
            output_payload["text"] = getattr(form, "response_text", form.submit).data or ""

        # Create submission
        submission = Submission(
            task_id=task.id,
            annotator_id=current_user.id,
            output_data=json.dumps(output_payload, ensure_ascii=False),
            status="submitted",
            submitted_at=utc_now(),
        )
        db.session.add(submission)

        task.status = "submitted"
        db.session.commit()

        log_event(
            "provisional.task_submitted",
            actor=current_user,
            entity_type="task",
            entity_id=task.id,
            detail={"project_id": project.id, "task_type": project.task_type},
            commit=True,
        )

        flash("Task submitted successfully! Earned towards verification.", "success")
        return redirect(url_for("provisional.dashboard"))

    return render_template(
        "onboarding/provisional_task_form.html",
        task=task,
        project=project,
        form=form,
        pref_doc=pref_doc,
        media_doc=media_doc,
        is_flipped=is_flipped,
    )


@provisional_bp.route("/history")
@provisional_required
def history():
    """View submission history, QA scores, and feedback for provisional worker."""
    page = request.args.get("page", 1, type=int)
    per_page = 20

    stmt = (
        select(Submission)
        .where(Submission.annotator_id == current_user.id)
        .options(joinedload(Submission.task).joinedload(Task.project))
        .order_by(Submission.submitted_at.desc())
    )
    submissions = db.paginate(stmt, page=page, per_page=per_page, error_out=False)

    return render_template(
        "onboarding/provisional_history.html",
        submissions=submissions,
    )


@provisional_bp.route("/earnings")
@provisional_required
def earnings():
    """View provisional pay ledger, accumulated balance, and payout history."""
    applicant = getattr(current_user, "applicant_profile", None)
    applicant_id = applicant.id if applicant else 0
    perf = compute_provisional_performance(applicant_id)

    # Approved submissions with rates
    stmt = (
        select(Submission)
        .where(
            Submission.annotator_id == current_user.id,
            Submission.status == "approved",
        )
        .options(joinedload(Submission.task).joinedload(Task.project))
        .order_by(Submission.submitted_at.desc())
    )
    approved_submissions = db.session.scalars(stmt).all()

    # Payments ledger
    stmt_payments = (
        select(Payment)
        .where(Payment.annotator_id == current_user.id)
        .order_by(Payment.created_at.desc())
    )
    payments = db.session.scalars(stmt_payments).all()

    return render_template(
        "onboarding/provisional_earnings.html",
        perf=perf,
        approved_submissions=approved_submissions,
        payments=payments,
    )
