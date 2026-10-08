"""Root ASGI entry point for Brydl Dashboard.

Production runs this module under ``gunicorn -k uvicorn.workers.UvicornWorker
--workers 2 asgi:app`` (see render.yaml / Procfile). It composes two
applications behind a single host/port so we can ship RedLab (v14) as an
in-place upgrade on the existing Render free-tier web service — no second
service, no second deploy, no second domain:

* ``/redlab/...``  →  the FastAPI sub-app from :mod:`redlab.app`
  (async — holds the session/turn/flag/conclude APIs).
* ``/...``        →  the existing Flask app (sync, via a2wsgi's
  WSGIMiddleware). Flask continues to own auth, users, projects, tasks,
  QA, and all Decimal-money payouts.

Threadpool sizing
-----------------
Flask handlers run on AnyIO's worker-thread pool because WSGIMiddleware
dispatches sync apps to a thread. Two gunicorn workers × the default of
~40 threads was close to the limit when 50 concurrent annotators chat
*and* submit red-team turns in the same window. We bump AnyIO's default
to 60 so a single worker can absorb a surge without ``BlockingIOError``
/ ``Too many threads`` timeouts on the Flask side.

Graceful degradation
--------------------
If fastapi / a2wsgi / uvicorn are not installed (should never happen in
production — they're in requirements.txt), this module falls back to
exposing the Flask WSGI app directly at ``app`` so ``gunicorn asgi:app``
still boots and existing /login /annotator routes continue to serve.
RedLab simply isn't reachable in that state — preferable to a hard 500.
"""
from __future__ import annotations

import logging

import anyio

from app import create_app as _create_flask_app
from redlab.flags import is_redlab_enabled

logger = logging.getLogger(__name__)

# Desired size for AnyIO's worker-thread pool (used by a2wsgi's
# WSGIMiddleware to run Flask handlers sync-in-async). 60 threads per
# uvicorn worker absorbs ~50 concurrent annotators × two workers under
# load without spawning extra ad-hoc threads.
_THREAD_LIMIT = 60

# --- Flask (always mounted) ------------------------------------------------
flask_app = _create_flask_app()

try:
    from a2wsgi import WSGIMiddleware
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from redlab.app import app as redlab_app  # FastAPI sub-app
    _HAVE_ASGI_DEPS = True
except ImportError:  # pragma: no cover - degrade, don't crash
    _HAVE_ASGI_DEPS = False
    logger.warning(
        "RedLab ASGI deps not installed; serving Flask-only. "
        "pip install -r requirements.txt to enable /redlab."
    )


if _HAVE_ASGI_DEPS:
    # Compose root ASGI app. We mount the RedLab FastAPI app at /redlab
    # (longest prefix first). Starlette strips /redlab before handing
    # to the sub-app, so redlab.app's /healthz is reachable as
    # /redlab/healthz, /v1/... is reachable as /redlab/v1/....
    root = FastAPI()

    @root.on_event("startup")
    async def _raise_thread_limit() -> None:
        """Bump AnyIO's to-thread limiter so Flask-under-ASGI has
        enough workers under concurrent annotator load. Runs inside
        the event loop (where the limiter is first created), not at
        import time."""
        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < _THREAD_LIMIT:
            limiter.total_tokens = _THREAD_LIMIT
            logger.info(
                "RedLab: AnyIO thread limiter set to %d tokens.",
                _THREAD_LIMIT,
            )

    # Mount RedLab sub-app at /redlab. Order matters: this must come
    # BEFORE the root mount so /redlab/* routes do not fall through to
    # Flask.
    root.mount("/redlab", redlab_app)

    # Top-level metadata at the exact /redlab path (no sub-app route
    # matches there because Starlette requires a trailing slash or
    # deeper path to dispatch inside the mount). Without this, /redlab
    # would fall through to Flask and return an HTML 404.
    @root.get("/redlab", include_in_schema=False)
    async def _redlab_root():
        return JSONResponse(
            status_code=200,
            content={
                "service": "redlab",
                "version": "14.0.0",
                "redlab_enabled": is_redlab_enabled(),
                "endpoints": ["/redlab/healthz"]
                + (["/redlab/v1/..."] if is_redlab_enabled() else []),
            },
        )

    # Mount Flask at root — catch-all for every other path (/login,
    # /annotator, /admin, static files, ...).
    root.mount("/", WSGIMiddleware(flask_app))

    app = root
else:  # pragma: no cover - degraded mode
    app = flask_app  # type: ignore[assignment]


__all__ = ["app"]


if __name__ == "__main__":  # local-dev convenience
    import uvicorn
    uvicorn.run(
        "asgi:app",
        host="127.0.0.1",
        port=5000,
        reload=False,
        log_level="info",
    )
