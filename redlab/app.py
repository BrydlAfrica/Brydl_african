"""RedLab FastAPI sub-application.

Mounted under ``/redlab`` by :mod:`asgi` (the root ASGI entry point).
Phase 1 scope is minimal:

* A ``GET /healthz`` endpoint that returns 200 with a static JSON body
  (used by Render's HTTP health-check — it MUST succeed even when the
  feature flag is OFF, so the platform keeps serving the Flask app while
  we ship RedLab code dark).
* A JSON error-boundary middleware that converts any unhandled exception
  into a ``{"error": "internal_error", "request_id": ...}`` 500 response
  instead of leaking a traceback or an HTML error page (RedLab is a
  machine-facing API; clients expect JSON at all times).
* A dependency-gated ``/v1/*`` sub-router: when ``REDLAB_ENABLED`` is
  false every sub-route returns 404 ``{"error": "redlab_disabled"}``;
  when true later phases mount their routers (sessions, messages, …)
  onto ``v1_router``.

The sub-app is built lazily via :func:`create_redlab_app` so tests can
construct it repeatedly with different flag states; :data:`app` is the
module-level instance used by ``asgi:app``.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from redlab.flags import is_redlab_enabled

logger = logging.getLogger(__name__)

# Default body for the disabled-v1 namespace — single source of truth so
# both the explicit 404 router and any future gate can return identical
# JSON.
DISABLED_BODY: dict[str, str] = {"error": "redlab_disabled"}


def _request_id_from(request: Request) -> str:
    """Extract a correlation id for JSON error responses.

    Uses the WSGI/ASGI ``X-Request-ID`` header if present (matching the
    Flask access log convention in :mod:`audit`), otherwise generates a
    fresh uuid4. Clients can echo this id back when reporting failures.
    """
    existing = request.headers.get("x-request-id")
    if existing:
        return existing
    return uuid.uuid4().hex[:12]


async def _json_error_boundary(
    request: Request, call_next: Any
) -> JSONResponse:
    """Catch-all error middleware returning a JSON 500 on any exception.

    Logs the exception with the request id and returns a sanitised body
    — never the exception text, never a traceback, so that internal
    details (SQL strings, API keys, model responses) cannot leak to
    the client.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
        # Re-emit X-Request-ID on successful responses too so clients
        # always have one to reference.
        rid = response.headers.get("X-Request-ID") or _request_id_from(request)
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:  # noqa: BLE001 — deliberate catch-all boundary
        rid = _request_id_from(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "RedLab unhandled error rid=%s path=%s dur=%dms",
            rid,
            request.url.path,
            duration_ms,
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "request_id": rid},
            headers={"X-Request-ID": rid},
        )


def create_redlab_app() -> FastAPI:
    """Construct the RedLab FastAPI sub-app.

    Built as a factory so tests can swap env vars and rebuild; the
    module-level :data:`app` below is the singleton used in production.
    """
    app = FastAPI(
        title="Brydl RedLab",
        version="14.0.0",
        description=(
            "Dynamic Red-Teaming as a Service — African-language "
            "adversarial evaluation of LLMs via Brydl's annotator pool."
        ),
        docs_url=None,   # no Swagger UI in production (Phase 9 tightens)
        redoc_url=None,
        openapi_url=None,
    )
    app.middleware("http")(_json_error_boundary)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        """Liveness probe for Render / ops.

        Returns 200 unconditionally. The response distinguishes flag
        state (``redlab_enabled: true|false``) so a deploy check can
        verify the flag sits at the expected value.
        """
        return {
            "status": "ok",
            "service": "redlab",
            "version": "14.0.0",
            "redlab_enabled": is_redlab_enabled(),
        }

    # /v1 namespace — gated. Phases 4-8 mount routers onto v1_router when
    # implemented; until then (and forever when the flag is off) the
    # catch-all returns a stable disabled JSON body.
    v1_router = FastAPI()
    v1_router.middleware("http")(_json_error_boundary)

    @v1_router.api_route(
        "/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
    )
    async def v1_gate(full_path: str) -> JSONResponse:
        if is_redlab_enabled():
            # Flag is ON but no router matched yet — real 404.
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "path": full_path},
            )
        return JSONResponse(status_code=404, content=DISABLED_BODY)

    app.mount("/v1", v1_router)
    return app


# Module-level singleton. Imported by asgi:app at startup.
app: FastAPI = create_redlab_app()
