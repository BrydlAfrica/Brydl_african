"""Authentication blueprint: sign-in and sign-out.

Security decisions implemented here:
- Generic "invalid email or password" error: the form never reveals
  whether an email exists.
- A dummy hash comparison runs when the email is unknown, so response
  timing cannot be used to enumerate accounts.
- Repeated failures from one email+IP pair trigger an escalating lockout.
  The tracker is in-memory, which is correct for Render free tier's
  single gunicorn worker; move it to the database or Redis when workers
  scale out.
- ``?next=`` targets must be root-relative paths on this site, blocking
  open redirects (Flask 3.1 no longer ships a helper for this, so the
  check is implemented explicitly in _is_safe_redirect()).
- Logout is POST-only with CSRF validation, so link previews and
  cross-site forms cannot sign users out.
- Emails are lowercased before lookup; users.email is documented to be
  stored lowercase everywhere (SQLite uniqueness is case-sensitive).
"""

import hashlib
import secrets
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from flask_wtf.csrf import ValidationError, validate_csrf
from sqlalchemy import select
from wtforms import (
    BooleanField,
    HiddenField,
    PasswordField,
    StringField,
    SubmitField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
)
from werkzeug.security import (
    check_password_hash,
    generate_password_hash,
)

from audit import log_event
from config import (
    FORGOT_MAX_PER_WINDOW,
    FORGOT_WINDOW_SECONDS,
    LOGIN_LOCKOUT_BASE_SECONDS,
    LOGIN_LOCKOUT_MAX_SECONDS,
    LOGIN_MAX_FAILURES,
    RESET_TOKEN_MAX_AGE_SECONDS,
)
from extensions import db
from mailer import (
    mail_enabled,
    send_password_reset_email,
)
from models import User, utc_now
from routes import dashboard_endpoint_for_role

auth_bp = Blueprint("auth", __name__)

# Hash of an unguessable dummy password: compared against whenever the
# submitted email is unknown, so a missing account costs the same scrypt
# work as a wrong password (timing-side-channel defence).
DUMMY_PASSWORD_HASH = generate_password_hash(
    "brydl-timing-equaliser-9f27c1", method="scrypt"
)

# In-memory login-failure tracking: {"email|ip": (count, locked_until,
# updated_at)}. Process-local by design for a single gunicorn worker;
# revisit when workers scale out.
_login_failures: dict[str, tuple[int, datetime | None, datetime]] = {}
_failures_lock = threading.Lock()
# Throttle tuning lives in config.py (env-tunable).
MAX_FAILURES = LOGIN_MAX_FAILURES
BASE_LOCKOUT = timedelta(seconds=LOGIN_LOCKOUT_BASE_SECONDS)
MAX_LOCKOUT = timedelta(seconds=LOGIN_LOCKOUT_MAX_SECONDS)
FAILURE_TTL = timedelta(hours=1)


class LoginForm(FlaskForm):
    """Server-side validated sign-in form (CSRF via hidden_tag)."""

    # The filter strips/lowercases at form-processing time — BEFORE the
    # validators run — so "  Ada@Brydl.NG " passes Email() and matches
    # the stored lowercase email. The view normalizes again as defence
    # in depth.
    email = StringField(
        "Email address",
        filters=[lambda value: value.strip().lower() if value else value],
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email is too long."),
        ],
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(max=128, message="Password is too long."),
        ],
    )
    # Valuable for annotators on intermittent connections: one sign-in
    # survives drops and restarts for REMEMBER_COOKIE_DURATION (7 days).
    remember_me = BooleanField("Keep me signed in for 7 days")
    next = HiddenField()
    submit = SubmitField("Sign in")


def _is_safe_redirect(target: str | None) -> bool:
    """Return True only for root-relative paths on this site.

    Blocks absolute URLs (``https://evil.com``), protocol-relative URLs
    (``//evil.com``), custom schemes (``javascript:...``) and backslash
    tricks. Flask 3.1 / Werkzeug 3.1 no longer ship the old
    url_has_allowed_host_and_scheme helper, so the check is explicit.

    Args:
        target: The candidate redirect target.

    Returns:
        True if the target is safe to redirect to.
    """
    if not target or not target.startswith("/"):
        return False
    parts = urlparse(target)
    return not parts.scheme and not parts.netloc


def _lockout_remaining(key: str, now: datetime) -> timedelta | None:
    """Return the remaining lockout for a key, or None if not locked.

    Args:
        key: The "email|ip" composite key.
        now: Current UTC time.

    Returns:
        Remaining lockout duration, or None when not locked out.
    """
    with _failures_lock:
        entry = _login_failures.get(key)
        if entry and entry[1] and entry[1] > now:
            return entry[1] - now
        return None


