"""Phase 1 (RedLab): ASGI host + healthcheck scaffold.

Verifies that:

1. ``asgi:app`` composes FastAPI (at /redlab) and Flask (at /) via
   a2wsgi.WSGIMiddleware and boots without error.
2. ``GET /redlab/healthz`` returns 200 JSON in BOTH flag states (ON
   and OFF) — Render's health check MUST succeed during the dark rollout.
3. When REDLAB_ENABLED is NOT "true", every ``/redlab/v1/*`` route
   returns 404 with the canonical ``{"error":"redlab_disabled"}`` body
   for any HTTP method.
4. When REDLAB_ENABLED is "true" but no Phase 4+ router is mounted,
   ``/redlab/v1/*`` returns 404 ``{"error":"not_found"}``.
5. Flask routes (/login, static files, 404s) continue to work at the
   root, served via the WSGI bridge.
6. The JSON-500 error boundary converts any unhandled RedLab exception
   into a sanitised JSON response (no traceback / message leak), and
   stamps an ``X-Request-ID`` header.
7. AnyIO's worker-thread limiter is raised to >= 60 tokens at startup
   so Flask-under-ASGI can absorb 50 concurrent annotators.
8. The feature-flag helper enforces exact-match ``"true"`` (truthy
   variants like ``"1"`` or ``"True"`` keep RedLab OFF — no half-on).
9. All new dependencies import cleanly (fastapi, uvicorn, httpx,
   pydantic v2, a2wsgi, import-linter, cryptography).
"""
from __future__ import annotations
import cryptography
import uvicorn
import a2wsgi
import pydantic
import httpx
import fastapi
import importlib

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# Jobs thread must not run while tests wipe the DB.
os.environ["BRYDL_DISABLE_JOBS"] = "1"
# Tests exercise both sides of the flag explicitly; start from OFF.
os.environ.pop("REDLAB_ENABLED", None)

# --- Bootstrap Flask app (same pattern as other suites) -------------------
from app import create_app  # noqa: E402
from extensions import db    # noqa: E402
from models import User     # noqa: E402

flask = create_app()
with flask.app_context():
    db.drop_all()
    db.create_all()
    ad = User(name="Admin", email="phase1-admin@brydl.test", role="admin")
    ad.set_password("adminx")
    db.session.add(ad)
    db.session.commit()

FAIL: list[str] = []


def expect(cond: bool, msg: str) -> None:
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)
    else:
        print("   ok:", msg)


# --- Dependency imports ----------------------------------------------------
import importlinter  # noqa: E402,F811
expect(int(pydantic.VERSION.split(".")[0]) >= 2,
       "pydantic >= 2.0 installed (got %s)" % pydantic.VERSION)

# --- Reload asgi with flag OFF --------------------------------------------
# Import asgi fresh so startup event has a clean flag state.
import asgi as asgi_mod  # noqa: E402
import redlab.flags as flags_mod  # noqa: E402
import redlab.app as redlab_app_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

expect(flags_mod.is_redlab_enabled() is False,
       "REDLAB_ENABLED defaults to OFF when env var unset")

# --- Exercise flag strictness ---------------------------------------------
# Only the string "true" (case-insensitive, whitespace trimmed) enables
# RedLab. Other truthy variants ("1", "yes", "on") keep it OFF to avoid
# accidental half-rollouts from a typo in the Render dashboard.
os.environ["REDLAB_ENABLED"] = "1"
expect(flags_mod.is_redlab_enabled() is False,
       "REDLAB_ENABLED='1' is treated as OFF")
os.environ["REDLAB_ENABLED"] = "yes"
expect(flags_mod.is_redlab_enabled() is False,
       "REDLAB_ENABLED='yes' is treated as OFF")
os.environ["REDLAB_ENABLED"] = "  TRUE  "
expect(flags_mod.is_redlab_enabled() is True,
       "REDLAB_ENABLED='  TRUE  ' (padded, upper) is treated as ON")
os.environ["REDLAB_ENABLED"] = "true"
expect(flags_mod.is_redlab_enabled() is True,
       "REDLAB_ENABLED='true' turns RedLab ON")
os.environ.pop("REDLAB_ENABLED", None)

# --- Tests with flag OFF --------------------------------------------------
importlib.reload(flags_mod)
importlib.reload(redlab_app_mod)
importlib.reload(asgi_mod)

# Inject two probe routes into the FastAPI sub-app BEFORE TestClient:
# one that raises, one that reports the thread-pool limit.


@redlab_app_mod.app.get("/__boom")
async def _boom():
    raise RuntimeError("super-secret-internal-detail")


@redlab_app_mod.app.get("/__threads")
async def _threads():
    import anyio.to_thread
    lim = anyio.to_thread.current_default_thread_limiter()
    return {"total_tokens": lim.total_tokens}


