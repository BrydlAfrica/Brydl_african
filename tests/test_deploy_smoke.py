"""Phase 13-I/J: deploy config + Sentry smoke test."""
from __future__ import annotations
from app import create_app
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["BRYDL_DISABLE_JOBS"] = "1"
FAIL = []


def expect(c, m):
    if not c:
        FAIL.append(m)
        print("  FAIL:", m)
    else:
        print("   ok:", m)


# 1. render.yaml + Procfile present and sensible
expect(os.path.exists("render.yaml"), "render.yaml present")
expect(os.path.exists("Procfile"), "Procfile present")
ry = open("render.yaml").read()
expect("flask --app app db upgrade" in ry, "render build runs migrations")
expect("gunicorn" in ry and "healthCheckPath" in ry, "gunicorn + health check")
expect("brydl-postgres" in ry and "DATABASE_URL" in ry, "PostgreSQL provisioned")
expect("BRYDL_AUTO_SCHEMA" in ry and "value: \"0\"" in ry,
       "auto-schema disabled in prod")

pf = open("Procfile").read()
expect("web: gunicorn" in pf, "Procfile uses gunicorn")

# 2. Deploy guide + smoke checklist exist
expect(os.path.exists("DEPLOY_GUIDE.md"), "deploy guide exists")
expect(os.path.exists("POST_DEPLOY_CHECKLIST.md"), "post-deploy checklist exists")

# 3. Sentry optional: app boots without DSN, with DSN it initializes.
os.environ.pop("SENTRY_DSN", None)
app = create_app()
with app.test_client() as c:
    r = c.get("/login")
    expect(r.status_code == 200, "app boots cleanly without SENTRY_DSN")

os.environ["SENTRY_DSN"] = "https://examplePublicKey@o0.ingest.sentry.io/0"
os.environ["SENTRY_TRACES_SAMPLE_RATE"] = "0.0"
app2 = create_app()
with app2.test_client() as c:
    r = c.get("/login")
    expect(r.status_code == 200, "app boots cleanly with SENTRY_DSN set")

# 4. Runtime + requirements files valid
expect(os.path.exists("runtime.txt"), "runtime.txt present")
rt = open("runtime.txt").read().strip()
expect(rt.startswith("python-"), "runtime.txt pins a python version")
req = open("requirements.txt").read()
expect("sentry-sdk" in req, "sentry-sdk pinned in requirements")
expect("gunicorn" in req and "psycopg2-binary" in req and "Flask-Migrate" in req,
       "core deploy deps pinned")

# 5. Health endpoint returns 200 (login page works as Render health path)
with app.test_client() as c:
    r = c.get("/login")
    expect(r.status_code == 200, "health check path /login returns 200")

print()
if FAIL:
    print(f"DEPLOY/SENTRY SMOKE TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL DEPLOY + SENTRY SMOKE TESTS PASSED")
