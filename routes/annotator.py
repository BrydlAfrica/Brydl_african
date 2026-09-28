"""Annotator blueprint: dashboard, task completion, history.

Shipped: the annotator landing dashboard (/annotator/ — open-task queue,
personal stats, monthly earnings, recent submissions), the task
completion page (/annotator/task/<id> — dynamic per-task-type output
form) and the submission history (/annotator/history).

Access control: @annotator_required (from routes/__init__.py, shared
with the admin blueprint) wraps Flask-Login's @login_required —
anonymous visitors are redirected to sign-in; signed-in admins get 403.

Security invariants enforced here:
- A task page 404s unless the task is assigned to the requesting
  annotator (404 rather than 403 so task ids cannot be probed).
- Task.expected_output (the QA answer key) is NEVER selected or rendered
  by any route/template in this blueprint.
- Double submission is prevented twice: a friendly status check before
  validation, and a race-safe conditional UPDATE inside the write
  transaction (only a task still in a submittable state can transition
  to 'submitted', so two simultaneous posts cannot both score).
- Rejected tasks may be redone: each revision creates a NEW Submission
  row (audit trail preserved); the rejected row is never overwritten.

All database access goes through the SQLAlchemy ORM — no raw SQL.
"""

import hashlib
import json
import re
import time
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
    Response,
)
from flask_login import current_user, logout_user
from flask_wtf import FlaskForm
from flask_wtf.csrf import ValidationError, validate_csrf
from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import joinedload
from wtforms import (
    IntegerField,
    PasswordField,
    RadioField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    AnyOf,
    DataRequired,
    EqualTo,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    Regexp,
)

from audit import log_event
from config import MAX_TEXTAREA_CHARS, PAGINATION_PER_PAGE
from consensus import refresh_group
from rendering import content_view, render_rich
from rubric import parse_dimensions
from extensions import db
import quizzes as quiz_engine
from models import MediaAsset, Notification
from models import QualificationQuiz, QuizAttempt
from models import Project, Submission, Task, money, utc_now
from routes import annotator_required

annotator_bp = Blueprint("annotator", __name__)

# Single-content cap shared with the admin side (config.MAX_TEXTAREA_CHARS):
# one task input, one annotator answer — same limit.
MAX_OUTPUT_CHARS = MAX_TEXTAREA_CHARS
HISTORY_PER_PAGE = PAGINATION_PER_PAGE

# Workflow states from which an annotator may open and submit a task.
SUBMITTABLE_STATUSES = ("assigned", "in_progress", "rejected")

CLASSIFICATION_LABELS = ("positive", "negative", "neutral")

# Endpoints reachable BEFORE accepting the current terms: only the
# terms page itself and its acceptance POST. Everything else —
# dashboard, tasks, profile, bank details — waits until the annotator
# has agreed (the acceptance is the consent record).
TERMS_EXEMPT_ENDPOINTS = frozenset({
    "annotator.terms",
    "annotator.accept_terms",
})


@annotator_bp.before_request
def require_current_terms():
    """Gate the annotator workspace behind the current T&C version.

    Anonymous visitors are skipped (Flask-Login redirects them) and so
    are admins (their decorator answers 403). Annotators whose stored
    terms_version differs from config.TERMS_VERSION are redirected to
    the terms page until they accept — bumping the config version
    re-prompts the whole team at once.

    Returns:
        None to allow the request, or a redirect to the terms page.
    """
    if not current_user.is_authenticated:
        return None
    if current_user.role != "annotator":
        return None
    if current_user.terms_version == current_app.config.get(
        "TERMS_VERSION"
    ):
        return None
    if request.endpoint in TERMS_EXEMPT_ENDPOINTS:
        return None
    return redirect(url_for("annotator.terms"))


