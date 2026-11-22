"""Route blueprints and shared access-control helpers for Brydl Dashboard.

Each blueprint lives in its own module. URL prefixes are assigned at
registration time in app.py so the entire URL map is visible in one place.
This package also owns the role-based access control decorators applied by
every admin, PM, and annotator route, plus the display-currency helper.
"""

from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import abort, current_app, has_request_context
from flask_login import current_user, login_required

from models import money


def role_required(*roles: str):
    """Build a view decorator restricting access to the given roles.

    Wraps Flask-Login's login_required, so anonymous visitors are
    redirected to the login page while an authenticated user with the
    wrong role receives 403 Forbidden. Use the ready-made
    admin_required / annotator_required / pm_required decorators
    instead of calling this directly.

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


def staff_required(view):
    """Allow any staff role (admin / project_manager / qa / auditor).

    Annotators are excluded. Used by cross-staff surfaces: messages,
    feedback composer, profile page, notifications.

    Args:
        view: The view function to protect.

    Returns:
        The protected view.
    """
    return role_required("admin", "project_manager", "qa", "auditor")(view)


def pm_required(view):
    """Allow admin or project_manager (see role_required).

    Project scoping (is this PM assigned to the project?) is enforced
    per-project inside the view with pm_can_access_project() — this
    decorator only checks that the actor is a PM-class user at all.
    Admins retain full access for fire-fighting.

    Args:
        view: The view function to protect.

    Returns:
        The protected view.
    """
    return role_required("admin", "project_manager")(view)


def qa_required(view):
    """Allow admin or QA: the submission-review surface."""
    return role_required("admin", "qa")(view)


def auditor_required(view):
    """Allow admin or auditor: arbitration + audit-trail surface."""
    return role_required("admin", "auditor")(view)


def annotator_required(view):
    """Allow only role='annotator' (see role_required).

    Also enforces applicant quarantine: if current_user has an active
    applicant_profile that has not graduated to 'trial' or 'active',
    redirect to their onboarding status view.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "annotator":
            abort(403)
        applicant = getattr(current_user, "applicant_profile", None)
        if applicant is not None and applicant.stage not in ("trial", "active"):
            from flask import redirect, url_for
            return redirect(url_for("onboard.status"))
        return view(*args, **kwargs)

    return wrapped


def pm_can_access_project(project) -> bool:
    """Return True if current_user can act on *project*.

    Admins always can. Project managers must have a ProjectAssignment
    row linking them to the project. Other staff see projects via
    admin-granted routes; this helper is for PM-scoped views only.

    Args:
        project: A Project ORM instance (or None).

    Returns:
        True iff the current user may open the PM surface for the project.
    """
    if not current_user.is_authenticated:
        return False
    if current_user.role == "admin":
        return True
    if current_user.role != "project_manager":
        return False
    if project is None:
        return False
    # assignments is a lazy backref; iterate once.
    for a in project.assignments:
        if a.user_id == current_user.id:
            return True
    return False


def dashboard_endpoint_for_role(role: str) -> str:
    """Return the blueprint endpoint of a role's landing dashboard."""
    if role == "admin":
        return "admin.dashboard"
    if role == "project_manager":
        return "pm.dashboard"
    if role == "qa":
        return "admin.review"
    if role == "auditor":
        return "admin.consensus_conflicts"
    return "annotator.dashboard"


def get_setting(key: str, default: str | None = None) -> str | None:
    """Return a SiteSetting value, falling back to *default*.

    DB reads happen once per request via a cached attribute on flask.g
    so a page render that consults 3-4 settings stays one query. When
    called outside a request context (tests / CLI seed) this falls back
    to the default silently so the value can be set from the env.

    Args:
        key: The settings key (e.g. 'usd_to_ngn_rate').
        default: Value to return when no row exists / no app context.

    Returns:
        The stored text value, or default.
    """
    if not has_request_context():
        return default
    from flask import g

    if not hasattr(g, "brydl_settings"):
        try:
            from models import SiteSetting
            from extensions import db

            g.brydl_settings = {
                row.key: row.value for row in db.session.query(SiteSetting).all()
            }
        except Exception:
            g.brydl_settings = {}
    value = g.brydl_settings.get(key)
    return value if value is not None else default


def get_setting_decimal(key: str, default: Decimal) -> Decimal:
    """Return a SiteSetting cast to Decimal, with fallback."""
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return default


def get_setting_int(key: str, default: int) -> int:
    """Return a SiteSetting cast to int, with fallback."""
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def usd_to_ngn(amount) -> Decimal:
    """Convert a USD amount to Naira for display at the configured rate.

    The rate is read first from the site_settings table (admin-editable
    at /admin/settings) and falls back to the Config.USD_TO_NGN_RATE env
    default so a fresh deploy without settings rows still works.

    Args:
        amount: The USD amount (Decimal preferred; anything numeric).

    Returns:
        The Naira equivalent as a Decimal quantized to 2 places.
    """
    rate = None
    if has_request_context():
        rate = get_setting("usd_to_ngn_rate")
    if rate is None:
        # Fall back to the env-provided Decimal constant.
        rate = current_app.config.get("USD_TO_NGN_RATE") or Decimal("1600")
    try:
        rate_dec = Decimal(str(rate))
    except (InvalidOperation, ValueError, TypeError):
        rate_dec = Decimal("1600")
    return money(Decimal(str(amount)) * rate_dec)
