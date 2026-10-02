"""Shared staff surface: profile, notifications, feedback.

Used by project_manager / qa / auditor (and a subset of routes is
reachable by annotators too — everyone gets a profile and feedback).
Admin reuses the same profile + messaging templates via their own
routes, but admin routes live in the admin blueprint where they always
have.
"""


from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from flask_wtf.csrf import ValidationError, validate_csrf
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from wtforms import PasswordField, StringField, SubmitField, TextAreaField
from wtforms.validators import (
    DataRequired,
    EqualTo,
    Length,
    ValidationError as FormValidationError,
)

from audit import log_event
from extensions import db
from models import (
    FeedbackReply,
    FeedbackThread,
    Notification,
    User,
    utc_now,
)

staff_bp = Blueprint("staff", __name__)


# ---------- Forms ----------------------------------------------------------


class ProfileForm(FlaskForm):
    name = StringField(
        "Display name",
        validators=[
            DataRequired(message="Name is required."),
            Length(max=120),
        ],
    )
    submit = SubmitField("Save profile")


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField(
        "Current password",
        validators=[DataRequired(message="Enter your current password.")],
    )
    new_password = PasswordField(
        "New password",
        validators=[
            DataRequired(message="Enter a new password."),
            Length(min=10, max=128, message="Password must be at least 10 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[
            DataRequired(),
            EqualTo("new_password", message="Passwords must match."),
        ],
    )
    submit = SubmitField("Change password")

    def validate_current_password(self, field):
        if not current_user.check_password(field.data):
            raise FormValidationError("Current password is incorrect.")


class FeedbackForm(FlaskForm):
    subject = StringField(
        "Subject",
        validators=[DataRequired(message="Enter a subject."), Length(max=200)],
    )
    body = TextAreaField(
        "Message",
        validators=[
            DataRequired(message="Write a message."),
            Length(max=4000, message="Message is too long (max 4000 chars)."),
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


def _unread_bell_count(user_id: int) -> int:
    return db.session.scalar(
        select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    ) or 0


def _allowed_to_view_thread(thread: FeedbackThread) -> bool:
    if current_user.role == "admin":
        return True
    return thread.author_id == current_user.id and thread.kind == "feedback"


# ---------- Profile --------------------------------------------------------


@staff_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Self-serve profile: name + password. Email is admin-only to change
    (security: email = login identifier). Bank details live on the
    annotator-only /annotator/profile page."""
    form = ProfileForm(obj=current_user)
    pw_form = PasswordChangeForm()
    if form.submit.data and form.validate_on_submit():
        current_user.name = form.name.data.strip()
        log_event(
            "profile.update",
            actor=current_user,
            entity_type="user",
            entity_id=current_user.id,
        )
        db.session.commit()
        flash("Profile saved.", "success")
        return redirect(url_for("staff.profile"))
    if pw_form.submit.data and pw_form.validate_on_submit():
        current_user.set_password(pw_form.new_password.data)
        current_user.auth_version = (current_user.auth_version or 0) + 1
        log_event(
            "auth.password.change",
            actor=current_user,
            entity_type="user",
            entity_id=current_user.id,
        )
        db.session.commit()
        flash("Password changed. You'll need to sign in again on other devices.", "success")
        return redirect(url_for("staff.profile"))
    return render_template(
        "staff/profile.html",
        form=form,
        pw_form=pw_form,
        unread_notifications=_unread_bell_count(current_user.id),
    )


# ---------- Notifications (bell) ------------------------------------------


@staff_bp.route("/notifications")
@login_required
def notifications():
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    stmt = (
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
    )
    pagination = db.paginate(stmt, page=page, per_page=30, error_out=False)
    # Mark first page as read when opened directly.
    if page == 1:
        db.session.execute(
            Notification.__table__.update()
            .where(
                Notification.user_id == current_user.id,
                Notification.read_at.is_(None),
            )
            .values(read_at=utc_now())
        )
        db.session.commit()
    return render_template(
        "staff/notifications.html",
        pagination=pagination,
        items=pagination.items,
        unread_notifications=0,
    )


# ---------- Feedback composer + inbox -------------------------------------


@staff_bp.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback_inbox():
    """If admin: inbox with all threads. Otherwise: list of *my* feedback
    threads + a composer."""
    form = FeedbackForm()
    reply_form = ReplyForm()
    if form.validate_on_submit() and current_user.role != "admin":
        thread = FeedbackThread(
            kind="feedback",
            subject=form.subject.data.strip(),
            body=form.body.data.strip(),
            author_id=current_user.id,
            status="new",
        )
        db.session.add(thread)
        # Notify every admin.
        admins = db.session.scalars(
            select(User).where(User.role == "admin", User.is_active.is_(True))
        ).all()
        for a in admins:
            from notifications import notify as _notify
            _notify(
                a,
                "feedback.new",
                payload={
                    "thread_id": None,  # flush will populate after commit
                    "subject": thread.subject,
                    "from": current_user.name,
                },
            )
        log_event(
            "feedback.create",
            actor=current_user,
            detail={"subject": thread.subject},
        )
        db.session.commit()
        flash("Your message has been sent to admin.", "success")
        return redirect(url_for("staff.feedback_inbox"))
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    if current_user.role == "admin":
        threads = db.paginate(
            select(FeedbackThread)
            .options(joinedload(FeedbackThread.author))
            .order_by(
                FeedbackThread.status == "resolved",
                FeedbackThread.created_at.desc(),
            ),
            page=page, per_page=20, error_out=False,
        )
    else:
        threads = db.paginate(
            select(FeedbackThread)
            .where(FeedbackThread.author_id == current_user.id)
            .order_by(FeedbackThread.created_at.desc()),
            page=page, per_page=20, error_out=False,
        )
    # Broadcasts visible to the current user (recent only).
    broadcasts = db.session.scalars(
        select(FeedbackThread)
        .where(FeedbackThread.kind == "broadcast")
        .order_by(FeedbackThread.created_at.desc())
        .limit(10)
    ).all()
    unread = _unread_bell_count(current_user.id)
    return render_template(
        "staff/feedback.html",
        threads=threads.items if hasattr(threads, "items") else threads,
        pagination=threads if hasattr(threads, "has_next") else None,
        broadcasts=broadcasts,
        form=form,
        reply_form=reply_form,
        unread_notifications=unread,
    )


@staff_bp.route("/feedback/<int:thread_id>", methods=["GET", "POST"])
@login_required
def feedback_thread(thread_id: int):
    thread = db.session.get(FeedbackThread, thread_id)
    if thread is None:
        abort(404)
    if not _allowed_to_view_thread(thread):
        abort(403)
    form = ReplyForm()
    if form.validate_on_submit():
        # Admin can post a reply; the original author can reply to their
        # own thread to follow up.
        is_author = thread.author_id == current_user.id
        if current_user.role != "admin" and not (
            is_author and thread.kind == "feedback"
        ):
            abort(403)
        reply = FeedbackReply(
            thread_id=thread.id,
            author_id=current_user.id,
            body=form.body.data.strip(),
        )
        db.session.add(reply)
        # Admin reply reopens a resolved thread, marks it 'reviewed'
        # when it was 'new'. Author follow-up moves resolved back to
        # reviewed so admin sees it again.
        if current_user.role == "admin":
            if thread.status == "new":
                thread.status = "reviewed"
            # Notify the original author.
            from notifications import notify as _notify
            _notify(
                thread.author,
                "feedback.reply",
                payload={"thread_id": thread.id, "subject": thread.subject},
            )
        else:
            if thread.status == "resolved":
                thread.status = "reviewed"
            for a in db.session.scalars(
                select(User).where(User.role == "admin", User.is_active.is_(True))
            ).all():
                from notifications import notify as _notify
                _notify(
                    a,
                    "feedback.reply",
                    payload={
                        "thread_id": thread.id,
                        "subject": thread.subject,
                        "from": current_user.name,
                    },
                )
        log_event(
            "feedback.reply",
            actor=current_user,
            entity_type="feedback_thread",
            entity_id=thread.id,
        )
        db.session.commit()
        flash("Reply sent.", "success")
        return redirect(url_for("staff.feedback_thread", thread_id=thread.id))
    unread = _unread_bell_count(current_user.id)
    return render_template(
        "staff/feedback_thread.html",
        thread=thread,
        form=form,
        unread_notifications=unread,
    )


@staff_bp.route("/feedback/<int:thread_id>/resolve", methods=["POST"])
@login_required
def feedback_resolve(thread_id: int):
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please reload and try again.", "warning")
        return redirect(url_for("staff.feedback_thread", thread_id=thread_id))
    if current_user.role != "admin":
        abort(403)
    thread = db.session.get(FeedbackThread, thread_id)
    if thread is None:
        abort(404)
    thread.status = "resolved"
    thread.resolved_at = utc_now()
    from notifications import notify as _notify
    _notify(
        thread.author,
        "feedback.resolved",
        payload={"thread_id": thread.id, "subject": thread.subject},
    )
    log_event(
        "feedback.resolve", actor=current_user,
        entity_type="feedback_thread", entity_id=thread.id,
    )
    db.session.commit()
    flash("Thread resolved.", "success")
    return redirect(url_for("staff.feedback_thread", thread_id=thread.id))