def _submission_form_class(
    task_type: str,
    rubric_dims: list[str] | None = None,
    scale_min: int = 1,
    scale_max: int = 5,
    label_choices: list[str] | None = None,
) -> type[FlaskForm]:
    """Build the submission form matching a project's task type.

    Field structure is decided server-side from Project.task_type — the
    client only ever receives the fields for the task at hand, and the
    same mapping validates the POST. Preference tasks in a project with
    rubric dimensions additionally carry one integer slider field per
    (dimension, canonical response) pair, named by INDEX
    (rubric_0_a ...) so free-text dimension names never touch field ids.

    Args:
        task_type: One of the models.TASK_TYPES values.
        rubric_dims: The project's parsed rubric dimensions (empty or
            None = no rubric fields).
        scale_min: Inclusive lower slider bound.
        scale_max: Inclusive upper slider bound.
        label_choices: Media-only — the image task's allowed labels.
            Passed separately from rubric_dims so a labelled media task
            never accidentally grows invisible rubric slider fields
            (their InputRequired checks would brick the POST).

    Returns:
        A FlaskForm subclass with the appropriate fields.
    """
    attrs: dict = {
        "__doc__": f"Submission form for {task_type} tasks.",
        "submit": SubmitField("Submit answer"),
    }
    for index, dim in enumerate(rubric_dims or []):
        for side in ("a", "b"):
            attrs[f"rubric_{index}_{side}"] = IntegerField(
                dim,
                default=(scale_min + scale_max) // 2,
                validators=[
                    InputRequired(
                        message="Every rubric slider needs a value."
                    ),
                    NumberRange(
                        min=scale_min,
                        max=scale_max,
                        message=(
                            f"Scores must be {scale_min} to"
                            f" {scale_max}."
                        ),
                    ),
                ],
            )
    if task_type == "classification":
        attrs["label_choice"] = RadioField(
            "Your classification",
            choices=[
                (label, label.title()) for label in CLASSIFICATION_LABELS
            ],
            validators=[
                DataRequired(message="Choose a label."),
                AnyOf(
                    CLASSIFICATION_LABELS,
                    message="Choose one of the listed labels.",
                ),
            ],
        )
    elif task_type == "translation":
        attrs["translated_text"] = TextAreaField(
            "Your translation",
            validators=[
                DataRequired(message="Write your translation."),
                Length(
                    max=MAX_OUTPUT_CHARS,
                    message=(f"Translation must be {MAX_OUTPUT_CHARS}"
                             " characters or less."),
                ),
            ],
        )
    elif task_type == "ner":
        attrs["marked_entities"] = TextAreaField(
            "Entities you identified",
            validators=[
                DataRequired(message="Mark at least one entity."),
                Length(
                    max=MAX_OUTPUT_CHARS,
                    message=(f"Answer must be {MAX_OUTPUT_CHARS}"
                             " characters or less."),
                ),
            ],
        )
    elif task_type == "rlhf":
        attrs["rating"] = RadioField(
            "Response rating",
            coerce=int,
            choices=[(value, "★" * value) for value in range(1, 6)],
            validators=[
                DataRequired(message="Choose a 1-5 star rating."),
                NumberRange(
                    min=1, max=5, message="Rating must be 1 to 5 stars."
                ),
            ],
        )
        attrs["explanation"] = TextAreaField(
            "Why this rating?",
            validators=[
                DataRequired(message="Explain your rating."),
                Length(
                    max=MAX_OUTPUT_CHARS,
                    message=(f"Explanation must be {MAX_OUTPUT_CHARS}"
                             " characters or less."),
                ),
            ],
        )
    elif task_type == "preference":
        attrs["chosen"] = RadioField(
            "Which response is better?",
            choices=[("a", "Response A"), ("b", "Response B")],
            validators=[
                DataRequired(message="Choose A or B."),
                AnyOf(
                    ["a", "b"],
                    message="Choose one of the two responses.",
                ),
            ],
        )
        min_just = int(current_app.config.get(
            "PREFERENCE_MIN_JUSTIFICATION", 40
        ))
        attrs["justification"] = TextAreaField(
            "Why is the chosen response better?",
            validators=[
                DataRequired(
                    message="Write your justification."
                ),
                Length(
                    min=min_just,
                    max=MAX_OUTPUT_CHARS,
                    message=(
                        "Justification must be between"
                        f" {min_just} and {MAX_OUTPUT_CHARS}"
                        " characters."
                    ),
                ),
            ],
        )
    elif task_type == "image_labeling":
        allowed = [
            label
            for label in (
                (label_choices if label_choices is not None
                 else rubric_dims) or []
            )
            if label
        ]
        if allowed:
            attrs["label_choice"] = RadioField(
                "What does the image show?",
                choices=[(label, label) for label in allowed],
                validators=[
                    DataRequired(message="Choose a label."),
                    AnyOf(
                        allowed,
                        message="Choose one of the listed labels.",
                    ),
                ],
            )
        else:
            attrs["label_choice"] = StringField(
                "What does the image show?",
                validators=[
                    DataRequired(message="Write a label."),
                    Length(
                        max=200,
                        message="Label must be 200 characters or"
                        " less.",
                    ),
                ],
            )
        attrs["notes"] = TextAreaField(
            "Notes (optional)",
            validators=[
                Optional(),
                Length(
                    max=MAX_OUTPUT_CHARS,
                    message=(f"Notes must be {MAX_OUTPUT_CHARS}"
                             " characters or less."),
                ),
            ],
        )
    elif task_type == "audio_transcription":
        attrs["transcript"] = TextAreaField(
            "Your transcription",
            validators=[
                DataRequired(message="Write what you hear."),
                Length(
                    max=MAX_OUTPUT_CHARS,
                    message=(f"Transcript must be {MAX_OUTPUT_CHARS}"
                             " characters or less."),
                ),
            ],
        )
    else:
        # Future task types fall back to a generic free-text answer so
        # a new taxonomy value never bricks the annotator workflow.
        attrs["answer_text"] = TextAreaField(
            "Your answer",
            validators=[
                DataRequired(message="Write your answer."),
                Length(
                    max=MAX_OUTPUT_CHARS,
                    message=(f"Answer must be {MAX_OUTPUT_CHARS}"
                             " characters or less."),
                ),
            ],
        )
    return type("SubmissionForm", (FlaskForm,), attrs)


def _preference_input_document(raw: str) -> dict | None:
    """Parse a stored preference task input into its document.

    Args:
        raw: The task's input_data column (compact JSON document).

    Returns:
        The dict when it is a valid preference document, else None.
    """
    try:
        doc = json.loads(raw or "")
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


