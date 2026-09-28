"""Application configuration for Brydl Dashboard.

A single Config class is loaded by the app factory in app.py. It is
environment-aware:

- SECRET_KEY comes from the environment in production (Render dashboard);
  locally it falls back to a persisted random key so offline development
  and multiple gunicorn workers still share one stable key.
- DATABASE_URL (Render PostgreSQL) overrides SQLite automatically; the
  legacy ``postgres://`` scheme Render exports is rewritten to the
  ``postgresql://`` scheme SQLAlchemy requires.
- Cookie ``Secure`` flags and HSTS activate automatically when the app
  detects it is running on Render (TLS terminated at Render's edge).
"""

import logging
import os
import secrets
import sqlite3
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Project base directory (.../brydl-dashboard). All filesystem paths are
# derived from here so the app behaves the same no matter where it is
# launched from.
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Flask's default instance folder (<project>/instance). The SQLite database
# and the generated SECRET_KEY fallback both live here.
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "brydl.db")
SECRET_KEY_PATH = os.path.join(INSTANCE_DIR, "secret_key")

# Upper bound for any free-text field that holds ONE piece of content
# (project instructions, a single task's input/gold text, one annotator
# answer). Enforced server-side by validators in the blueprints and
# client-side via maxlength attributes. Bulk paste boxes are exempt at
# the box level (they hold many tasks), but every parsed line is still
# checked against this cap.
MAX_TEXTAREA_CHARS = 10_000


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback.

    Args:
        name: The environment variable to read.
        default: Value used when unset or not a valid integer.

    Returns:
        The parsed integer.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s.", name, raw, default)
        return default


def _env_decimal(name: str, default: str) -> Decimal:
    """Read a Decimal environment variable with a safe fallback.

    Args:
        name: The environment variable to read.
        default: Decimal-literal string used when unset or invalid.

    Returns:
        The parsed Decimal.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return Decimal(default)
    try:
        return Decimal(raw)
    except InvalidOperation:
        logger.warning("Invalid %s=%r; using default %s.", name, raw, default)
        return Decimal(default)


def _load_database_uri() -> str:
    """Return the SQLAlchemy database URI for the current environment.

    Priority order:
    1. The DATABASE_URL environment variable (how Render injects its
       PostgreSQL credentials). Render's legacy ``postgres://`` scheme is
       rewritten to SQLAlchemy's required ``postgresql://``. NOTE: a
       PostgreSQL deployment also requires adding ``psycopg2-binary`` to
       requirements.txt.
    2. SQLite in the instance folder (default: local / offline dev).
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    return "sqlite:///" + DB_PATH.replace(os.sep, "/")


