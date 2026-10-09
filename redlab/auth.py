"""Authentication & CSRF dependencies for the RedLab FastAPI sub-app.

RedLab mounts alongside Flask under a single host via :mod:`asgi`;
annotators authenticate through the existing Flask login flow, so
their browser already holds the Flask signed session cookie
(``session``, signed with ``SECRET_KEY`` via :mod:`itsdangerous`).

This module reuses that cookie:

* :func:`get_current_annotator` reads the cookie, verifies its
  signature and age, loads the matching ``User`` row from the shared
  database, and rejects anyone who is not authenticated or lacks
  annotator/admin role.
* :func:`verify_csrf_header` validates the ``X-CSRFToken`` request
  header against the CSRF token stashed inside the Flask session
  (flask-wtf convention: token stored under ``csrf_token``).

Both are FastAPI Depends() dependencies so they compose cleanly with
the Phase 4/5/6 routers.

Cookie/session notes
--------------------
* Flask's default session serializer is
  :class:`itsdangerous.URLSafeTimedSerializer` with
  ``salt="cookie-session"``. We derive a serializer lazily using
  the active Flask app's ``secret_key`` so it stays in sync with
  whatever :func:`config._load_secret_key` produced (env var or
  instance/secret_key fallback).
* The module imports the Flask app lazily too — tests construct the
  RedLab FastAPI app first, set env vars, and then build the Flask
  app; resolving ``flask_app`` inside the dependency (cached per
  process) avoids import-order problems.
* Maximum session age is 30 days, matching Flask-Login's default
  ``REMEMBER_COOKIE_DURATION`` fallback and flask-wtf's CSRF TTL of
  43200s (12 hours) for the CSRF check itself.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from fastapi import Header, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from itsdangerous.exc import SignatureExpired

logger = logging.getLogger(__name__)

# Name of the session cookie Flask sets. Keep in sync with the Flask
# config (SESSION_COOKIE_NAME default is "session").
SESSION_COOKIE_NAME: str = "session"
# CSRF header the annotator SPA must send on mutating requests.
CSRF_HEADER_NAME: str = "x-csrftoken"
# Name under which flask-wtf stores the CSRF token inside the session.
CSRF_SESSION_KEY: str = "csrf_token"
# Max accepted age of the session cookie (seconds). 30 days is the
# Flask-Login "remember" default; non-"remember" sessions are shorter
# but still bounded here as a defence-in-depth cap.
MAX_SESSION_AGE: int = 30 * 24 * 3600


class RedLabAuthError(HTTPException):
    """Raised for any auth/CSRF failure; mapped to JSON by FastAPI."""

    def __init__(self, code: str, detail: str,
                 http_status: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(status_code=http_status, detail={
            "error": code,
            "detail": detail,
        })


@lru_cache(maxsize=1)
def _flask_app():  # pragma: no cover - returns live Flask app
    """Return the cached Flask app (created lazily).

    Imported at first call so env-driven config has time to settle.
    The wrapper is importable at module load without forcing the
    whole Flask stack to init.
    """
    from app import create_app
    return create_app()


@lru_cache(maxsize=1)
def _serializer() -> URLSafeTimedSerializer:
    """Return an itsdangerous serializer matching Flask's config.

    Flask's session interface uses ``URLSafeTimedSerializer`` with
    salt ``"cookie-session"`` on the app's ``secret_key``. We mirror
    that so we can verify cookies without going through Flask's
    request stack.
    """
    app = _flask_app()
    secret: Any = app.secret_key
    # Fallback: if secret_key is a list/tuple (fallback chain), use
    # the first one — Flask accepts multiple keys for rotation, but
    # signing always uses [0].
    if isinstance(secret, (list, tuple)):
        secret = secret[0]
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return URLSafeTimedSerializer(
        secret_key=secret,
        salt="cookie-session",
        signer_kwargs={"key_derivation": "hmac", "digest_method": __import__(
            "hashlib").sha1},
    )


def _load_session(cookie_val: str) -> dict:
    """Verify the signed session cookie and return its payload.

    Raises ``RedLabAuthError`` on bad signature / expired / bad
    structure; never returns partially-decoded data.
    """
    if not cookie_val:
        raise RedLabAuthError("not_authenticated",
                              "missing session cookie")
    s = _serializer()
    try:
        data = s.loads(cookie_val, max_age=MAX_SESSION_AGE)
    except SignatureExpired:
        raise RedLabAuthError("session_expired",
                              "session cookie expired")
    except BadSignature:
        raise RedLabAuthError("bad_session",
                              "invalid session signature")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RedLab session decode failed: %s", exc)
        raise RedLabAuthError("bad_session",
                              "malformed session cookie") from exc
    if not isinstance(data, dict):
        raise RedLabAuthError("bad_session", "session payload not a dict")
    return data


def _load_user(session_data: dict):
    """Load the User row referenced by the Flask-Login session.

    Returns ``None`` if the user id is missing or the row is gone.
    Pushes a Flask app context so the call works when reached from
    the FastAPI side (where there is otherwise no active Flask
    request/context — FastAPI runs on AnyIO worker threads).
    """
    # Flask-Login stores the authenticated user id under "_user_id"
    # (as a string). It also stores "_fresh" and "_id" for its own
    # bookkeeping; we only need the primary key.
    uid = session_data.get("_user_id")
    if not uid:
        return None
    # Flask-Login's user_loader is fed the str id; the User model
    # uses an integer PK.
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return None
    app = _flask_app()
    from models import User  # noqa: WPS433
    # Push an app context if we are not already inside one so that
    # ``User.query`` / ``db.session`` resolve from the FastAPI
    # worker thread. ``app_context()`` is re-entrant so calling it
    # when already in a request context is a no-op for our purposes.
    ctx = None
    from flask import has_app_context  # local to avoid circulars
    if not has_app_context():
        ctx = app.app_context()
        ctx.push()
    try:
        return User.query.get(uid_int)
    finally:
        if ctx is not None:
            ctx.pop()


def get_current_annotator(request: Request):
    """FastAPI dependency: return the authenticated User.

    Raises 401 with a JSON body if the session cookie is missing /
    invalid / expired, or the user is inactive. Raises 403 if the
    user does not have an annotator/admin role.
    """
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME, "")
    data = _load_session(cookie_val)
    user = _load_user(data)
    if user is None:
        raise RedLabAuthError("not_authenticated",
                              "no user for session")
    is_active = bool(getattr(user, "is_active", True))
    if not is_active:
        raise RedLabAuthError("account_disabled",
                              "user account disabled",
                              http_status=status.HTTP_403_FORBIDDEN)
    role = (getattr(user, "role", "") or "").lower()
    if role not in ("annotator", "admin", "pm", "qa"):
        # Only red-team-facing roles are allowed to hit RedLab APIs.
        raise RedLabAuthError("forbidden",
                              f"role '{role}' cannot access RedLab",
                              http_status=status.HTTP_403_FORBIDDEN)
    return user


def _csrf_serializer() -> URLSafeTimedSerializer:
    """Build a URLSafeTimedSerializer matching flask-wtf's CSRF setup.

    flask-wtf uses salt ``"wtf-csrf-token"`` against ``SECRET_KEY``
    and URLSafeTimedSerializer defaults (HMAC/SHA1) for both
    :func:`flask_wtf.csrf.generate_csrf` and :func:`validate_csrf`.
    """
    import hashlib
    app = _flask_app()
    secret: Any = app.config.get("WTF_CSRF_SECRET_KEY") or app.secret_key
    if isinstance(secret, (list, tuple)):
        secret = secret[0]
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return URLSafeTimedSerializer(
        secret_key=secret, salt="wtf-csrf-token",
        signer_kwargs={"key_derivation": "hmac",
                       "digest_method": hashlib.sha1},
    )


def verify_csrf_header(
    request: Request,
    x_csrftoken: str | None = Header(default=None, alias="X-CSRFToken"),
) -> None:
    """FastAPI dependency: verify X-CSRFToken matches session token.

    The header is expected to be the SIGNED form (as produced by
    :func:`flask_wtf.csrf.generate_csrf`); the session stores the RAW
    random token. We decode the signed header with the same salt
    flask-wtf uses, then constant-time compare against the session
    value. A missing / expired / mismatched token raises 403.
    """
    import hmac

    cookie_val = request.cookies.get(SESSION_COOKIE_NAME, "")
    data = _load_session(cookie_val)
    expected_raw = data.get(CSRF_SESSION_KEY)
    if not expected_raw:
        raise RedLabAuthError("csrf_missing",
                              "session has no csrf token",
                              http_status=status.HTTP_403_FORBIDDEN)
    provided = (x_csrftoken or "").strip()
    if not provided:
        raise RedLabAuthError("csrf_missing",
                              "missing X-CSRFToken header",
                              http_status=status.HTTP_403_FORBIDDEN)

    # flask-wtf's default TTL is 3600s, with WTF_CSRF_TIME_LIMIT
    # overriding; fall back to that if configured, else 1 hour.
    app = _flask_app()
    ttl = app.config.get("WTF_CSRF_TIME_LIMIT", 3600) if app else 3600
    try:
        decoded = _csrf_serializer().loads(provided, max_age=ttl)
    except Exception:  # noqa: BLE001
        raise RedLabAuthError("csrf_invalid",
                              "X-CSRFToken invalid or expired",
                              http_status=status.HTTP_403_FORBIDDEN)
    if not hmac.compare_digest(str(expected_raw), str(decoded)):
        raise RedLabAuthError("csrf_invalid",
                              "X-CSRFToken does not match session",
                              http_status=status.HTTP_403_FORBIDDEN)


__all__ = [
    "RedLabAuthError", "SESSION_COOKIE_NAME", "CSRF_HEADER_NAME",
    "CSRF_SESSION_KEY", "MAX_SESSION_AGE", "get_current_annotator",
    "verify_csrf_header",
]