with TestClient(asgi_mod.app, raise_server_exceptions=False) as c:
    # 1. Healthz always 200
    r = c.get("/redlab/healthz")
    expect(r.status_code == 200, "flag-off /redlab/healthz -> 200")
    body = r.json()
    expect(body["status"] == "ok", "healthz body status=ok")
    expect(body["service"] == "redlab", "healthz body service=redlab")
    expect(body["redlab_enabled"] is False,
           "healthz reports redlab_enabled=false")
    expect("X-Request-ID" in r.headers, "healthz emits X-Request-ID header")

    # 2. /redlab/v1/* is disabled for all methods
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        if method == "GET":
            r = c.request(method, "/redlab/v1/sessions/x/turn")
        else:
            r = c.request(method, "/redlab/v1/sessions/x/turn",
                          json={"ping": 1})
        expect(r.status_code == 404,
               "flag-off /redlab/v1 via %s -> 404 (got %s)"
               % (method, r.status_code))
        ct = r.headers.get("content-type", "")
        expect("application/json" in ct,
               "flag-off /redlab/v1 %s returns JSON (ct=%s)"
               % (method, ct))
        expect(r.json() == {"error": "redlab_disabled"},
               "flag-off /redlab/v1 %s canonical error body" % method)

    # 3. /redlab metadata endpoint
    r = c.get("/redlab")
    expect(r.status_code == 200, "flag-off /redlab -> 200 metadata")
    expect(r.json()["redlab_enabled"] is False,
           "/redlab metadata reports flag off")

    # 4. Flask routes work through the WSGI bridge
    r = c.get("/login")
    expect(r.status_code == 200, "flask /login -> 200 via WSGI bridge")
    expect("csrf_token" in r.text,
           "flask /login renders CSRF token (full template)")

    # /admin/ requires auth; Starlette TestClient follows redirects by
    # default so we get 200 at /login. Use allow_redirects=False to see
    # the original 302 and confirm Flask-Login still guards admin.
    r = c.get("/admin/", follow_redirects=False)
    expect(r.status_code == 302,
           "flask /admin/ -> 302 redirect to login (auth still enforced, got %s)"
           % r.status_code)
    expect("/login" in r.headers.get("location", ""),
           "flask /admin/ redirect Location contains /login")

    r = c.get("/static/does-not-exist.brydl")
    expect(r.status_code == 404,
           "flask static 404 still 404 (got %s)" % r.status_code)

    # 5. JSON-500 error boundary in RedLab sub-app
    r = c.get("/redlab/__boom")
    expect(r.status_code == 500,
           "redlab /__boom -> 500 (error boundary caught) (got %s)" % r.status_code)
    err = r.json()
    expect(err["error"] == "internal_error",
           "500 body error=internal_error (got %r)" % err)
    expect("request_id" in err, "500 body includes request_id")
    expect("super-secret-internal-detail" not in r.text,
           "500 body does NOT leak exception message")
    expect("X-Request-ID" in r.headers, "500 emits X-Request-ID header")
    expect(r.headers["X-Request-ID"] == err["request_id"],
           "X-Request-ID header matches body request_id")

    # 6. Thread limiter raised
    r = c.get("/redlab/__threads")
    expect(r.status_code == 200, "thread probe 200")
    expect(r.json()["total_tokens"] >= 60,
           "AnyIO thread limiter >= 60 (got %s)" % r.json()["total_tokens"])

    # --- Flip flag ON, re-test --------------------------------------------
    os.environ["REDLAB_ENABLED"] = "true"
    importlib.reload(flags_mod)
    # Note: is_redlab_enabled() is read at request time so reload of
    # asgi / redlab.app is not needed.

    r = c.get("/redlab/healthz")
    expect(r.status_code == 200 and r.json()["redlab_enabled"] is True,
           "flag-on /redlab/healthz -> 200 with redlab_enabled=true")

    r = c.get("/redlab")
    expect(r.status_code == 200 and r.json()["redlab_enabled"] is True,
           "flag-on /redlab metadata reports enabled")

    # v1 now returns 'not_found' (not 'redlab_disabled') because the
    # catch-all route forwards to the enabled branch.
    r = c.get("/redlab/v1/some-nonexistent-route")
    expect(r.status_code == 404,
           "flag-on unknown /redlab/v1/... -> 404 (got %s)" % r.status_code)
    expect(r.json()["error"] == "not_found",
           "flag-on unknown v1 route body error=not_found (got %r)" % r.json())

    # POST (which Phase 4 will implement) also falls through cleanly.
    r = c.post("/redlab/v1/sessions/x/turn", json={"message": "hi"})
    expect(r.status_code == 404 and r.json()["error"] == "not_found",
           "flag-on POST to nonexistent v1 route -> JSON 404")

    # Flask still mounted and working
    r = c.get("/login")
    expect(r.status_code == 200 and "csrf_token" in r.text,
           "flask /login still works with REDLAB_ENABLED=true")

    os.environ.pop("REDLAB_ENABLED", None)

# --- Final verdict --------------------------------------------------------
# (Probe routes /__boom and /__threads are left on the singleton — they
# are harmless: no other suite goes through asgi:app, and future phases
# mount their own routers under /v1 which takes precedence.)
print()
if FAIL:
    print("FAILED: %d check(s) failed:" % len(FAIL))
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print("ALL 31 Phase 1 ASGI CHECKS PASSED")
