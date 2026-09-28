"""Route blueprints and shared access-control helpers for Brydl Dashboard.

Each blueprint lives in its own module. URL prefixes are assigned at
registration time in app.py so the entire URL map is visible in one place.
This package also owns the role-based access control decorators applied by
every admin and annotator route, plus the display-currency helper.
"""

from decimal import Decimal
from functools import wraps

from flask import abort, current_app
from flask_login import current_user, login_required

from models import money


def role_required(*roles: str):
    """Build a view decorator restricting access to the given roles.

    Wraps Flask-Login's login_required, so anonymous visitors are
    redirected to the login page while an authenticated user with the
    wrong role receives 403 Forbidden. Use the ready-made
    admin_required / annotator_required decorators instead of calling
    this directly.

    Args:
        *roles: Role names allowed to call the view (e.g. "admin").

    Returns:
        The decorating function.
    """

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                # Wrong role, not failed authentication: forbid outright
                # rather than revealing which dashboards exist.
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    """Allow only role='admin' (see role_required).

    Args:
        view: The view function to protect.

    Returns:
        The protected view.
    """
    return role_required("admin")(view)


def qa_required(view):
    """Allow admin or QA: the submission-review surface.

    QA reviewers work the review queue and quality stats; admins keep
    full access (see role_required).

    Args:
        view: The view function to protect.

    Returns:
        The protected view.
    """
    return role_required("admin", "qa")(view)


def auditor_required(view):
    """Allow admin or auditor: arbitration + audit-trail surface.

    Auditors rule on blind-consensus conflicts and read the audit
    trail; admins keep full access (see role_required).

    Args:
        view: The view function to protect.

    Returns:
        The protected view.
    """
    return role_required("admin", "auditor")(view)


def annotator_required(view):
    """Allow only role='annotator' (see role_required).

    Args:
        view: The view function to protect.

    Returns:
        The protected view.
    """
    return role_required("annotator")(view)


def dashboard_endpoint_for_role(role: str) -> str:
    """Return the blueprint endpoint of a role's landing dashboard.

    Args:
        role: The user's role. Admins land on the management
            dashboard, QA on the review queue, auditors on the
            consensus conflicts queue, annotators on their workspace.

    Returns:
        The endpoint name to pass to url_for() after sign-in.
    """
    if role == "admin":
        return "admin.dashboard"
    if role == "qa":
        return "admin.review"
    if role == "auditor":
        return "admin.consensus_conflicts"
    return "annotator.dashboard"


def usd_to_ngn(amount) -> Decimal:
    """Convert a USD amount to Naira for display at the configured rate.

    V1 uses the fixed config.USD_TO_NGN_RATE Decimal constant. Naira
    values are computed at render time only — the database always
    stores USD. Decimal arithmetic end to end: no float money ever.

    Args:
        amount: The USD amount (Decimal preferred; anything numeric).

    Returns:
        The Naira equivalent as a Decimal quantized to 2 places.
    """
    rate = current_app.config.get("USD_TO_NGN_RATE") or Decimal("1600")
    return money(Decimal(str(amount)) * Decimal(str(rate)))
