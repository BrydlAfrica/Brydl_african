import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Deployment-readiness tests: files, seed script (incl. production
guard + idempotency), and the backup CLI (create/manifest/retention/
verify/restore/safety)."""
import glob
import json
import os
import subprocess
import sys

PROJ = ROOT
VENV_PY = sys.executable
BACKUP_KEEP = "3"

FAILURES = []


def run(args, env_extra=None):
    """Run a project subprocess; return CompletedProcess."""
    env = dict(os.environ, PYTHONPATH=PROJ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [VENV_PY] + args,
        cwd=PROJ,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def db_counts():
    from extensions import db
    from models import User, Project, Task, Submission, AuditLog
    from app import create_app
    app = create_app()
    with app.app_context():
        def count(model):
            return db.session.scalar(db.select(db.func.count(model.id)))
        return {
            "users": count(User),
            "projects": count(Project),
            "tasks": count(Task),
            "submissions": count(Submission),
            "audits": count(AuditLog),
        }


print("== 1. deployment files ==")
# v14+: gunicorn serves the ASGI entry point (FastAPI-at-/redlab
# composed with Flask-at-/ via a2wsgi) using uvicorn workers, 2
# workers for free-tier CPU, threadpool raised in asgi.py startup.
procfile = open(f"{PROJ}/Procfile").read()
expect("gunicorn" in procfile and "uvicorn.workers.UvicornWorker" in procfile
       and "asgi:app" in procfile,
       "Procfile starts gunicorn + UvicornWorker against asgi:app")
runtime = open(f"{PROJ}/runtime.txt").read()
expect(runtime.strip() == "python-3.11.6", "runtime.txt pins python-3.11.6")
render = open(f"{PROJ}/render.yaml").read()
for needle in ("brydl-dashboard", "plan: free",
               "buildCommand: pip install -r requirements.txt",
               "uvicorn.workers.UvicornWorker", "--workers", "asgi:app",
               "healthCheckPath: /redlab/healthz",
               "PYTHON_VERSION", "SECRET_KEY", "generateValue: true",
               "FLASK_ENV", "production"):
    expect(needle in render, f"render.yaml contains {needle!r}")
gitignore = open(f"{PROJ}/.gitignore").read()
for needle in ("instance/", "__pycache__/", "*.py[cod]", ".env", "venv/",
               "backups/"):
    expect(needle in gitignore, f".gitignore covers {needle!r}")
config_src = open(f"{PROJ}/config.py").read()
for needle in ('os.environ.get("SECRET_KEY"', "DATABASE_URL",
               "BACKUP_DIR", "BACKUP_KEEP"):
    expect(needle in config_src, f"config.py has {needle!r}")
app_src = open(f"{PROJ}/app.py").read()
expect("ProxyFix" in app_src and "RENDER" in app_src,
       "app.py handles Render proxy headers (ProxyFix)")
expect("backup_cli" in app_src, "app.py registers the backup CLI")

print("== 2. seed.py: fresh run ==")
for path in (f"{PROJ}/instance", f"{PROJ}/backups"):
    subprocess.run(["rm", "-rf", path], check=True)
proc = run(["seed.py"])
expect(proc.returncode == 0, f"seed.py exits 0 ({proc.stderr[-200:]})")
expect("admin@brydl.com" in proc.stdout, "prints admin credentials")
expect("REFUSING" not in proc.stdout, "does not refuse in dev")
counts = db_counts()
expect(counts["users"] == 10, f"10 users (1 admin + 4 staff + 5 annotators): {counts}")
expect(counts["projects"] == 1, "1 sample project")
expect(counts["tasks"] == 10, "10 sample tasks")

from app import create_app
from extensions import db
from models import User, Project, Task, AuditLog
app = create_app()
with app.app_context():
    roles = dict(db.session.execute(
        db.select(User.role, db.func.count(User.id)).group_by(User.role)
    ).all())
    expect(roles.get("admin") == 1 and roles.get("annotator") == 5,
           f"role split 1 admin / 5 annotators: {roles}")
    statuses = dict(db.session.execute(
        db.select(Task.status, db.func.count(Task.id)).group_by(Task.status)
    ).all())
    expect(statuses.get("assigned") == 4 and statuses.get("pending") == 6,
           f"4 pre-assigned + 6 pending: {statuses}")
    golds = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.expected_output.is_not(None)))
    expect(golds == 3, f"3 gold-standard tasks: {golds}")
    project = db.session.scalar(db.select(Project))
    from decimal import Decimal
    expect(project.status == "active"
           and project.pay_per_task == Decimal("0.10"),
           f"project active at $0.10/task ({project.pay_per_task!r})")

print("== 3. seed.py: idempotent re-run ==")
before = db_counts()
proc = run(["seed.py"])
expect(proc.returncode == 0, "re-run exits 0")
after = db_counts()
# Entities are idempotent; the audit trail grows by exactly the one
# system.seed row — that growth is correct behaviour, not duplication.
expect({k: v for k, v in before.items() if k != "audits"}
       == {k: v for k, v in after.items() if k != "audits"},
       f"no duplicate entities: {before} -> {after}")
expect(after["audits"] == before["audits"] + 1,
       "exactly one new audit row per re-seed")

print("== 4. seed.py: production guard ==")
proc = run(["seed.py"], env_extra={"RENDER": "1"})
expect(proc.returncode != 0 and "REFUSING to seed" in (proc.stdout + proc.stderr),
       "refuses when RENDER is set")
proc = run(["seed.py"], env_extra={"FLASK_ENV": "production"})
expect(proc.returncode != 0 and "REFUSING to seed" in (proc.stdout + proc.stderr),
       "refuses when FLASK_ENV=production")

print("== 5. seeded credentials actually sign in ==")
from flask_wtf.csrf import generate_csrf
import re
client = app.test_client()
page = client.get("/login").get_data(as_text=True)
tk = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
r = client.post("/login", data={"email": "admin@brydl.com",
                                "password": "admin123", "csrf_token": tk})
expect(r.status_code == 302 and "/admin" in r.headers["Location"],
       "admin@brydl.com / admin123 signs in to the admin dashboard")

print("== 6. backup create + manifest + retention ==")
for i in range(5):
    proc = run(["-m", "flask", "--app", "app", "backup", "create"],
               env_extra={"BACKUP_KEEP": BACKUP_KEEP})
    if proc.returncode != 0:
        print(proc.stdout[-500:], proc.stderr[-500:])
        expect(False, f"backup create #{i + 1}")
        break
else:
    expect(True, "5 backup creates ran")
snaps = sorted(glob.glob(f"{PROJ}/backups/brydl-*.db"))
expect(len(snaps) == int(BACKUP_KEEP),
       f"retention keeps {BACKUP_KEEP}: {len(snaps)} present")
latest = snaps[-1]
manifest = json.load(open(
    latest.replace(".db", ".manifest.json"), encoding="utf-8"))
import hashlib
digest = hashlib.sha256(open(latest, "rb").read()).hexdigest()
expect(manifest["sha256"] == digest, "manifest sha256 matches the file")
expect(manifest["row_counts"]["tasks"] == 10
       and manifest["row_counts"]["users"] == 10,
       f"manifest row counts sane: {manifest['row_counts']}")
expect(manifest["integrity_check"] == "ok", "manifest integrity ok")
proc = run(["-m", "flask", "--app", "app", "backup", "list"])
expect(proc.returncode == 0 and "brydl-" in proc.stdout, "backup list works")
name = os.path.basename(latest)
proc = run(["-m", "flask", "--app", "app", "backup", "verify", name])
expect("checksum: OK" in proc.stdout and "sqlite integrity: ok"
       in proc.stdout, "backup verify passes")

print("== 7. backup restore ==")
with app.app_context():
    db.session.add(Task(project_id=1, input_data="EXTRA after backup"))
    db.session.commit()
    extra = db.session.scalar(db.select(db.func.count(Task.id)))
expect(extra == 11, "extra task added post-backup")
proc = run(["-m", "flask", "--app", "app", "backup", "restore", name])
expect(proc.returncode == 0 and "DRY-RUN" in proc.stdout,
       "restore without --yes is a dry-run")
with app.app_context():
    still = db.session.scalar(db.select(db.func.count(Task.id)))
expect(still == 11, "dry-run wrote nothing")
proc = run(["-m", "flask", "--app", "app", "backup", "restore", name,
            "--yes"])
expect(proc.returncode == 0 and "Restored" in proc.stdout
       and "restart the web service" in proc.stdout,
       "restore --yes succeeds + restart guidance shown")
# A restarted worker opens a FRESH connection — emulate by disposing the
# pooled engine before reading.
with app.app_context():
    db.session.remove()
    app.extensions["sqlalchemy"].engine.dispose()
app2 = create_app()
with app2.app_context():
    restored = db.session.scalar(db.select(db.func.count(Task.id)))
expect(restored == 10, f"extra task gone after restore: {restored}")
proc = run(["-m", "flask", "--app", "app", "backup", "restore",
            "../evil.db", "--yes"])
expect(proc.returncode != 0, "path traversal refused")

print("== 8. backup + seed events audited ==")
with app.app_context():
    actions = set(db.session.scalars(db.select(AuditLog.action)).all())
expect("system.backup" in actions and "system.restore" in actions
       and "system.seed" in actions, f"ops events audited: {sorted(actions)}")

subprocess.run(["rm", "-rf", f"{PROJ}/backups", f"{PROJ}/instance"],
               check=True)

print()
if FAILURES:
    print(f"DEPLOY TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL DEPLOY TESTS PASSED")