def _media_input_document(raw: str) -> dict | None:
    """Parse a stored media task input into its document.

    Valid documents carry kind image_labeling / audio_transcription,
    an integer media_id, and an optional prompt; image tasks may
    carry a comma-separated labels list that becomes the answer
    choices.

    Args:
        raw: The task's input_data column.

    Returns:
        The validated dict, or None for anything else (the task then
        renders through the plain-text fallback paths).
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
    if media_id < 1:
        return None
    labels = doc.get("labels")
    if labels is not None and not isinstance(labels, str):
        return None
    prompt = doc.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return None
    return doc


def _media_allowed_labels(doc: dict | None) -> list[str]:
    """Extract the answer choices from an image task document.

    Args:
        doc: A parsed media input document (or None).

    Returns:
        The cleaned label list; empty when the task accepts a free
        text label.
    """
    if not doc:
        return []
    raw = doc.get("labels") or ""
    seen: list[str] = []
    for part in raw.split(","):
        label = part.strip()
        if label and label not in seen:
            seen.append(label)
    return seen


def _preference_flipped(task_id: int, annotator_id: int) -> bool:
    """Deterministically decide this annotator's A/B display order.

    Hashing (task id, annotator id) gives every annotator a stable
    ~50/50 view order for the same task, which spreads position bias
    ("always pick the left one") without storing anything extra. The
    same value is computed on GET (display) and POST (mapping back),
    so the client never sends order state.

    Args:
        task_id: Primary key of the task.
        annotator_id: Primary key of the annotator.

    Returns:
        True when the canonical B response is displayed on the LEFT.
    """
    digest = hashlib.blake2b(
        f"{task_id}:{annotator_id}".encode(), digest_size=8
    ).digest()
    return bool(int.from_bytes(digest, "big") % 2)


def _build_output_data(task_type: str, form: FlaskForm) -> dict:
    """Package a validated form into the stored output_data document.

    Args:
        task_type: The project's task type.
        form: The validated submission form.
        flipped: Preference only — True when the canonical B response
            was DISPLAYED on the left for this annotator, so the chosen
            radio (left/right) maps back to the canonical response.

    Returns:
        A JSON-serialisable dict tagged with the answer kind, so the QA
        review feature can interpret every answer shape unambiguously.
    """
    if task_type == "classification":
        return {
            "kind": "classification",
            "label": form.label_choice.data,
        }
    if task_type == "translation":
        return {
            "kind": "translation",
            "text": form.translated_text.data.strip(),
        }
    if task_type == "ner":
        return {
            "kind": "ner",
            "marked_entities": form.marked_entities.data.strip(),
        }
    if task_type == "rlhf":
        return {
            "kind": "rlhf",
            "rating": int(form.rating.data),
            "explanation": form.explanation.data.strip(),
        }
    if task_type == "image_labeling":
        doc = {
            "kind": "image_labeling",
            "label": form.label_choice.data.strip(),
        }
        notes = (form.notes.data or "").strip()
        if notes:
            doc["notes"] = notes
        return doc
    if task_type == "audio_transcription":
        return {
            "kind": "audio_transcription",
            "transcript": form.transcript.data.strip(),
        }
    if task_type == "preference":
        flipped = getattr(form, "_flipped", False)
        shown = form.chosen.data  # "a" = LEFT as displayed
        canonical = shown if not flipped else (
            "b" if shown == "a" else "a"
        )
        doc = {
            "kind": "preference",
            "chosen": canonical,
            "seen_order": "ba" if flipped else "ab",
            "justification": form.justification.data.strip(),
        }
        dims = getattr(form, "_rubric_dims", None) or []
        if dims:
            doc["rubric"] = {
                dim: {
                    "a": getattr(form, f"rubric_{index}_a").data,
                    "b": getattr(form, f"rubric_{index}_b").data,
                }
                for index, dim in enumerate(dims)
            }
        return doc
    return {"kind": "text", "text": form.answer_text.data.strip()}


def _load_own_task(task_id: int) -> Task | None:
    """Fetch a task that belongs to the current annotator.

    Args:
        task_id: Primary key of the task.

    Returns:
        The Task if it exists AND is assigned to the current user,
        otherwise None (callers turn that into a 404 so ids cannot be
        probed).
    """
    task = db.session.get(Task, task_id)
    if task is None or task.assigned_to != current_user.id:
        return None
    return task


@annotator_bp.route(
    "/task/<int:task_id>/media/<int:asset_id>",
    endpoint="task_media",
)
@annotator_required
def task_media(task_id: int, asset_id: int):
    """Serve a task's media bytes to its assigned annotator only.

    The asset id is validated against the task's OWN input document
    (the task must reference it), the task must belong to the caller,
    and the asset must belong to the task's project — so a valid
    session can never probe another project's media by guessing ids.

    Args:
        task_id: The caller's assigned task.
        asset_id: The media asset the task references.

    Returns:
        The raw bytes with the upload-time allowlisted content type.
    """
    task = _load_own_task(task_id)
    if task is None:
        abort(404)
    doc = _media_input_document(task.input_data)
    if doc is None or doc["media_id"] != asset_id:
        abort(404)
    asset = db.session.get(MediaAsset, asset_id)
    if asset is None or asset.project_id != task.project_id:
        abort(404)
    return Response(
        asset.data,
        mimetype=asset.content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


def _render_task_page(
    task: Task,
    form: FlaskForm,
    pref_input: dict | None = None,
    flipped: bool = False,
    pref_views: dict | None = None,
    rubric_dims: list[str] | None = None,
    media_doc: dict | None = None,
    media_url: str | None = None,
    rendered_prompt: str | None = None,
    media_labels: list[str] | None = None,
):
    """Render the task completion page (shared by GET and failed POST).

    For rejected tasks, the QA feedback and the annotator's previous
    answer are loaded so the revision has context.

    Args:
        task: The task being worked on.
        form: The (possibly bound) submission form.
        pref_input: Parsed preference document (preference tasks only).
        flipped: True when canonical response B displays on the left.
        pref_views: Sanitized rich-HTML views for prompt/responses.
        rubric_dims: The project's rubric dimension names (preference
            tasks in rubric-enabled projects only).
        media_doc: Parsed media input document (image/audio tasks).
        media_url: The task-bound URL serving the media bytes.

    Returns:
        The rendered template response.
    """
    project = task.project
    input_view = content_view(task.input_data)
    qa_notes = None
    previous = None
    if task.status == "rejected":
        latest = db.session.scalar(
            select(Submission)
            .where(
                Submission.task_id == task.id,
                Submission.annotator_id == current_user.id,
            )
            .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        )
        if latest is not None:
            qa_notes = latest.qa_notes
            try:
                previous = json.loads(latest.output_data)
            except ValueError:
                previous = {"kind": "unknown", "raw": latest.output_data}
    return render_template(
        "annotator/task.html",
        task=task,
        project=project,
        form=form,
        task_type=project.task_type,
        qa_notes=qa_notes,
        previous=previous,
        max_chars=MAX_OUTPUT_CHARS,
        pref_input=pref_input,
        pref_views=pref_views,
        flipped=flipped,
        rubric_dims=rubric_dims or [],
        scale_min=int(current_app.config.get("RUBRIC_SCALE_MIN", 1)),
        scale_max=int(current_app.config.get("RUBRIC_SCALE_MAX", 5)),
        media_doc=media_doc,
        media_url=media_url,
        rendered_prompt=rendered_prompt,
        media_labels=media_labels or [],
        input_view=input_view,
        min_justification=int(current_app.config.get(
            "PREFERENCE_MIN_JUSTIFICATION", 40
        )),
    )


@annotator_bp.app_context_processor
def _unread_notification_badge():
    """Expose the unread-notification count to templates.

    Non-critical by design: on any database problem the badge simply
    renders as 0 and the error is logged.

    Returns:
        A template context fragment (empty for admins/anonymous).
    """
    if not current_user.is_authenticated or current_user.role != "annotator":
        return {}
    try:
        unread = db.session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == current_user.id,
                Notification.read_at.is_(None),
            )
        )
    except Exception:
        current_app.logger.exception("Unread badge query failed.")
        unread = 0
    return {"unread_notifications": unread or 0}


def _gate_blocker(project_id: int) -> QualificationQuiz | None:
    """The active quiz blocking the caller on one project, if any.

    Args:
        project_id: Primary key of the project.

    Returns:
        The active QualificationQuiz when the caller has neither a
        passed nor an exempt attempt on it, else None.
    """
    qualified = exists().where(
        QuizAttempt.quiz_id == QualificationQuiz.id,
        QuizAttempt.annotator_id == current_user.id,
        QuizAttempt.status.in_(("passed", "exempt")),
    )
    return db.session.scalar(
        select(QualificationQuiz).where(
            QualificationQuiz.project_id == project_id,
            QualificationQuiz.status == "active",
            ~qualified,
        )
    )


def _pending_quizzes() -> list[dict]:
    """Active quizzes the caller still owes, for the dashboard card.

    Returns:
        One dict per gated project: quiz id, project name, attempts
        used/allowed. Empty when everything is passed/exempt.
    """
    qualified = exists().where(
        QuizAttempt.quiz_id == QualificationQuiz.id,
        QuizAttempt.annotator_id == current_user.id,
        QuizAttempt.status.in_(("passed", "exempt")),
    )
    quizzes = db.session.scalars(
        select(QualificationQuiz)
        .options(joinedload(QualificationQuiz.project))
        .join(Project, Project.id == QualificationQuiz.project_id)
        .where(
            QualificationQuiz.status == "active",
            Project.status == "active",
            ~qualified,
        )
        .order_by(QualificationQuiz.id.asc())
    ).all()
    cards: list[dict] = []
    for quiz in quizzes:
        used = db.session.scalar(
            select(func.count(QuizAttempt.id)).where(
                QuizAttempt.quiz_id == quiz.id,
                QuizAttempt.annotator_id == current_user.id,
                QuizAttempt.status == "failed",
            )
        ) or 0
        cards.append({
            "quiz": quiz,
            "project_name": quiz.project.name
            if quiz.project else "the project",
            "attempts_used": used,
        })
    return cards


@annotator_bp.route("/", endpoint="dashboard")
@annotator_required
def dashboard():
    """Render the annotator dashboard: queue, stats, earnings, recents.

    Statistics are non-critical: on a database error the page still
    renders with zeros and an error flash instead of a 500.

    Returns:
        The rendered annotator dashboard template.
    """
    open_tasks: list = []
    recent: list = []
    stats = {
        "open_tasks": 0,
        "completed": 0,
        "approved": 0,
        "rejected": 0,
        "awaiting_review": 0,
        "earnings_month": 0.0,
        "earnings_all": 0.0,
    }
    try:
        # --- Open queue: assigned + in_progress + rejected (revisions),
        # oldest first. Rejected tasks stay visible so the annotator can
        # act on QA feedback; the task page shows the revision banner.
        rows = db.session.execute(
            select(
                Task,
                Project.name.label("project_name"),
                Project.deadline.label("project_deadline"),
            )
            .join(Task.project)
            .where(
                Task.assigned_to == current_user.id,
                Task.status.in_(("assigned", "in_progress", "rejected")),
            )
            .order_by(Task.created_at.asc(), Task.id.asc())
        ).all()
        open_tasks = [
            {
                "task": row.Task,
                "project_name": row.project_name,
                "project_deadline": row.project_deadline,
            }
            for row in rows
        ]
        stats["open_tasks"] = len(open_tasks)

        # --- Personal tally: one GROUP BY over my submissions ----------
        counts = dict(
            db.session.execute(
                select(Submission.status, func.count(Submission.id))
                .where(Submission.annotator_id == current_user.id)
                .group_by(Submission.status)
            ).all()
        )
        stats["completed"] = sum(counts.values())
        stats["approved"] = counts.get("approved", 0)
        stats["rejected"] = counts.get("rejected", 0)
        stats["awaiting_review"] = counts.get("submitted", 0) + counts.get(
            "revision", 0
        )

        # --- Earnings ---------------------------------------------------
        # This month = approved work whose QA approval (reviewed_at)
        # falls in the current UTC month; all-time from the model helper.
        month_start = utc_now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        stats["earnings_month"] = money(
            db.session.scalar(
                select(func.coalesce(func.sum(Project.pay_per_task), 0))
                .select_from(Submission)
                .join(Submission.task)
                .join(Task.project)
                .where(
                    Submission.annotator_id == current_user.id,
                    Submission.status == "approved",
                    Submission.reviewed_at >= month_start,
                )
            )
            or 0
        )
        stats["earnings_all"] = current_user.get_total_earnings()

        # --- Activity pulse: today, last 7 days, active streak (UTC) ---
        day_start = utc_now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_start = day_start - timedelta(days=6)
        stats["today"] = db.session.scalar(
            select(func.count(Submission.id)).where(
                Submission.annotator_id == current_user.id,
                Submission.submitted_at >= day_start,
            )
        ) or 0
        stats["week"] = db.session.scalar(
            select(func.count(Submission.id)).where(
                Submission.annotator_id == current_user.id,
                Submission.submitted_at >= week_start,
            )
        ) or 0
        days_worked = {
            row[0].date()
            for row in db.session.execute(
                select(Submission.submitted_at).where(
                    Submission.annotator_id == current_user.id,
                    Submission.submitted_at.is_not(None),
                )
            )
        }
        streak, cursor = 0, utc_now().date()
        if cursor not in days_worked:
            cursor -= timedelta(days=1)  # streak survives until midnight
        while cursor in days_worked:
            streak += 1
            cursor -= timedelta(days=1)
        stats["streak"] = streak

        # --- Recent submissions (display columns only) -------------------
        recent_rows = db.session.execute(
            select(
                Submission.status,
                Submission.qa_score,
                Submission.submitted_at,
                Task.id.label("task_id"),
                Project.name.label("project_name"),
            )
            .join(Submission.task)
            .join(Task.project)
            .where(Submission.annotator_id == current_user.id)
            .order_by(Submission.submitted_at.desc(), Submission.id.desc())
            .limit(5)
        ).all()
        recent = [
            {
                "task_id": row.task_id,
                "project_name": row.project_name,
                "status": row.status,
                "qa_score": row.qa_score,
                "submitted_at": row.submitted_at,
            }
            for row in recent_rows
        ]
    except Exception:
        current_app.logger.exception("Annotator dashboard stats failed.")
        flash("Dashboard statistics could not be loaded.", "danger")

    claim_cap = int(current_app.config.get("CLAIM_DAILY_CAP", 20))
    claim_day_start = utc_now().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    try:
        claimed_today = db.session.scalar(
            select(func.count(Task.id)).where(
                Task.assigned_to == current_user.id,
                Task.claimed_at.is_not(None),
                Task.claimed_at >= claim_day_start,
            )
        ) or 0
    except Exception:
        current_app.logger.exception("Claim counter query failed.")
        claimed_today = 0
    try:
        pending_quizzes = _pending_quizzes()
    except Exception:
        current_app.logger.exception("Pending quizzes query failed.")
        pending_quizzes = []
    return render_template(
        "annotator/dashboard.html", stats=stats, open_tasks=open_tasks,
        claimed_today=claimed_today, claim_cap=claim_cap,
        recent=recent, pending_quizzes=pending_quizzes,
    )


@annotator_bp.route("/dashboard")
@annotator_required
def dashboard_alias():
    """Redirect the legacy /annotator/dashboard path to /annotator/.

    Returns:
        A redirect response to the canonical dashboard URL.
    """
    return redirect(url_for("annotator.dashboard"))


@annotator_bp.route("/tasks")
@annotator_required
def tasks_alias():
    """Send the navbar's 'My Tasks' link to the dashboard queue section.

    Returns:
        A redirect to the dashboard's task-queue anchor.
    """
    return redirect(url_for("annotator.dashboard", _anchor="my-tasks"))


@annotator_bp.route("/task/<int:task_id>", methods=["GET", "POST"])
@annotator_required
def task_page(task_id: int):
    """Show a task and accept the annotator's answer for it.

    GET renders the project instructions, the input data and a dynamic
    output form for the project's task type. POST validates the answer,
    creates a new Submission and flips the task to 'submitted' in the
    same transaction. The transition is conditional on the task still
    being in a submittable state, which makes double submissions
    impossible even under concurrent posts.

    Args:
        task_id: Primary key of the task.

    Returns:
        The rendered task page, or a redirect with flash feedback.
        404 for unknown ids and tasks not assigned to the requester.
    """
    task = _load_own_task(task_id)
    if task is None:
        abort(404)

    # Work that QA already has (or has approved) can never be reopened
    # from here; rejected tasks re-open for revision.
    if task.status == "submitted":
        flash(
            f"Task #{task.id} is already awaiting QA review.", "info"
        )
        return redirect(url_for("annotator.dashboard"))

    # Qualification gate: an active quiz on this project demands a
    # passed (or exempt) attempt before the workspace opens.
    try:
        blocker = _gate_blocker(task.project_id)
    except Exception:
        current_app.logger.exception("Quiz gate check failed.")
        blocker = None
    if blocker is not None:
        flash(
            "Pass the qualification quiz for this project before"
            " working on its tasks.",
            "warning",
        )
        return redirect(
            url_for("annotator.take_quiz", quiz_id=blocker.id)
        )
    if task.status == "approved":
        flash(f"Task #{task.id} was already approved. Nice work!", "success")
        return redirect(url_for("annotator.dashboard"))
    if task.status not in SUBMITTABLE_STATUSES:
        abort(404)

    project = task.project
    rubric_dims = parse_dimensions(
        project.rubric_dimensions or ""
    )[0] if project.task_type == "preference" else []
    # Media workspaces (Phase 8): the task document carries the media
    # reference; a broken/dangling reference degrades to the
    # plain-text fallback instead of bricking the task.
    media_doc = None
    media_url = None
    if project.task_type in ("image_labeling", "audio_transcription"):
        media_doc = _media_input_document(task.input_data)
        if media_doc is not None:
            media_asset = db.session.get(
                MediaAsset, media_doc["media_id"]
            )
            if (media_asset is not None
                    and media_asset.project_id == project.id):
                media_url = url_for(
                    "annotator.task_media",
                    task_id=task.id,
                    asset_id=media_asset.id,
                )
            else:
                media_doc = None
    rendered_prompt = (
        render_rich(media_doc["prompt"])
        if media_doc and (media_doc.get("prompt") or "").strip()
        else None
    )
    media_labels = (
        _media_allowed_labels(media_doc)
        if project.task_type == "image_labeling" else []
    )
    form = _submission_form_class(
        project.task_type,
        rubric_dims,
        int(current_app.config.get("RUBRIC_SCALE_MIN", 1)),
        int(current_app.config.get("RUBRIC_SCALE_MAX", 5)),
        label_choices=media_labels,
    )()
    form._rubric_dims = rubric_dims
    pref_input = None
    pref_views = None
    flipped = False
    if project.task_type == "preference":
        pref_input = _preference_input_document(task.input_data)
        if pref_input is None:
            # A preference project should only hold preference tasks;
            # a malformed row must never 500 the annotator's workspace.
            current_app.logger.error(
                "Task %s has non-preference input in a preference"
                " project.", task.id,
            )
            flash(
                "This task's data is malformed. Please contact Brydl"
                " support.",
                "danger",
            )
            return redirect(url_for("annotator.dashboard"))
        flipped = _preference_flipped(task.id, current_user.id)
        form._flipped = flipped
        pref_views = {
            "prompt": render_rich(pref_input["prompt"]),
            "response_a": render_rich(pref_input["response_a"]),
            "response_b": render_rich(pref_input["response_b"]),
        }

    if form.validate_on_submit():
        output = _build_output_data(project.task_type, form)
        submission = Submission(
            task_id=task.id,
            annotator_id=current_user.id,
            # ensure_ascii=False keeps Hausa/Yoruba characters readable
            # in the stored JSON.
            output_data=json.dumps(output, ensure_ascii=False),
        )
        try:
            log_event(
                "submission.create",
                actor=current_user,
                entity_type="task",
                entity_id=task.id,
                detail={
                    "project_id": project.id,
                    "kind": output.get("kind"),
                    "revision": task.status == "rejected",
                },
            )
            db.session.add(submission)
            # Race-safe transition: the UPDATE only matches a task still
            # in a submittable state while the write lock is held, so a
            # concurrent double-post cannot create a second scoring row.
            transitioned = db.session.execute(
                update(Task)
                .where(
                    Task.id == task.id,
                    Task.status.in_(SUBMITTABLE_STATUSES),
                )
                .values(status="submitted")
            ).rowcount
            if transitioned != 1:
                db.session.rollback()
                flash(
                    f"Task #{task.id} was already submitted.", "warning"
                )
                return redirect(url_for("annotator.dashboard"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Submission failed for task %s by user %s.",
                task.id,
                current_user.id,
            )
            flash(
                "Could not save your answer. Please try again.", "danger"
            )
            return _render_task_page(
                task, form, pref_input, flipped, pref_views,
                rubric_dims=rubric_dims,
                media_doc=media_doc, media_url=media_url,
                rendered_prompt=rendered_prompt,
                media_labels=media_labels
            )

        if task.consensus_group_id is not None:
            # Bookkeeping only: never let evaluation break a good submit.
            refresh_group(task.consensus_group_id)
            db.session.commit()
        flash(
            f"Answer submitted for task #{task.id}. It is now awaiting"
            " QA review.",
            "success",
        )
        return redirect(url_for("annotator.dashboard"))

    if form.csrf_token.errors:
        flash("Security check failed. Please try again.", "warning")
    return _render_task_page(
        task, form, pref_input, flipped, pref_views,
        rubric_dims=rubric_dims,
        media_doc=media_doc, media_url=media_url,
        rendered_prompt=rendered_prompt,
        media_labels=media_labels
    )


@annotator_bp.route("/claim", methods=["POST"], endpoint="claim_task")
@annotator_required
def claim_task():
    """Self-claim the oldest pending task from an active project.

    Race-safe by construction: a conditional UPDATE flips exactly one
    still-pending task to assigned for this annotator while the write
    lock is held — two annotators clicking Claim at the same instant
    can never receive the same task. A per-UTC-day cap (config
    CLAIM_DAILY_CAP) keeps self-service volume fair.

    Returns:
        A redirect (PRG) back to the dashboard with flash feedback.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("annotator.dashboard"))

    cap = int(current_app.config.get("CLAIM_DAILY_CAP", 20))
    day_start = utc_now().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # The claim is a read (cap) then a conditional write. Under a
    # simultaneous burst, SQLite can refuse the read->write upgrade
    # (snapshot conflict); PostgreSQL serializes cleanly. A bounded
    # retry makes the claim deterministic on both engines without
    # letting anyone double-claim: the UPDATE re-checks 'pending'
    # under the write lock on every attempt.
    claimed_today = 0
    for attempt in range(3):
        try:
            claimed_today = db.session.scalar(
                select(func.count(Task.id)).where(
                    Task.assigned_to == current_user.id,
                    Task.claimed_at.is_not(None),
                    Task.claimed_at >= day_start,
                )
            ) or 0
            if claimed_today >= cap:
                flash(
                    f"You have claimed {claimed_today} task(s) today"
                    f" — the daily limit is {cap}. Assigned work is"
                    " unaffected.",
                    "info",
                )
                return redirect(url_for("annotator.dashboard"))

            # Blind-consensus guard: never let one annotator hold two
            # siblings of the same round (their own assignments count,
            # whatever their status).
            owned_groups = set(
                db.session.scalars(
                    select(Task.consensus_group_id).where(
                        Task.assigned_to == current_user.id,
                        Task.consensus_group_id.is_not(None),
                    )
                ).all()
            )
            eligible = (
                select(Task.id)
                .join(Task.project)
                .where(
                    Task.status == "pending",
                    Project.status == "active",
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
            )
            # Qualification gate: skip projects whose active quiz the
            # caller has not passed (or been exempted from).
            gated = exists().where(
                QualificationQuiz.project_id == Project.id,
                QualificationQuiz.status == "active",
                ~exists().where(
                    QuizAttempt.quiz_id == QualificationQuiz.id,
                    QuizAttempt.annotator_id == current_user.id,
                    QuizAttempt.status.in_(("passed", "exempt")),
                ),
            )
            eligible = eligible.where(~gated)
            if owned_groups:
                eligible = eligible.where(
                    Task.consensus_group_id.is_(None)
                    | Task.consensus_group_id.notin_(owned_groups)
                )
            oldest_pending = eligible.limit(1).scalar_subquery()
            result = db.session.execute(
                update(Task)
                .where(
                    Task.id == oldest_pending,
                    Task.status == "pending",
                )
                .values(
                    status="assigned",
                    assigned_to=current_user.id,
                    claimed_at=utc_now(),
                )
            )
            if result.rowcount != 1:
                db.session.rollback()
                flash(
                    "No tasks are available to claim right now — ask"
                    " an admin to upload more work.",
                    "info",
                )
                return redirect(url_for("annotator.dashboard"))
            db.session.commit()
            break
        except Exception:
            db.session.rollback()
            if attempt == 2:
                current_app.logger.exception(
                    "Task claim failed for user %s.", current_user.id
                )
                flash(
                    "Could not claim a task. Please try again.",
                    "danger",
                )
                return redirect(url_for("annotator.dashboard"))
            time.sleep(0.05 * (attempt + 1))

    log_event(
        "task.claim",
        actor=current_user,
        entity_type="user",
        entity_id=current_user.id,
        detail={"claimed_today": claimed_today + 1},
        commit=True,
    )
    flash("Task claimed! It is now at the top of your queue.", "success")
    return redirect(url_for("annotator.dashboard"))


@annotator_bp.route("/notifications", endpoint="notifications")
@annotator_required
def notifications():
    """Show the annotator's notification feed and mark it read.

    Newest first, capped at 50 entries; opening the page clears the
    unread badge (a single conditional UPDATE). A database problem
    renders an empty feed with a flash, never a 500.

    Returns:
        The rendered notifications template.
    """
    items: list = []
    try:
        rows = db.session.scalars(
            select(Notification)
            .where(Notification.user_id == current_user.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(50)
        ).all()
        db.session.execute(
            update(Notification)
            .where(
                Notification.user_id == current_user.id,
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
        )
        db.session.commit()
        items = rows
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Notification feed failed.")
        flash("Could not load notifications. Please try again.", "danger")
    return render_template("annotator/notifications.html", items=items)


@annotator_bp.route("/history")
@annotator_required
def history():
    """List the annotator's submissions, newest first, paginated.

    Returns:
        The rendered history template. Out-of-range pages redirect to
        the last valid page.
    """
    rows: list = []
    pagination = None
    try:
        page = max(request.args.get("page", 1, type=int) or 1, 1)
        # db.paginate() returns scalars, so the statement selects the
        # Submission entity only; task/project details for the page's
        # rows are resolved afterwards in two batched IN queries.
        stmt = (
            select(Submission)
            .where(Submission.annotator_id == current_user.id)
            .order_by(
                Submission.submitted_at.desc(), Submission.id.desc()
            )
        )
        pagination = db.paginate(
            stmt, page=page, per_page=HISTORY_PER_PAGE, error_out=False
        )
        if page > pagination.pages and pagination.pages > 0:
            return redirect(
                url_for("annotator.history", page=pagination.pages)
            )
        submissions = list(pagination.items)
        task_ids = {submission.task_id for submission in submissions}
        task_info: dict[int, tuple[str, float]] = {}
        if task_ids:
            task_info = {
                tid: (name, pay)
                for tid, name, pay in db.session.execute(
                    select(
                        Task.id,
                        Project.name,
                        Project.pay_per_task,
                    )
                    .join(Task.project)
                    .where(Task.id.in_(task_ids))
                ).all()
            }
        rows = [
            {
                "submission": submission,
                "task_id": submission.task_id,
                "project_name": task_info.get(
                    submission.task_id, ("—", 0.0)
                )[0],
                "pay": task_info.get(submission.task_id, ("—", 0.0))[1],
            }
            for submission in submissions
        ]
    except Exception:
        current_app.logger.exception("Submission history failed.")
        flash("History could not be loaded.", "danger")

    return render_template(
        "annotator/history.html", rows=rows, pagination=pagination
    )


# ---------------------------------------------------------------------------
# Profile, terms acceptance, theme, continue-working
# ---------------------------------------------------------------------------


class ProfileForm(FlaskForm):
    """Display-name form on the annotator profile page."""

    name = StringField(
        "Display name",
        validators=[
            DataRequired(message="Name is required."),
            Length(max=120, message="Name must be 120 characters or less."),
        ],
    )
    # Optional contact number (payment questions). Nigerian local
    # numbers (11 digits, leading 0) and international (+234...) both
    # match; whitespace is stripped before storage downstream.
    phone = StringField(
        "Phone number (optional)",
        validators=[
            Optional(),
            Regexp(
                r"^\+?[0-9]{7,15}$",
                message="Enter 7-15 digits, optionally starting with"
                        " + (e.g. 08012345678).",
            ),
        ],
    )
    # Distinct submit-field names: two forms share one page, so the
    # clicked form must be identifiable from the POST body alone.
    # (Field NAME stays save_name for stable test/audit contracts;
    # only the rendered label covers details beyond the name.)
    save_name = SubmitField("Save details")


class PasswordForm(FlaskForm):
    """Self-service password change (requires the current password)."""

    current_password = PasswordField(
        "Current password",
        validators=[DataRequired(message="Enter your current password.")],
    )
    new_password = PasswordField(
        "New password",
        validators=[
            DataRequired(message="New password is required."),
            Length(
                min=8,
                max=128,
                message="Password must be between 8 and 128 characters.",
            ),
        ],
    )
    confirm_password = PasswordField(
        "Repeat new password",
        validators=[
            DataRequired(message="Repeat the new password."),
            EqualTo("new_password", message="The two passwords do not"
                    " match."),
        ],
    )
    save_password = SubmitField("Change password")


@annotator_bp.route("/profile", methods=["GET", "POST"], endpoint="profile")
@annotator_required
def profile():
    """Annotator account page: rename, change password, theme, terms.

    Two independent forms share the page; the clicked form's submit
    field decides which update runs. CSRF is validated up front, the
    current password is verified before any change, and every change
    is audited (user.profile_update / user.password_change).

    Returns:
        The rendered profile template with validation feedback, or a
        redirect after a successful update.
    """
    pform = ProfileForm(obj=current_user)
    pwform = PasswordForm()
    bankform = BankDetailsForm(obj=current_user)
    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please try again.", "warning")
            return redirect(url_for("annotator.profile"))
        if pform.save_name.data:
            if pform.validate():
                new_name = pform.name.data.strip()
                new_phone = (pform.phone.data or "").strip() or None
                if (new_name != current_user.name
                        or new_phone != current_user.phone):
                    current_user.name = new_name
                    current_user.phone = new_phone
                    log_event(
                        "user.profile_update",
                        actor=current_user,
                        entity_type="user",
                        entity_id=current_user.id,
                        # The phone number itself never enters the
                        # audit trail (PII minimisation).
                        detail={"field": "profile_details"},
                        commit=True,
                    )
                    flash("Your details have been updated.", "success")
                else:
                    flash("Profile unchanged.", "info")
                return redirect(url_for("annotator.profile"))
        elif pwform.save_password.data:
            if pwform.validate():
                if not current_user.check_password(
                    pwform.current_password.data
                ):
                    flash("Current password is incorrect.", "danger")
                    return redirect(url_for("annotator.profile"))
                current_user.set_password(pwform.new_password.data)
                # Invalidate every session/remember-me cookie for this
                # account — including this very one (sign in again).
                current_user.auth_version += 1
                log_event(
                    "user.password_change",
                    actor=current_user,
                    entity_type="user",
                    entity_id=current_user.id,
                    detail={"via": "self-service"},
                    commit=True,
                )
                logout_user()
                flash(
                    "Password changed. Please sign in with your new"
                    " password.",
                    "success",
                )
                return redirect(url_for("auth.login"))
    return render_template(
        "annotator/profile.html", pform=pform, pwform=pwform,
        bankform=bankform,
    )


@annotator_bp.route("/terms", endpoint="terms")
@annotator_required
def terms():
    """Show the current Terms & Conditions with acceptance status.

    Returns:
        The rendered terms template; includes the acceptance form when
        this annotator has not yet accepted the current version.
    """
    version = current_app.config.get("TERMS_VERSION", "1.0")
    accepted = current_user.terms_version == version
    return render_template(
        "annotator/terms.html", version=version, accepted=accepted
    )


@annotator_bp.route(
    "/terms/accept", methods=["POST"], endpoint="accept_terms"
)
@annotator_required
def accept_terms():
    """Record acceptance of the current terms version (POST + CSRF).

    Stores the version and UTC timestamp on the account and writes a
    ``user.terms_accept`` audit row — the consent record.

    Returns:
        A redirect to the dashboard (or back to the terms page when
        the CSRF check fails).
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("annotator.terms"))
    version = current_app.config.get("TERMS_VERSION", "1.0")
    current_user.terms_version = version
    current_user.terms_accepted_at = utc_now()
    log_event(
        "user.terms_accept",
        actor=current_user,
        entity_type="user",
        entity_id=current_user.id,
        detail={"version": version},
        commit=True,
    )
    flash("Thank you — the terms are accepted. You're all set!", "success")
    return redirect(url_for("annotator.dashboard"))


@annotator_bp.route(
    "/continue", methods=["POST"], endpoint="continue_work"
)
@annotator_required
def continue_work():
    """Jump straight into the oldest open task in the annotator's queue.

    One tap from the dashboard: opens the same task page as the queue's
    first Start/Revise/Continue button (the dashboard queue's ordering,
    so rejected revisions surface first).

    Returns:
        A redirect to the task page, or back to the dashboard with a
        friendly note when the queue is empty (or the CSRF check
        fails).
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("annotator.dashboard"))
    task = db.session.scalars(
        select(Task)
        .where(
            Task.assigned_to == current_user.id,
            Task.status.in_(SUBMITTABLE_STATUSES),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
    ).first()
    if task is None:
        flash(
            "No open tasks right now — new work appears here as soon as"
            " it's assigned to you.",
            "info",
        )
        return redirect(url_for("annotator.dashboard"))
    return redirect(url_for("annotator.task_page", task_id=task.id))


class BankDetailsForm(FlaskForm):
    """Payout account form (annotator-supplied bank details)."""

    bank_name = StringField(
        "Bank name",
        validators=[
            DataRequired(message="Bank name is required."),
            Length(max=120, message="Bank name must be 120 characters"
                   " or less."),
        ],
    )
    account_type = SelectField(
        "Account type",
        choices=[
            ("", "Choose the account type…"),
            ("savings", "Savings account"),
            ("current", "Current account"),
        ],
        validators=[
            DataRequired(message="Choose the account type."),
            AnyOf(("savings", "current"),
                  message="Account type must be savings or current."),
        ],
    )
    account_number = StringField(
        "Account number",
        validators=[
            DataRequired(message="Account number is required."),
            Length(
                min=10,
                max=10,
                message="Enter the 10-digit NUBAN account number.",
            ),
        ],
    )
    account_name = StringField(
        "Account name",
        validators=[
            DataRequired(message="Account name is required."),
            Length(
                max=120,
                message="Account name must be 120 characters or less.",
            ),
        ],
    )
    save_bank = SubmitField("Save payout account")
    remove_bank = SubmitField("Remove payout account")


# Nigerian NUBAN: exactly ten digits. StringField + validator (not
# IntegerField) because leading zeros are real and must survive.
_BANK_NUMBER_RE = re.compile(r"^\d{10}$")


@annotator_bp.route(
    "/profile/bank-details", methods=["POST"], endpoint="bank_details"
)
@annotator_required
def bank_details():
    """Save or remove the annotator's payout account (POST + CSRF).

    A separate endpoint from the profile page so each concern has one
    form and one audit trail. The account number must be exactly ten
    digits (NUBAN). Details are shown to admins on the Team page
    (masked) and included in bank-batch payout CSVs (full, for the
    payment run). The audit row never contains the account number.

    Returns:
        A redirect back to the profile with flash feedback.
    """
    form = BankDetailsForm()
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("annotator.profile"))

    # Removal runs FIRST and skips the required-field validators — an
    # empty form plus the Remove button is the whole point.
    if form.remove_bank.data:
        had_details = current_user.bank_name is not None
        current_user.bank_name = None
        current_user.bank_account_number = None
        current_user.bank_account_last4 = None
        current_user.bank_account_name = None
        current_user.bank_account_type = None
        if had_details:
            log_event(
                "user.bank_details_remove",
                actor=current_user,
                entity_type="user",
                entity_id=current_user.id,
                commit=True,
            )
            flash("Payout account removed.", "success")
        else:
            flash("No payout account saved.", "info")
        return redirect(url_for("annotator.profile"))

    if not form.validate_on_submit():
        for field in (form.bank_name, form.account_type,
                      form.account_number, form.account_name):
            for error in field.errors:
                flash(error, "danger")
        return redirect(url_for("annotator.profile"))

    number = form.account_number.data.strip()
    if not _BANK_NUMBER_RE.match(number):
        flash("Account number must be exactly 10 digits (NUBAN).",
              "danger")
        return redirect(url_for("annotator.profile"))

    current_user.bank_name = form.bank_name.data.strip()
    current_user.set_bank_account(number)
    current_user.bank_account_name = form.account_name.data.strip()
    current_user.bank_account_type = form.account_type.data
    log_event(
        "user.bank_details_update",
        actor=current_user,
        entity_type="user",
        entity_id=current_user.id,
        detail={
            "bank": current_user.bank_name,
            "type": current_user.bank_account_type,
        },
        commit=True,
    )
    flash(
        "Payout account saved. Brydl admins can now see it for payment"
        " runs.",
        "success",
    )
    return redirect(url_for("annotator.profile"))


# ===== Qualification quiz (Phase 9) ===================================

def _quiz_form_class(questions: list[dict]) -> type[FlaskForm]:
    """Build the answer form for one quiz's canonical questions.

    Field names are canonical ("q_<index>"); only option TEXTS reach
    the client — the correct-answer flags stay server-side, so the
    render path can never leak the key.

    Args:
        questions: The canonical question list.

    Returns:
        A FlaskForm subclass with one required RadioField per
        question.
    """
    attrs: dict = {
        "__doc__": "Qualification quiz answer form.",
        "submit": SubmitField("Submit answers"),
    }
    for index, question in enumerate(questions):
        attrs[f"q_{index}"] = RadioField(
            question["text"],
            choices=[
                (str(option_index), option["text"])
                for option_index, option in enumerate(
                    question["options"]
                )
            ],
            validators=[
                DataRequired(message="Choose an answer for every"
                             " question."),
            ],
        )
    return type("QuizAnswerForm", (FlaskForm,), attrs)


@annotator_bp.route("/quiz/<int:quiz_id>", methods=["GET", "POST"])
@annotator_required
def take_quiz(quiz_id: int):
    """Take one qualification quiz (annotator-only, CSRF).

    GET renders every question in the annotator's deterministic order
    (blake2b over quiz+annotator — a refresh never reshuffles a
    half-answered attempt). POST grades server-side against the
    canonical payload: pass stores a 'passed' attempt and opens the
    project; fail stores the score and counts against max_attempts.
    Already-qualified annotators and exhausted attempts are refused.

    Args:
        quiz_id: Primary key of the quiz.

    Returns:
        The quiz page, or a redirect with flash feedback. 404 for
        unknown quizzes.
    """
    quiz = db.session.get(QualificationQuiz, quiz_id)
    if quiz is None:
        abort(404)
    project = quiz.project
    if quiz.status != "active" or project.status != "active":
        flash("This quiz is not open right now.", "info")
        return redirect(url_for("annotator.dashboard"))
    qualified = db.session.scalar(
        select(QuizAttempt.id).where(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.annotator_id == current_user.id,
            QuizAttempt.status.in_(("passed", "exempt")),
        )
    )
    if qualified is not None:
        flash("You already qualify for this project.", "info")
        return redirect(url_for("annotator.dashboard"))
    attempts_used = db.session.scalar(
        select(func.count(QuizAttempt.id)).where(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.annotator_id == current_user.id,
            QuizAttempt.status == "failed",
        )
    ) or 0
    if attempts_used >= quiz.max_attempts:
        flash(
            "You have used all your attempts for this quiz. Contact"
            " Brydl support to request a reset.",
            "danger",
        )
        return redirect(url_for("annotator.dashboard"))

    questions = quiz_engine.load_questions(quiz.questions_json)
    if not questions:
        flash("This quiz is not available right now.", "danger")
        return redirect(url_for("annotator.dashboard"))
    form = _quiz_form_class(questions)()
    if form.validate_on_submit():
        answers = {}
        for index in range(len(questions)):
            field = getattr(form, f"q_{index}")
            answers[f"q_{index}"] = int(field.data)
        correct, total = quiz_engine.grade(questions, answers)
        score = quiz_engine.score_percent(correct, total)
        passed = score >= quiz.pass_score
        try:
            db.session.add(QuizAttempt(
                quiz_id=quiz.id,
                annotator_id=current_user.id,
                status="passed" if passed else "failed",
                correct_count=correct,
                total_count=total,
                score_percent=score,
                answers_json=json.dumps(answers),
            ))
            log_event(
                "quiz.attempt",
                actor=current_user,
                entity_type="qualification_quiz",
                entity_id=quiz.id,
                detail={
                    "project_id": project.id,
                    "passed": passed,
                    "score_percent": score,
                    "attempt": attempts_used + 1,
                },
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Quiz attempt failed for user %s quiz %s.",
                current_user.id, quiz.id,
            )
            flash("Could not save your attempt. Please try again.",
                  "danger")
            return redirect(
                url_for("annotator.take_quiz", quiz_id=quiz.id)
            )
        if passed:
            flash(
                f"Passed with {score}% — you can now work on"
                f" {project.name}.",
                "success",
            )
        else:
            left = quiz.max_attempts - (attempts_used + 1)
            extra = (
                f" {left} attempt(s) left."
                if left > 0
                else " That was your last attempt."
            )
            flash(
                f"Scored {score}% — the pass mark is"
                f" {quiz.pass_score}%.{extra}",
                "danger",
            )
        return redirect(url_for("annotator.dashboard"))

    order = quiz_engine.display_order(
        quiz.id, current_user.id, len(questions)
    )
    attempts_left = quiz.max_attempts - attempts_used
    return render_template(
        "annotator/quiz.html",
        quiz=quiz,
        project=project,
        questions=questions,
        order=order,
        form=form,
        attempts_left=attempts_left,
    )
