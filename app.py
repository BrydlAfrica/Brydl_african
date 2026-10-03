"""Application factory for Brydl Dashboard.

Creates and wires the Flask application: configuration, database,
Flask-Login, the three blueprints, browser security headers, custom error
pages, and template globals. A module-level ``app`` is exposed so gunicorn
can serve the factory directly on Render with ``gunicorn app:app``.
"""

import os
import sys
import time

from flask import Flask, g, render_template, request, session
from flask_login import current_user
from flask_wtf.csrf import generate_csrf
from werkzeug.middleware.proxy_fix import ProxyFix

from audit import (
    get_request_id,
    new_request_id,
    reset_request_id,
    setup_logging,
)
from backup import backup_cli
from config import Config
from seed_admin import seed_admin_command
from extensions import db, migrate, login_manager
from models import User
from routes.admin import admin_bp
from routes.annotator import annotator_bp
from routes.public import public_bp
from routes.api import api_bp
from routes.client_portal import client_bp
from routes.pm import pm_bp
from routes.staff import staff_bp
from client_portal import redact_client_path
from routes import usd_to_ngn
from routes.auth import auth_bp

# Content-Security-Policy for the whole app. Templates load the
# self-hosted Bootstrap 5 from static/vendor/ (no external origins):
# and Bootstrap Icons from cdn.jsdelivr.net (stack constraint: CDN, no
# build step), so that origin is allow-listed for scripts, styles, and
# fonts; everything else is locked to same-origin. Notes:
# - 'unsafe-inline' on style-src is required by Bootstrap's inline style
#   attributes; scripts are NOT allowed inline (XSS mitigation).
# - form-action 'self' is a second CSRF layer at the browser level: even a
#   successful injection cannot post our forms to an attacker's site.
# - Any new third-party asset origin must be allow-listed here.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def create_app(config_class: type = Config) -> Flask:
    """Build and configure the Flask application (app factory pattern).

    Args:
        config_class: Configuration object to load. Defaults to Config;
            tests can inject a subclass (e.g. to point at an in-memory
            database).

    Returns:
        A fully initialised Flask application instance.
    """
    app = Flask(__name__)
    app.config.from_object(config_class)
    setup_logging(app)

    # Render terminates TLS at its edge proxy, so the app itself sees
    # plain http. ProxyFix restores the real scheme/client from Render's
    # X-Forwarded-* headers, keeping secure-cookie and scheme logic
    # correct. Only enabled on Render (RENDER env var) — never trust
    # these headers from a client on a direct connection.
    if os.environ.get("RENDER"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    # --- Extensions -----------------------------------------------------
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"

    # Template global for CSRF-protected forms that are not FlaskForm
    # instances (the navbar logout form). flask-wtf only registers this
    # global when CSRFProtect is initialised; we validate per-form, so
    # register it explicitly.
    app.jinja_env.globals["csrf_token"] = generate_csrf

    # --- Blueprints -------------------------------------------------------
    app.register_blueprint(auth_bp)  # /login, /logout at the root
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(annotator_bp, url_prefix="/annotator")
    app.register_blueprint(pm_bp)        # /pm/ — Project Manager workspace
    app.register_blueprint(staff_bp)     # /profile, /feedback, /notifications (shared)
    app.register_blueprint(client_bp)    # /client/<token> portal
    app.register_blueprint(public_bp)    # / landing page
    app.register_blueprint(api_bp)       # /api/v1 machine API (Bearer)

    # Ops CLI: `flask --app app backup create|list|verify|restore`.
    app.cli.add_command(backup_cli)
    # First-admin bootstrap: `flask --app app seed-admin`.
    app.cli.add_command(seed_admin_command)

    # Phase 10: background job runner (in-process thread; the
    # battery sets BRYDL_DISABLE_JOBS=1, a dedicated worker service
    # would run worker.py with JOBS_ENABLED=0 on the web dyno).
    if os.environ.get("BRYDL_DISABLE_JOBS", "") != "1":
        import jobs as jobs_engine

        jobs_engine.start_runner(app)

    # --- Request correlation + access log ---------------------------------
    # Every request gets a 12-hex correlation id shared by the access
    # line, all AUDIT lines and any traceback it produced, and echoed
    # back as X-Request-ID so a user-reported error maps to exactly one
    # log slice. See audit.py.
    @app.before_request
    def _start_request():
        """Stamp the correlation id and the request start time."""
        g.request_started_at = time.perf_counter()
        g.request_id_token = new_request_id()

    @app.after_request
    def _finish_request(response):
        """Echo the correlation id and write the REQ access-log line."""
        response.headers["X-Request-ID"] = get_request_id()
        if not request.path.startswith("/static"):
            started = getattr(g, "request_started_at", None)
            duration_ms = (
                (time.perf_counter() - started) * 1000 if started else -1
            )
            user = (
                current_user.id if current_user.is_authenticated else "-"
            )
            # The client-portal token IS the credential: never let a
            # raw link land in the access log.
            logged_path = redact_client_path(request.path)
            app.logger.info(
                "REQ %s %s -> %s (%.0f ms) ip=%s user=%s",
                request.method,
                logged_path,
                response.status_code,
                duration_ms,
                request.remote_addr or "-",
                user,
            )
        return response

    @app.teardown_request
    def _teardown_request(exc):
        """Restore the request-id context (thread reuse safety)."""
        token = getattr(g, "request_id_token", None)
        if token is not None and not getattr(g, "_request_id_reset", False):
            try:
                reset_request_id(token)
            except (RuntimeError, ValueError):
                # Token may have already been reset in test-client thread
                # reuse; swallow to avoid leaking noise into logs.
                pass
            g._request_id_reset = True

    # --- Flask-Login session loading ---------------------------------------
    @login_manager.user_loader
    def load_user(user_id: str):
        """Load a user by primary key for Flask-Login.

        SECURITY: inactive users are rejected here, not just at login
        time. Flask-Login only checks is_active inside login_user(), never
        when reloading a session or remember-me cookie — without this
        check, a deactivated annotator would keep full access for up to
        REMEMBER_COOKIE_DURATION (7 days). This loader is the single choke
        point for both the session and cookie paths.

        Args:
            user_id: The "<primary_key>.<auth_version>" string stored in
                the session (legacy cookies carry the bare id).

        Returns:
            The active User object, or None (which Flask-Login treats as
            an anonymous session) for malformed ids, unknown users, or
            deactivated users.
        """
        ident, _, version = user_id.partition(".")
        try:
            user = db.session.get(User, int(ident))
        except (TypeError, ValueError):
            # Malformed session id: fall back to anonymous, not a 500.
            return None
        except Exception:
            app.logger.exception("user_loader database lookup failed.")
            return None
        if user is None or not user.is_active:
            # Purge the stale identity so the session cannot be
            # resurrected later (e.g. if the user is re-activated, or the
            # id lingers after the account row is deleted).
            session.pop("_user_id", None)
            session.pop("_fresh", None)
            session.pop("_remember", None)
            return None
        # Session ids embed auth_version ("id.version"; legacy cookies
        # carry no suffix = version 0). A mismatch means the password
        # changed since this session/cookie was issued — revoke it.
        try:
            issued_version = int(version) if version else 0
        except ValueError:
            return None
        if issued_version != user.auth_version:
            session.pop("_user_id", None)
            session.pop("_fresh", None)
            session.pop("_remember", None)
            return None
        return user

    # --- Browser security headers ------------------------------------------
    @app.after_request
    def set_security_headers(response):
        """Attach baseline security headers to every response.

        Args:
            response: The outgoing Flask response object.

        Returns:
            The same response with security headers applied.
        """
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = (
            "strict-origin-when-cross-origin"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            CONTENT_SECURITY_POLICY
        )
        # HSTS only makes sense once TLS actually terminates in front of
        # us (i.e. on Render); locally it would do nothing.
        if os.environ.get("RENDER"):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # --- Error pages --------------------------------------------------------
    # Custom handlers so users never see the framework's bare grey error
    # pages. Every template extends base.html, so error pages keep the
    # full app shell (navbar, footer, flash area). The helper degrades to
    # a minimal inline page if the templates themselves cannot render
    # (e.g. the database or session store is what broke) — a failing
    # error page must never mask the original status code.
    def _render_error_page(template_name: str, status: int):
        """Render a custom error page with a safe minimal fallback.

        Args:
            template_name: Template path relative to templates/.
            status: HTTP status code for the response.

        Returns:
            The error response with the original status code preserved.
        """
        try:
            return render_template(template_name), status
        except Exception:
            app.logger.exception(
                "Error page %s failed to render.", template_name
            )
            page = (
                '<!doctype html><html lang="en"><head>'
                '<meta charset="utf-8">'
                f"<title>Error {status} · Brydl Dashboard</title></head>"
                '<body style="font-family: system-ui; padding: 2rem;">'
                f"<h1>Error {status}</h1>"
                "<p>Something went wrong while displaying this page.</p>"
                '<p><a href="/">Go to the homepage</a></p></body></html>'
            )
            return page, status

    @app.errorhandler(403)
    def forbidden(error):
        """Render the 'Access denied' page for role-restricted areas.

        Args:
            error: The raised HTTPException (unused).

        Returns:
            The 403 response.
        """
        return _render_error_page("errors/403.html", 403)

    @app.errorhandler(405)
    def method_not_allowed(error):
        """Render the branded page for wrong-method requests.

        Args:
            error: The raised HTTPException (unused).

        Returns:
            The 405 response.
        """
        return _render_error_page("errors/405.html", 405)

    @app.errorhandler(404)
    def page_not_found(error):
        """Render the 'Page not found' page for unknown URLs.

        Args:
            error: The raised HTTPException (unused).

        Returns:
            The 404 response.
        """
        return _render_error_page("errors/404.html", 404)

    @app.errorhandler(500)
    def internal_server_error(error):
        """Render the 'Something went wrong' page for unhandled errors.

        Resets the database session first: after a failed request the
        session can hold a broken transaction that would poison every
        later query in the same worker process. The exception itself is
        logged by Flask before this handler runs; the response never
        leaks a traceback to the client.

        Args:
            error: The unhandled exception (unused).

        Returns:
            The 500 response.
        """
        db.session.rollback()
        return _render_error_page("errors/500.html", 500)

    # --- Database schema ----------------------------------------------------
    # Two schema paths, one source of truth (models.py):
    # - Local dev / tests: create_all() builds the schema directly —
    #   zero-setup, idempotent, exactly what the suites rely on.
    # - Production (Render): the schema comes EXCLUSIVELY from
    #   `flask db upgrade` (migrations/, run in the build command);
    #   render.yaml sets BRYDL_AUTO_SCHEMA=0 to switch this off.
    # tests/test_migrations.py guards parity: the migrated schema and
    # the create_all schema must stay byte-identical.
    if app.config.get("SCHEMA_AUTO_CREATE", True):
        with app.app_context():
            try:
                db.create_all()
            except Exception:
                # Fail fast: a broken database at boot must be visible in
                # the Render logs, not silently degrade requests to 500s.
                app.logger.exception(
                    "Could not create database tables."
                )
                raise

    # Flask-Migrate: `flask --app app db init|migrate|upgrade|check`.
    migrate.init_app(app, db)

    # --- Schema drift guard -------------------------------------------------
    # create_all() creates MISSING tables but never alters existing ones,
    # so a database from an older build boots fine and then 500s on every
    # user query ("no such column"). Detect that at boot and fail with the
    # exact remediation instead of cryptic per-page errors.
    with app.app_context():
        missing = []
        inspector = db.inspect(db.engine)
        existing = set(inspector.get_table_names())
        for table in db.metadata.sorted_tables:
            if table.name not in existing:
                continue  # fresh table; create_all just made it
            present = {
                col["name"] for col in inspector.get_columns(table.name)
            }
            for column in table.columns:
                if column.name not in present:
                    missing.append(f"{table.name}.{column.name}")
        # Never fire inside `flask db …` commands: upgrading IS the
        # remediation, and autogenerate must be able to run against a
        # stale database to produce the fixing revision.
        if missing and "db" not in sys.argv:
            raise RuntimeError(
                "DATABASE SCHEMA OUT OF DATE — this database predates the"
                " current models. Missing columns: "
                + ", ".join(missing)
                + ". LOCAL DEV (data is throwaway): stop the server,"
                " delete the instance/ folder, run `python seed.py`."
                " PRODUCTION: databases on Render are created by"
                " migrations in the build command — reaching this state"
                " means `flask --app app db upgrade` did not run; check"
                " the Render build logs. (Pre-migration databases cannot"
                " be `db upgrade`d in place: revision 1 recreates the"
                " tables.)"
            )

    # --- Template globals ---------------------------------------------------
    @app.context_processor
    def inject_globals() -> dict:
        """Expose shared variables to every Jinja2 template."""
        from models import Notification, SiteSetting
        from sqlalchemy import func, select

        unread = 0
        site_name = "Brydl Dashboard"
        if current_user.is_authenticated:
            try:
                unread = db.session.scalar(
                    select(func.count(Notification.id)).where(
                        Notification.user_id == current_user.id,
                        Notification.read_at.is_(None),
                    )
                ) or 0
                try:
                    s = db.session.get(SiteSetting, "site_name")
                    if s and s.value:
                        site_name = s.value
                except Exception:
                    pass
            except Exception:
                # Context processor must never crash the page.
                unread = 0
        # Curry conversion so macros can call _ngn_convert(amount) without
        # depending on the caller importing usd_to_ngn directly.
        return {
            "current_user": current_user,
            "app_name": site_name,
            "usd_to_ngn": usd_to_ngn,
            "unread_notifications": unread,
        }

    # --- Optional error tracking (Sentry) ----------------------------------
    # Set SENTRY_DSN in the environment to enable. No SDK import when the
    # DSN is absent so local/offline installs never hit the network. The
    # Flask integration registers its own before-request/error handlers;
    # send_default_pii=False keeps user emails/IDs off the wire unless you
    # explicitly opt in from the Sentry dashboard later.
    _sentry_dsn = os.environ.get("SENTRY_DSN", "").strip()
    if _sentry_dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration

            sentry_sdk.init(
                dsn=_sentry_dsn,
                integrations=[FlaskIntegration()],
                traces_sample_rate=float(
                    os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")
                ),
                profiles_sample_rate=float(
                    os.environ.get("SENTRY_PROFILES_SAMPLE_RATE", "0")
                ),
                send_default_pii=False,
                environment=os.environ.get(
                    "SENTRY_ENVIRONMENT",
                    "production" if app.config.get("FLASK_ENV") == "production"
                    else "development",
                ),
                release=os.environ.get("RELEASE_SHA", "dev"),
            )
            app.logger.info("Sentry error tracking enabled.")
        except ImportError:
            app.logger.warning(
                "SENTRY_DSN is set but sentry-sdk is not installed; "
                "run `pip install sentry-sdk[flask]` to enable."
            )
        except Exception:
            app.logger.exception("Failed to initialise Sentry.")

    return app


# Module-level instance so gunicorn can import it: ``gunicorn app:app``.
app = create_app()


if __name__ == "__main__":
    # Development server only. debug=True exposes an interactive debugger
    # that can execute arbitrary code, so it must never face the public
    # internet; it binds to loopback by default and Render uses gunicorn.
    app.run(debug=True, port=5000)