def _register_failure(key: str, now: datetime) -> timedelta | None:
    """Record a failed sign-in; start an escalating lockout at the cap.

    Args:
        key: The "email|ip" composite key.
        now: Current UTC time.

    Returns:
        The just-started lockout duration, or None if under the cap.
    """
    with _failures_lock:
        _prune_failures(now)
        count = _login_failures.get(key, (0, None, now))[0] + 1
        if count < MAX_FAILURES:
            _login_failures[key] = (count, None, now)
            return None
        # Exponential backoff: 2, 4, 8 ... minutes, capped at 15.
        delay = min(MAX_LOCKOUT, BASE_LOCKOUT * (2 ** (count - MAX_FAILURES)))
        _login_failures[key] = (count, now + delay, now)
        return delay


def _clear_failures(key: str) -> None:
    """Reset failure tracking for a key after a successful sign-in.

    Args:
        key: The "email|ip" composite key.
    """
    with _failures_lock:
        _login_failures.pop(key, None)


def _prune_failures(now: datetime) -> None:
    """Drop stale tracker entries (caller must hold _failures_lock).

    Args:
        now: Current UTC time.
    """
    if len(_login_failures) < 256:
        return
    cutoff = now - FAILURE_TTL
    stale = [
        key
        for key, (_, _, updated) in _login_failures.items()
        if updated < cutoff
    ]
    for key in stale:
        _login_failures.pop(key, None)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Sign in and route the user to their role's dashboard.

    GET renders the form (redirecting first if already signed in). POST
    validates credentials behind a lockout, answers with generic errors,
    and honours only safe ``?next=`` targets.

    Returns:
        The rendered login page or a redirect response.
    """
    if current_user.is_authenticated:
        return redirect(
            url_for(dashboard_endpoint_for_role(current_user.role))
        )

    form = LoginForm(next=request.args.get("next", ""))

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        password = form.password.data
        # Bind the lockout to email + source IP so a third party cannot
        # lock a victim's account out from an unfamiliar address alone.
        key = f"{email}|{request.remote_addr or 'unknown'}"
        now = datetime.now(timezone.utc)

        remaining = _lockout_remaining(key, now)
        if remaining:
            log_event(
                "auth.login.locked_out",
                actor_name=email,
                detail={
                    "minutes_left": round(
                        remaining.total_seconds() / 60, 1
                    )
                },
                commit=True,
            )
            minutes = max(1, int(remaining.total_seconds() // 60) + 1)
            flash(
                "Too many failed sign-in attempts. Please try again in "
                f"{minutes} minute(s).",
                "warning",
            )
            return render_template("login.html", form=form), 429

        try:
            user = db.session.scalar(select(User).where(User.email == email))
        except Exception:
            current_app.logger.exception("Sign-in lookup failed.")
            flash(
                "Something went wrong on our side. Please try again.",
                "danger",
            )
            return render_template("login.html", form=form), 500

        # Always run exactly one hash comparison - against a dummy hash
        # when the email is unknown - so timing cannot reveal which
        # emails have accounts.
        password_ok = check_password_hash(
            user.password_hash if user else DUMMY_PASSWORD_HASH, password
        )

        if user is None or not password_ok or not user.is_active:
            # A deactivated account gets the same message as a wrong
            # password: no signal that the email exists at all.
            if user is None:
                reason = "unknown_email"
            elif not password_ok:
                reason = "bad_password"
            else:
                reason = "inactive_account"
            lockout = _register_failure(key, now)
            if lockout:
                minutes = max(1, int(lockout.total_seconds() // 60) + 1)
                log_event(
                    "auth.login.failed",
                    actor_name=email,
                    detail={
                        "reason": reason,
                        "lockout_minutes": minutes,
                    },
                    commit=True,
                )
                flash(
                    "Too many failed sign-in attempts. Please try again "
                    f"in {minutes} minute(s).",
                    "warning",
                )
                return render_template("login.html", form=form), 429
            log_event(
                "auth.login.failed",
                actor_name=email,
                detail={"reason": reason},
                commit=True,
            )
            flash("Invalid email or password.", "danger")
            return render_template("login.html", form=form)

        _clear_failures(key)
        # Rotate session content before attaching the new identity so no
        # pre-login data carries into the authenticated session
        # (session-fixation defence in depth).
        session.clear()
        login_user(user, remember=form.remember_me.data)
        log_event(
            "auth.login.success",
            actor=user,
            detail={"remember": bool(form.remember_me.data)},
            commit=True,
        )
        flash(f"Welcome back, {user.name}!", "success")

        # The Terms gate comes FIRST: an annotator who has not accepted
        # the current terms lands on the terms page before anything else
        # in the app (the annotator blueprint re-enforces this on every
        # request; this makes it the visible first screen after login).
        if (
            user.role == "annotator"
            and user.terms_version
            != current_app.config.get("TERMS_VERSION")
        ):
            flash(
                "Welcome! Please review and accept the Terms &"
                " Conditions to continue.",
                "info",
            )
            return redirect(url_for("annotator.terms"))
        if _is_safe_redirect(form.next.data):
            return redirect(form.next.data)
        return redirect(url_for(dashboard_endpoint_for_role(user.role)))

    return render_template("login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """Sign the user out (POST + CSRF only, so links can't log you out).

    Returns:
        A redirect to the login page.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        log_event(
            "auth.logout.csrf_rejected",
            actor=current_user,
            commit=True,
        )
        flash("Security check failed. Please try again.", "warning")
        return redirect(
            url_for(dashboard_endpoint_for_role(current_user.role))
        )
    log_event("auth.logout", actor=current_user, commit=True)
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/account/theme", methods=["POST"])
@login_required
def set_theme():
    """Persist the signed-in user's colour theme (POST + CSRF only).

    Self-service preference available to both roles; accepted values
    are the Bootstrap themes the UI supports ("light"/"dark"). Audited
    as ``user.theme``.

    Returns:
        A redirect back to the caller's dashboard with feedback.
    """
    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for(dashboard_endpoint_for_role(
            current_user.role)))
    theme = request.form.get("theme", "").strip().lower()
    if theme not in ("light", "dark"):
        flash("Unknown theme.", "warning")
        return redirect(url_for(dashboard_endpoint_for_role(
            current_user.role)))
    current_user.theme = theme
    log_event(
        "user.theme",
        actor=current_user,
        entity_type="user",
        entity_id=current_user.id,
        detail={"theme": theme},
        commit=True,
    )
    flash(f"Theme switched to {theme}.", "success")
    return redirect(url_for(dashboard_endpoint_for_role(
        current_user.role)))