def _load_secret_key() -> str:
    """Return the Flask SECRET_KEY using a secure fallback chain.

    Priority order:
    1. The SECRET_KEY environment variable (must be set in production,
       e.g. in the Render dashboard). Refuses to boot on a suspiciously
       short key: a brute-forceable session key lets attackers forge
       login sessions, so a weak value is treated as misconfiguration.
    2. A 64-hex-character key persisted in instance/secret_key, created
       on first run with owner-only permissions (0600). Persisting the
       key keeps sessions and CSRF tokens valid across restarts and
       across multiple gunicorn workers.
    3. An in-memory random key as a last resort if the filesystem is
       not writable (sessions will reset on every restart).

    Returns:
        The secret key string.

    Raises:
        ValueError: If SECRET_KEY is set in the environment but shorter
            than 32 characters.
    """
    env_key = os.environ.get("SECRET_KEY", "").strip()
    if env_key:
        if len(env_key) < 32:
            raise ValueError(
                "SECRET_KEY is set but too short (< 32 chars); a weak"
                " key allows attackers to forge session cookies."
                " Generate a strong one with:"
                ' python -c "import secrets;'
                ' print(secrets.token_hex(32))"'
            )
        return env_key

    try:
        if os.path.isfile(SECRET_KEY_PATH):
            with open(SECRET_KEY_PATH, "r", encoding="utf-8") as fh:
                persisted = fh.read().strip()
            if len(persisted) >= 32:
                return persisted

        # First run (or corrupt key file): generate and persist a key.
        logger.warning(
            "SECRET_KEY env var not set; generated a fallback key at"
            " %s. Set SECRET_KEY in the environment for production.",
            SECRET_KEY_PATH,
        )
        key = secrets.token_hex(32)
        with open(SECRET_KEY_PATH, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.chmod(SECRET_KEY_PATH, 0o600)
        return key
    except OSError:
        # Unwritable filesystem (rare): per-process random key.
        logger.warning(
            "Could not persist SECRET_KEY fallback; using a per-process"
            " random key. Sessions and CSRF tokens will reset on restart."
        )
        return secrets.token_hex(32)


# --- Tunables (module level: importable, env-overridable) --------------
# Page size for EVERY list view (tasks, review queue, history, payments
# history, audit trail, projects).
PAGINATION_PER_PAGE = _env_int("PAGINATION_PER_PAGE", 20)
# Max tasks uploaded/assigned/reviewed in one request.
UPLOAD_BATCH_LIMIT = _env_int("UPLOAD_BATCH_LIMIT", 500)
# CSV file import (from the admin's device): row and byte caps. A file
# is parsed in memory only — never written to disk — and every row is
# re-validated server-side at the confirm step.
IMPORT_MAX_ROWS = _env_int("IMPORT_MAX_ROWS", 2000)
IMPORT_MAX_BYTES = _env_int("IMPORT_MAX_BYTES", 2_000_000)
# Character cap on QA review notes (tighter than MAX_TEXTAREA_CHARS).
MAX_QA_NOTES_CHARS = _env_int("MAX_QA_NOTES_CHARS", 2000)
# Escalating sign-in lockout per email+IP (see routes/auth.py).
LOGIN_MAX_FAILURES = _env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_BASE_SECONDS = _env_int("LOGIN_LOCKOUT_BASE_SECONDS", 120)
LOGIN_LOCKOUT_MAX_SECONDS = _env_int("LOGIN_LOCKOUT_MAX_SECONDS", 900)
# Naira per USD for DISPLAY ONLY (see routes.usd_to_ngn).
USD_TO_NGN_RATE = _env_decimal("USD_TO_NGN_RATE", "1600")
# Version tag of the annotator Terms & Conditions. Bump it (or set
# BRYDL_TERMS_VERSION) to re-prompt every annotator to accept the new
# version before they can work again.
TERMS_VERSION = os.environ.get("BRYDL_TERMS_VERSION", "").strip() or "1.0"
# Local dev convenience: create_app() builds missing tables directly.
# Production leaves this OFF (render.yaml sets BRYDL_AUTO_SCHEMA=0) and
# creates/evolves the schema exclusively through `flask db upgrade`.
SCHEMA_AUTO_CREATE = os.environ.get("BRYDL_AUTO_SCHEMA", "1") == "1"


def _sqlite_connect_with_retry(path: str):
    """Build a SQLite connector that retries transient open failures.

    Under a simultaneous burst (many threads requesting fresh pooled
    connections at the same instant), the sqlite3 driver can return
    "unable to open database file" while the -wal/-shm sidecars are
    being created by peer connections. A short bounded retry (worst
    case ~100 ms) makes connect storms deterministic. PostgreSQL
    connections never take this path.

    Args:
        path: The database file path parsed from the engine URI — the
            creator MUST honour it (subprocesses and tests open other
            databases than the default one).

    Returns:
        A zero-argument connector for SQLAlchemy's ``creator`` option.
    """

    def connect():
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                return sqlite3.connect(
                    path, timeout=15, check_same_thread=False
                )
            except sqlite3.OperationalError as exc:
                last_error = exc
                time.sleep(0.01 * (attempt + 1))
        raise last_error  # pragma: no cover - all retries exhausted

    return connect


def _engine_options(uri: str) -> dict:
    """Return SQLAlchemy engine options for the given database URI.

    The two backends need different driver-level knobs: SQLite wants a
    busy timeout in seconds (reducing "database is locked" under
    concurrent annotators), PostgreSQL wants a connect timeout so a
    hiccup on Render's database network fails fast instead of hanging
    a gunicorn worker.

    Args:
        uri: The SQLAlchemy database URI.

    Returns:
        Engine options (pool sizing + backend-specific connect_args).
    """
    options: dict = {
        "pool_pre_ping": True,
        "pool_size": 10,
        "max_overflow": 20,
    }
    if uri.startswith("postgresql"):
        options["connect_args"] = {"connect_timeout": 10}
    else:
        # Retry-enabled creator honouring THIS engine's file path (see
        # _sqlite_connect_with_retry); connect_args do not apply when
        # a creator is provided, so the busy timeout moves inside it.
        db_path = uri.split("sqlite:///", 1)[1]
        options["creator"] = _sqlite_connect_with_retry(db_path)
    return options


# Rows fetched per batch while streaming a dataset export (keeps memory
# flat no matter how large the project is).
EXPORT_YIELD_PER = _env_int("EXPORT_YIELD_PER", 500)
# Self-service password reset: token lifetime, and a per-email request
# throttle (anti email-bombing). In-memory, per worker.
RESET_TOKEN_MAX_AGE_SECONDS = _env_int(
    "RESET_TOKEN_MAX_AGE_SECONDS", 3600
)
FORGOT_MAX_PER_WINDOW = _env_int("FORGOT_MAX_PER_WINDOW", 3)
FORGOT_WINDOW_SECONDS = _env_int("FORGOT_WINDOW_SECONDS", 900)
# Self-claim (Tier 1): max tasks one annotator may claim per UTC day.
CLAIM_DAILY_CAP = _env_int("CLAIM_DAILY_CAP", 20)
# Revision-loop exit: after this many rejections of the SAME task, it
# auto-returns to the pending pool and the annotator is notified, so a
# bad-fit pairing cannot cycle forever.
TASK_MAX_REJECTIONS = _env_int("TASK_MAX_REJECTIONS", 3)
# RLHF preference tasks: the written justification is the arbitration
# evidence — enforce a minimum length (characters).
PREFERENCE_MIN_JUSTIFICATION = _env_int(
    "PREFERENCE_MIN_JUSTIFICATION", 40
)
# Rich rendering cap: task content longer than this renders as plain
# escaped text instead of markdown (processing-cost guard).
MARKDOWN_RENDER_MAX_CHARS = _env_int("MARKDOWN_RENDER_MAX_CHARS", 20_000)
# SFT builder: per-field cap on prompt/completion/system text and the
# hard row cap on one export batch (production safety valve).
SFT_MAX_TEXT_CHARS = _env_int("SFT_MAX_TEXT_CHARS", 20_000)
# Rubric sliders (RLHF Phase 5): dimension list limits and the rating
# scale every slider uses (inclusive bounds).
MAX_RUBRIC_DIMENSIONS = _env_int("MAX_RUBRIC_DIMENSIONS", 6)
MAX_RUBRIC_DIM_NAME = _env_int("MAX_RUBRIC_DIM_NAME", 40)
RUBRIC_SCALE_MIN = _env_int("RUBRIC_SCALE_MIN", 1)
RUBRIC_SCALE_MAX = _env_int("RUBRIC_SCALE_MAX", 5)
# Paystack Transfers (Tier 2 payouts). PAYSTACK_SECRET_KEY is a sk_*
# live/test secret from dashboard.paystack.co; unset/empty = the
# feature is DISABLED and payouts fall back to the CSV batch flow.
# Base URL is overridable so the test gateway can be pinned.
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
PAYSTACK_BASE_URL = os.environ.get(
    "PAYSTACK_BASE_URL", "https://api.paystack.co"
).strip().rstrip("/")
PAYSTACK_TIMEOUT_SECONDS = _env_int("PAYSTACK_TIMEOUT_SECONDS", 15)
# Media workspaces (Phase 8): per-file upload cap. Media bytes live in
# the database (included in backups, survives redeploys); the knob
# keeps rows bounded. SVG is never accepted (script vector).
MEDIA_MAX_BYTES = _env_int("MEDIA_MAX_BYTES", 8 * 1024 * 1024)

# --- Phase 10: API tier + background workers + optional Redis ---------
# Machine API: Bearer keys, project-scoped, per-key minute window.
API_RATE_LIMIT_PER_MINUTE = _env_int("API_RATE_LIMIT_PER_MINUTE", 60)
# Optional shared counter store (Render Key Value / any Redis). Empty
# = in-process counters (per gunicorn worker; documented limitation).
REDIS_URL = os.environ.get("REDIS_URL", "")
# Background jobs: DB-backed queue executed by an in-process runner
# thread (survives redeploys, $0). BRYDL_DISABLE_JOBS=1 turns the
# runner off (the test battery sets it; a dedicated worker service
# later flips JOBS_ENABLED to 0 on the web service instead).
JOBS_ENABLED = os.environ.get("JOBS_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no", "off")
JOBS_POLL_SECONDS = _env_int("JOBS_POLL_SECONDS", 30)
JOB_MAX_ATTEMPTS = _env_int("JOB_MAX_ATTEMPTS", 5)
RECONCILE_MIN_AGE_SECONDS = _env_int("RECONCILE_MIN_AGE_SECONDS", 600)
SFT_EXPORT_MAX_ROWS = _env_int("SFT_EXPORT_MAX_ROWS", 5000)
# Public base URL of the deployment (no trailing slash). Notification
# emails link here; unset = in-app only, emails without links.
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")
# Key for encrypting bank account numbers at rest (Fernet key —
# generate with: Fernet.generate_key()). Unset = derived
# deterministically from SECRET_KEY (fine for dev; set a dedicated
# key in production so SECRET_KEY rotation stays independent of
# bank-data decryption).
BANK_ENC_KEY = os.environ.get("BANK_ENC_KEY", "").strip()
# SMTP for outbound mail. With SMTP_HOST unset (local dev) the reset
# link is written to the application log instead of being emailed —
# the flow still completes end to end.
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = _env_int("SMTP_PORT", 587)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# Envelope sender, e.g. "Brydl <no-reply@brydl.ng>".
MAIL_FROM = (
    os.environ.get("MAIL_FROM", "").strip() or "Brydl <no-reply@brydl.local>"
)
# STARTTLS on 587; 465 uses implicit TLS. Override with SMTP_STARTTLS=0.
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") == "1"


class Config:
    """Central configuration consumed by create_app() in app.py."""

    # Mirror of the module-level MAX_TEXTAREA_CHARS so Jinja templates can
    # render it as maxlength={{ config.MAX_TEXTAREA_CHARS }}.
    MAX_TEXTAREA_CHARS = MAX_TEXTAREA_CHARS

    # --- Security -------------------------------------------------------
    SECRET_KEY = _load_secret_key()

    # True when running on Render (Render exports a RENDER env var) or
    # when forced manually. Over HTTPS these flags stop session and
    # remember-me cookies ever travelling over plaintext HTTP.
    SECURE_COOKIES = os.environ.get("RENDER") is not None
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = SECURE_COOKIES
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SECURE_COOKIES
    REMEMBER_COOKIE_DURATION = timedelta(days=7)

    # --- Flask-WTF --------------------------------------------------------
    WTF_CSRF_ENABLED = True
    # 12 hours instead of the 1-hour default: annotators on intermittent
    # connections can spend longer than an hour on a single form without
    # failing CSRF validation when they finally submit. The extra window
    # is mitigated by SameSite=Lax cookies and the CSP form-action
    # restriction set in app.py.
    WTF_CSRF_TIME_TO_LIVE = 43200

    # --- Database (SQLite locally; DATABASE_URL switches to PostgreSQL) ---
    SQLALCHEMY_DATABASE_URI = _load_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Backend-aware engine options (see _engine_options above): SQLite
    # gets a busy timeout, PostgreSQL a connect timeout; pool sizing
    # supports 50+ concurrent annotators on threaded gunicorn workers.
    SQLALCHEMY_ENGINE_OPTIONS = _engine_options(SQLALCHEMY_DATABASE_URI)

    # Dev-only direct table creation (production migrates instead —
    # see create_app and render.yaml).
    SCHEMA_AUTO_CREATE = SCHEMA_AUTO_CREATE

    # --- Uploads ------------------------------------------------------------
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB

    # --- Pagination, bulk ops, login throttling ---------------------------
    # Tunables live at module level above (importable by routes/tests)
    # and are mirrored here so from_object() exposes them on app.config.
    PAGINATION_PER_PAGE = PAGINATION_PER_PAGE
    UPLOAD_BATCH_LIMIT = UPLOAD_BATCH_LIMIT
    # File-import caps (mirrored for templates).
    IMPORT_MAX_ROWS = IMPORT_MAX_ROWS
    IMPORT_MAX_BYTES = IMPORT_MAX_BYTES
    MAX_QA_NOTES_CHARS = MAX_QA_NOTES_CHARS
    LOGIN_MAX_FAILURES = LOGIN_MAX_FAILURES
    LOGIN_LOCKOUT_BASE_SECONDS = LOGIN_LOCKOUT_BASE_SECONDS
    LOGIN_LOCKOUT_MAX_SECONDS = LOGIN_LOCKOUT_MAX_SECONDS

    # --- Backups ----------------------------------------------------------
    # `flask --app app backup create` writes timestamped snapshots here
    # and prunes beyond BACKUP_KEEP. Override both from the environment.
    BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(
        BASE_DIR, "backups"
    )
    BACKUP_KEEP = _env_int("BACKUP_KEEP", 14)

    # --- Display currency ---------------------------------------------------
    # Fixed Naira rate for DISPLAY ONLY (V1). NGN values are computed at
    # render time via the usd_to_ngn() helper and never stored in the
    # database. Swap this for a live FX source in a later version.
    USD_TO_NGN_RATE = USD_TO_NGN_RATE  # module-level Decimal mirror

    # Annotator terms version (see routes/annotator.py gate).
    TERMS_VERSION = TERMS_VERSION
    # Tier 1 knobs (mirrored for templates).
    CLAIM_DAILY_CAP = CLAIM_DAILY_CAP
    SITE_URL = SITE_URL
    BANK_ENC_KEY = BANK_ENC_KEY
    TASK_MAX_REJECTIONS = TASK_MAX_REJECTIONS
    PREFERENCE_MIN_JUSTIFICATION = PREFERENCE_MIN_JUSTIFICATION
    MARKDOWN_RENDER_MAX_CHARS = MARKDOWN_RENDER_MAX_CHARS
    SFT_MAX_TEXT_CHARS = SFT_MAX_TEXT_CHARS
    MAX_RUBRIC_DIMENSIONS = MAX_RUBRIC_DIMENSIONS
    MAX_RUBRIC_DIM_NAME = MAX_RUBRIC_DIM_NAME
    RUBRIC_SCALE_MIN = RUBRIC_SCALE_MIN
    RUBRIC_SCALE_MAX = RUBRIC_SCALE_MAX
    PAYSTACK_SECRET_KEY = PAYSTACK_SECRET_KEY
    PAYSTACK_BASE_URL = PAYSTACK_BASE_URL
    PAYSTACK_TIMEOUT_SECONDS = PAYSTACK_TIMEOUT_SECONDS
    MEDIA_MAX_BYTES = MEDIA_MAX_BYTES
    # Phase 10 knobs (API tier, background jobs, optional Redis).
    API_RATE_LIMIT_PER_MINUTE = API_RATE_LIMIT_PER_MINUTE
    REDIS_URL = REDIS_URL
    JOBS_ENABLED = JOBS_ENABLED
    JOBS_POLL_SECONDS = JOBS_POLL_SECONDS
    JOB_MAX_ATTEMPTS = JOB_MAX_ATTEMPTS
    RECONCILE_MIN_AGE_SECONDS = RECONCILE_MIN_AGE_SECONDS
    SFT_EXPORT_MAX_ROWS = SFT_EXPORT_MAX_ROWS

    # Dataset export streaming batch size.
    EXPORT_YIELD_PER = EXPORT_YIELD_PER

    # Password reset + SMTP (see mailer.py).
    RESET_TOKEN_MAX_AGE_SECONDS = RESET_TOKEN_MAX_AGE_SECONDS
    SMTP_HOST = SMTP_HOST
    SMTP_PORT = SMTP_PORT
