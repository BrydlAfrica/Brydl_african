import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Scalability requirements tests: indexes, pagination everywhere,
N+1 freedom, Decimal money, concurrency, paths, config-driven tuning."""
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import create_app
from extensions import db
from models import (
    AuditLog,
    Payment,
    Project,
    Submission,
    Task,
    User,
    money,
    utc_now,
)
from sqlalchemy import event

app = create_app()


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email, "password": password,
                                       "csrf_token": tk})


with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann_a.set_password("annotator-pass-1")
    db.session.add_all([admin, ann_a])
    db.session.commit()
    admin_id, ann_a_id = admin.id, ann_a.id

c = app.test_client()
login(c, "admin@brydl.ng", "admin-pass-1")
ra = app.test_client()
login(ra, "a@brydl.ng", "annotator-pass-1")

# ---- 1. composite indexes exist ----
conn = sqlite3.connect(
    ROOT + "/instance/brydl.db"
)
expected = {
    "tasks": {"ix_tasks_assignee_status", "ix_tasks_project_status"},
    "submissions": {
        "ix_submissions_annotator_status",
        "ix_submissions_status_payment",
        "ix_submissions_annotator_submitted",
    },
    "payments": {
        "ix_payments_annotator_status",
        "ix_payments_status_paid_at",
    },
    "users": {"ix_users_role_active"},
}
for table, wanted in expected.items():
    found = {
        row[1] for row in conn.execute(
            f"PRAGMA index_list({table})"
        ).fetchall()
    }
    missing = wanted - found
    assert not missing, f"{table} missing indexes: {missing}"
# money columns are NUMERIC affinity
cols = {row[1]: row[2].upper() for row in conn.execute(
    "PRAGMA table_info(payments)").fetchall()}
assert cols["amount"].startswith("NUMERIC"), cols
cols = {row[1]: row[2].upper() for row in conn.execute(
    "PRAGMA table_info(projects)").fetchall()}
assert cols["pay_per_task"].startswith("NUMERIC"), cols
cols = {row[1]: row[2].upper() for row in conn.execute(
    "PRAGMA table_info(users)").fetchall()}
assert "pay_rate" not in cols, "dead pay_rate column must be gone"
conn.close()
print("1. indexes: composites present, money columns NUMERIC,"
      " pay_rate gone OK")

# ---- 2. pagination on every list view (20/page from config) ----
with app.app_context():
    from config import PAGINATION_PER_PAGE
    assert PAGINATION_PER_PAGE == 20
# 25 projects -> page 1 has 20, page 2 has 5
with app.app_context():
    for i in range(25):
        db.session.add(Project(
            name=f"Scale Proj {i:02d}", client_name="C",
            task_type="classification", pay_per_task=money("0.10"),
            instructions="i", created_by=admin_id))
    db.session.commit()
html = c.get("/admin/projects").get_data(as_text=True)
assert "Showing 20 of 25 project" in html
assert 'aria-label="Project pages"' in html
html = c.get("/admin/projects?page=2").get_data(as_text=True)
assert "Scale Proj 00" in html and "Scale Proj 04" in html
html1 = c.get("/admin/projects").get_data(as_text=True)
assert "Scale Proj 24" in html1 and "Scale Proj 00" not in html1
# audit trail paginates at 20 too (was 50 before this requirement)
with app.app_context():
    for i in range(25):
        from audit import log_event
        log_event("test.bulk", actor=admin, entity_type="test",
                  entity_id=i, commit=True)
html = c.get("/admin/audit").get_data(as_text=True)
m = re.search(r"Showing (\d+) of (\d+) event", html)
assert m and m.group(1) == "20", m.group(0) if m else "no footer"
print("2. pagination: projects + audit at 20/page with working pager OK")

# ---- 3. no N+1: query counts stay flat as rows grow ----
query_counts = []

def count_hook(conn, cursor, statement, params, context, executemany):
    query_counts.append(statement)

with app.app_context():
    proj = db.session.scalar(db.select(Project).where(
        Project.name == "Scale Proj 00"))
    for i in range(30):
        db.session.add(Task(project_id=proj.id,
                            input_data=f"n+1 probe {i}"))
    db.session.commit()

def measure(client, path):
    query_counts.clear()
    with app.app_context():
        engine = app.extensions["sqlalchemy"].engine
    event.listen(engine, "before_cursor_execute", count_hook)
    try:
        assert client.get(path).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count_hook)
    return len(query_counts)

tasks_small = measure(c, "/admin/tasks")
with app.app_context():
    proj = db.session.scalar(db.select(Project).where(
        Project.name == "Scale Proj 00"))
    for i in range(70):
        db.session.add(Task(project_id=proj.id,
                            input_data=f"n+1 probe bulk {i}"))
    db.session.commit()
tasks_big = measure(c, "/admin/tasks")
assert tasks_big <= tasks_small + 2, (tasks_small, tasks_big)
assert tasks_big < 20, f"tasks list used {tasks_big} queries"
hist = measure(ra, "/annotator/history")
assert hist < 12, f"history used {hist} queries"
rev = measure(c, "/admin/review")
assert rev < 20, f"review queue used {rev} queries"
print(f"3. no N+1: tasks list {tasks_small}->{tasks_big} queries for"
      f" 5x rows (flat); history {hist}, review {rev} OK")

# ---- 4. Decimal money end to end ----
html = c.get("/admin/projects/new").get_data(as_text=True)
tk = get_csrf(html)
r = c.post("/admin/projects/new", data={
    "name": "Decimal Proj", "client_name": "C",
    "task_type": "translation", "pay_per_task": "0.10",
    "required_annotations": "1",
    "instructions": "i", "csrf_token": tk})
assert r.status_code == 302
with app.app_context():
    proj = db.session.scalar(db.select(Project).where(
        Project.name == "Decimal Proj"))
    assert isinstance(proj.pay_per_task, Decimal), type(proj.pay_per_task)
    assert proj.pay_per_task == Decimal("0.10")
    # three approved submissions x $0.10
    for i in range(3):
        t = Task(project_id=proj.id, input_data=f"d{i}", status="approved",
                 assigned_to=ann_a_id)
        db.session.add(t)
        db.session.flush()
        db.session.add(Submission(
            task_id=t.id, annotator_id=ann_a_id,
            output_data='{"kind":"translation","text":"x"}',
            status="approved", qa_score=90.0, submitted_at=utc_now(),
            reviewed_at=utc_now()))
    db.session.commit()
html = c.get("/admin/payments").get_data(as_text=True)
tk = get_csrf(html)
assert "$0.30" in html  # exact cent total, no float drift
assert "&#8358;480.00" in html  # 0.30 x 1600 as Decimal
r = c.post("/admin/payments/generate", data={"csrf_token": tk})
with app.app_context():
    pay = db.session.scalar(db.select(Payment))
    assert isinstance(pay.amount, Decimal)
    assert pay.amount == Decimal("0.30"), pay.amount
assert isinstance(
    app.extensions["sqlalchemy"], object)
from routes import usd_to_ngn
with app.app_context():
    ngn = usd_to_ngn(Decimal("0.10"))
    assert isinstance(ngn, Decimal) and ngn == Decimal("160.00"), ngn
    assert money(0.1) == Decimal("0.10")  # float artefact killed by repr
print("4. Decimal money: Numeric columns, exact 3x$0.10=$0.30 payout,"
      " Decimal NGN conversion OK")

# ---- 5. concurrency: 30 parallel users, WAL + threaded pool ----
def burst(worker):
    client = app.test_client()
    outcomes = []
    for i in range(3):
        r = client.get("/annotator/" if worker % 2 else "/admin/tasks",
                       follow_redirects=False)
        if r.status_code in (200, 302):
            outcomes.append(True)
        else:
            outcomes.append(r.status_code)
    return outcomes

with app.app_context():
    pass
login_client = app.test_client()
results = []
with ThreadPoolExecutor(max_workers=30) as pool:
    futures = [pool.submit(burst, w) for w in range(30)]
    for f in futures:
        results.extend(f.result())
bad = [r for r in results if r is not True]
assert not bad, f"concurrent failures: {bad}"
procfile = open(ROOT + "/Procfile").read()
# v14+: gunicorn runs 2 uvicorn workers against asgi:app; Flask
# handlers are dispatched to AnyIO's to-thread pool (raised to 60
# in asgi.py startup), replacing the old --threads 4 flag.
assert "uvicorn.workers.UvicornWorker" in procfile and "asgi:app" in procfile
assert "--workers 2" in procfile, procfile
render = open(ROOT + "/render.yaml").read()
assert "uvicorn.workers.UvicornWorker" in render and "asgi:app" in render
assert "--workers 2" in render
cfg = open(ROOT + "/config.py").read()
assert '"pool_size": 10' in cfg and '"max_overflow": 20' in cfg
asgi_src = open(ROOT + "/asgi.py").read()
assert "_THREAD_LIMIT = 60" in asgi_src, "AnyIO threadpool bumped to 60"
print("5. concurrency: 30 parallel clients x 3 requests all succeed;"
      " 2 uvicorn workers × 60 AnyIO threads sized OK")

# ---- 6. cross-platform paths ----
for fname in ("config.py", "backup.py"):
    src = open(ROOT + f"/{fname}").read()
    assert 'os.path.join' in src, fname
for fname in ("config.py", "backup.py", "app.py", "seed.py"):
    src = open(ROOT + f"/{fname}").read()
    assert not re.search(r'["=+] */home', src), fname
assert ".replace(os.sep" in open(
    ROOT + "/config.py").read()
print("6. paths: os.path.join everywhere, no POSIX-only separators OK")

# ---- 7. config-driven settings (no magic numbers in routes) ----
admin_src = open(ROOT + "/routes/admin.py").read()
assert not re.search(r"PER_PAGE = \d+", admin_src), "literal page sizes"
assert "UPLOAD_BATCH_LIMIT = 500" not in admin_src
auth_src = open(ROOT + "/routes/auth.py").read()
assert "timedelta(minutes=2)" not in auth_src
assert "LOGIN_MAX_FAILURES" in auth_src
from config import (
    LOGIN_LOCKOUT_BASE_SECONDS,
    LOGIN_LOCKOUT_MAX_SECONDS,
    LOGIN_MAX_FAILURES,
    MAX_QA_NOTES_CHARS,
    UPLOAD_BATCH_LIMIT,
)
assert (LOGIN_MAX_FAILURES, LOGIN_LOCKOUT_BASE_SECONDS,
        LOGIN_LOCKOUT_MAX_SECONDS) == (5, 120, 900)
assert MAX_QA_NOTES_CHARS == 2000 and UPLOAD_BATCH_LIMIT == 500
print("7. config-driven: pagination/batch/notes/login-throttle/FX all in"
      " config.py OK")

print("\nALL SCALABILITY TESTS PASSED")