# ---------------------------------------------------------------------------
# Self-service password reset (email one-off token)
# ---------------------------------------------------------------------------

# Per-email request throttle: {lower-cased email: deque[utcnow]}. In
# memory, per worker — enough against email bombing on a single-host
# deploy, and it holds no state an attacker could read.
_forgot_lock = threading.Lock()
_forgot_requests: dict[str, deque] = {}


def _hash_reset_token(token: str) -> str:
    """Return the SHA-256 hex digest of a reset token.

    Only the hash is stored, so a database leak cannot be replayed as
    a working reset link (the same one-way discipline as the password
    hash itself).

    Args:
        token: The raw one-off token from the reset link.

    Returns:
        Lower-case hex digest.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _forgot_allowed(email: str, now: datetime) -> bool:
    """Return True when this email may request another reset link.

    Args:
        email: Lower-cased email address.
        now: Current UTC time.

    Returns:
        False once FORGOT_MAX_PER_WINDOW requests were made within
        FORGOT_WINDOW_SECONDS (anti email-bombing throttle).
    """
    with _forgot_lock:
        bucket = _forgot_requests.get(email)
        if bucket is not None:
            horizon = now - timedelta(seconds=FORGOT_WINDOW_SECONDS)
            while bucket and bucket[0] < horizon:
                bucket.popleft()
            if len(bucket) >= FORGOT_MAX_PER_WINDOW:
                return False
        return True


def _forgot_record(email: str, now: datetime) -> None:
    """Record a reset request for the throttle bucket (see above)."""
    with _forgot_lock:
        _forgot_requests.setdefault(email, deque()).append(now)


class ForgotPasswordForm(FlaskForm):
    """Ask-for-a-reset-link form (email only)."""

    email = StringField(
        "Email address",
        filters=[lambda value: value.strip().lower() if value else value],
        validators=[
            DataRequired(message="Email is required."),
            Email(message="Enter a valid email address."),
            Length(max=255, message="Email is too long."),
        ],
    )
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    """Choose-a-new-password form (reuses the profile change rules)."""

    password = PasswordField(
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
            EqualTo(
                "password", message="The two passwords do not match."
            ),
        ],
    )
    submit = SubmitField("Reset password")


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Request a password-reset email.

    The response is deliberately identical whether or not the email
    exists (and whether it is active): no account enumeration. Real
    requests are throttled per email, the one-off token is stored only
    as a SHA-256 hash with a UTC expiry, and delivery goes through
    mailer.py — or the application log in dev fallback.

    Returns:
        The rendered form, or a redirect to the login page carrying
        the (always identical) sent-if-it-exists flash.
    """
    if current_user.is_authenticated:
        return redirect(url_for(dashboard_endpoint_for_role(
            current_user.role)))
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        # models.utc_now(): NAIVE UTC, matching how DateTime columns are
        # stored (aware-vs-naive comparisons would raise TypeError at
        # validation time).
        now = utc_now()
        if not _forgot_allowed(email, now):
            log_event(
                "auth.password_reset.throttled",
                actor_name=email,
                detail={"window_seconds": FORGOT_WINDOW_SECONDS},
                commit=True,
            )
        else:
            user = db.session.scalar(
                select(User).where(User.email == email)
            )
            # Inactive accounts get nothing: a deactivated person has
            # no self-service path back in (an admin reactivates them).
            if user is not None and user.is_active:
                _forgot_record(email, now)
                raw_token = secrets.token_urlsafe(32)
                user.reset_token_hash = _hash_reset_token(raw_token)
                user.reset_token_expires = (
                    now + timedelta(seconds=RESET_TOKEN_MAX_AGE_SECONDS)
                )
                db.session.commit()
                reset_url = url_for(
                    "auth.reset_token",
                    token=raw_token,
                    _external=True,
                )
                sent = send_password_reset_email(email, reset_url)
                log_event(
                    "auth.password_reset.requested",
                    actor_name=email,
                    detail={
                        "user_id": user.id,
                        "delivery": "smtp" if mail_enabled()
                        else "dev_log",
                        "sent": sent,
                    },
                    commit=True,
                )
        flash(
            "If that email belongs to a Brydl account, a reset link is"
            " on its way. Check your spam folder if it doesn't arrive"
            " within a few minutes.",
            "info",
        )
        return redirect(url_for("auth.login"))
    return render_template("forgot_password.html", form=form)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_token(token: str):
    """Consume a one-off reset token and set a new password.

    The token in the URL is hashed and matched against the stored
    hash; expired or already-used links are refused the same way.
    A successful reset clears the token columns (single use) and is
    audited. The person is NOT auto-signed-in: they return to the
    login page with their new password.

    Args:
        token: The raw token from the emailed link.

    Returns:
        The reset form, a redirect to it after success, or a redirect
        back to the forgot page for any invalid/expired token.
    """
    if current_user.is_authenticated:
        return redirect(url_for(dashboard_endpoint_for_role(
            current_user.role)))
    token_hash = _hash_reset_token(token)
    now = utc_now()  # naive UTC, same as stored expiries
    user = db.session.scalar(
        select(User).where(User.reset_token_hash == token_hash)
    )
    if (
        user is None
        or user.reset_token_expires is None
        or user.reset_token_expires <= now
    ):
        log_event(
            "auth.password_reset.invalid_link",
            detail={"reason": "unknown_or_expired"},
            commit=True,
        )
        flash(
            "This reset link is invalid or has expired. Please request"
            " a fresh one.",
            "warning",
        )
        return redirect(url_for("auth.forgot_password"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        # Single use: even the hash must not match again. Also kill
        # every existing session/cookie for the account.
        user.reset_token_hash = None
        user.reset_token_expires = None
        user.auth_version += 1
        log_event(
            "auth.password_reset",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            detail={"via": "email_token"},
            commit=True,
        )
        flash(
            "Password updated. Sign in with your new password.",
            "success",
        )
        return redirect(url_for("auth.login"))
    return render_template(
        "reset_password.html", form=form, token=token
    )


class ActivateAccountForm(FlaskForm):
    """Form for setting password upon official workforce account verification."""

    password = PasswordField(
        "New Password",
        validators=[
            DataRequired(message="Password is required."),
            Length(min=8, max=128, message="Password must be at least 8 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(message="Please confirm your password."),
            EqualTo("password", message="Passwords must match."),
        ],
    )
    submit = SubmitField("Activate Official Account")


@auth_bp.route("/activate/<token>", methods=["GET", "POST"])
def activate_account(token: str):
    """Consume a single-use activation token, set password, and activate official account.

    Args:
        token: The raw token from the verification email link.

    Returns:
        The activation form or redirect to official dashboard.
    """
    if current_user.is_authenticated:
        logout_user()

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = utc_now()
    user = db.session.scalar(
        select(User).where(User.activation_token_hash == token_hash)
    )
    if (
        user is None
        or user.activation_token_expires is None
        or user.activation_token_expires <= now
    ):
        flash(
            "This activation link is invalid or has expired. Please contact support or your administrator.",
            "warning",
        )
        return redirect(url_for("auth.login"))

    form = ActivateAccountForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.activation_token_hash = None
        user.activation_token_expires = None
        user.auth_version += 1
        db.session.commit()

        login_user(user)
        log_event(
            "auth.account_activated",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            commit=True,
        )
        flash(
            "Your official Brydl annotator account has been successfully activated! Welcome to the verified workspace.",
            "success",
        )
        return redirect(url_for("annotator.dashboard"))

    return render_template(
        "activate_account.html", form=form, token=token, user=user
    )
