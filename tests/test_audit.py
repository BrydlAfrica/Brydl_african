import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Audit trail + structured logging tests."""
import glob
import json as jsonlib
import logging
import re

from app import create_app
from audit import get_request_id
from extensions import db
from models import User, Project, Task, Submission, AuditLog

app = create_app()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        self.records.append(record)


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
    ann_a_id = ann_a.id

# ---- 1. auth events ----
anon = app.test_client()
r = anon.get("/login")
tk = get_csrf(r.get_data(as_text=True))
r = anon.post("/login", data={"email": "a@brydl.ng", "password": "wrong",
                              "csrf_token": tk})
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.login.failed").order_by(
        AuditLog.id.desc()))
    assert row is not None and row.user_id is None
    assert row.actor_name == "a@brydl.ng"
    assert jsonlib.loads(row.detail)["reason"] == "bad_password"
    assert row.ip_address == "127.0.0.1", row.ip_address
r = login(anon, "admin@brydl.ng", "admin-pass-1")
assert r.status_code == 302
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.login.success").order_by(AuditLog.id.desc()))
    assert row is not None and row.user_id is not None
    assert row.actor_name == "Ada Admin"
c = app.test_client()
login(c, "admin@brydl.ng", "admin-pass-1")
html = c.get("/admin/").get_data(as_text=True)
tk = get_csrf(html)
c.post("/logout", data={"csrf_token": tk})
with app.app_context():
    assert db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.logout")) is not None
login(c, "admin@brydl.ng", "admin-pass-1")
print("1. auth audits: failed (reason, email label, ip), success, logout OK")

# ---- 2. the full workflow leaves one audit row per action ----
html = c.get("/admin/projects/new").get_data(as_text=True)
tk = get_csrf(html)
r = c.post("/admin/projects/new", data={
    "name": "Audit Proj", "client_name": "ACME", "task_type": "classification",
    "required_annotations": "1",
    "pay_per_task": "0.50", "instructions": "i", "csrf_token": tk})
assert r.status_code == 302
with app.app_context():
    proj = db.session.scalar(db.select(Project).where(
        Project.name == "Audit Proj"))
    proj_id = proj.id
html = c.get("/admin/tasks/upload").headers  # bare path -> redirect
page = c.get("/admin/tasks?upload_open=1").get_data(as_text=True)
tk = get_csrf(page)
r = c.post("/admin/tasks/upload", data={
    "project": str(proj_id), "tasks_data": "t1\nt2", "csrf_token": tk})
assert r.status_code == 302
with app.app_context():
    tasks = db.session.scalars(db.select(Task).where(
        Task.project_id == proj_id).order_by(Task.id)).all()
    t1, t2 = tasks[0].id, tasks[1].id
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "task.upload").order_by(AuditLog.id.desc()))
    assert jsonlib.loads(row.detail)["count"] == 2
    assert row.entity_type == "project" and row.entity_id == proj_id
page = c.get("/admin/tasks").get_data(as_text=True)
tk = get_csrf(page)
r = c.post("/admin/tasks/assign", data={
    "task_ids": [str(t1), str(t2)], "annotator_id": str(ann_a_id),
    "csrf_token": tk})
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "task.assign").order_by(AuditLog.id.desc()))
    d = jsonlib.loads(row.detail)
    assert d["count"] == 2 and d["assignee"] == "Tunde A"
    assert sorted(d["task_ids"]) == sorted([t1, t2])
r = c.post(f"/admin/tasks/{t2}/unassign", data={"csrf_token": tk})
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "task.unassign").order_by(AuditLog.id.desc()))
    assert row.entity_id == t2
    assert jsonlib.loads(row.detail)["from_status"] == "assigned"
ra = app.test_client()
login(ra, "a@brydl.ng", "annotator-pass-1")
page = ra.get(f"/annotator/task/{t1}").get_data(as_text=True)
tk2 = get_csrf(page)
ra.post(f"/annotator/task/{t1}", data={"label_choice": "positive",
                                       "csrf_token": tk2})
with app.app_context():
    sub = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t1))
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "submission.create").order_by(AuditLog.id.desc()))
    assert row.user_id == ann_a_id and row.entity_id == t1
    d = jsonlib.loads(row.detail)
    assert d["kind"] == "classification" and d["revision"] is False
page = c.get(f"/admin/review/{sub.id}").get_data(as_text=True)
tk3 = get_csrf(page)
c.post(f"/admin/review/{sub.id}", data={
    "action": "approve", "qa_score": "90", "csrf_token": tk3})
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "review.verdict").order_by(AuditLog.id.desc()))
    d = jsonlib.loads(row.detail)
    assert row.entity_id == sub.id and d["action"] == "approve"
    assert d["qa_score"] == 90.0 or d["qa_score"] == 90
page = c.get("/admin/payments").get_data(as_text=True)
tk4 = get_csrf(page)
c.post("/admin/payments/generate", data={"csrf_token": tk4})
with app.app_context():
    from models import Payment
    pay = db.session.scalar(db.select(Payment))
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "payment.generate").order_by(AuditLog.id.desc()))
    assert row.entity_id == pay.id
    assert float(jsonlib.loads(row.detail)["amount_usd"]) == 0.5
c.post(f"/admin/payments/{pay.id}/mark-paid",
       data={"payment_method": "bank_transfer", "csrf_token": tk4})
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "payment.mark_paid").order_by(AuditLog.id.desc()))
    d = jsonlib.loads(row.detail)
    assert d["payment_method"] == "bank_transfer"
    assert row.user_id is not None
print("2. workflow events: create/pause? upload/assign/unassign/submit/"
      "verdict/generate/mark-paid all recorded OK")

# ---- 3. pause + resume events ----
r = c.post(f"/admin/projects/{proj_id}/pause", data={"csrf_token": tk4})
with app.app_context():
    assert db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "project.pause")) is not None
c.post(f"/admin/projects/{proj_id}/pause", data={"csrf_token": tk4})
with app.app_context():
    assert db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "project.resume")) is not None
print("3. pause/resume audited OK")

# ---- 4. /admin/audit page: RBAC + filters ----
r = app.test_client().get("/admin/audit")
assert r.status_code == 302 and "/login" in r.headers["Location"]
r = ra.get("/admin/audit")
assert r.status_code == 403
page = c.get("/admin/audit").get_data(as_text=True)
assert "Audit trail" in page and "review.verdict" in page
page = c.get("/admin/audit?q=payment").get_data(as_text=True)
assert "payment.generate" in page and "task.upload" not in page
page = c.get(f"/admin/audit?actor={ann_a_id}").get_data(as_text=True)
assert "submission.create" in page and "project.create" not in page
page = c.get("/admin/audit?q=%21%5F").get_data(as_text=True)
assert "No audit events match" in page
page = c.get("/admin/audit?actor=99999").get_data(as_text=True)
assert "No audit events match" in page
print("4. audit page: RBAC, action + actor filters, junk params safe OK")

# ---- 5. structured logging: correlated request ids ----
capture = _Capture()
app.logger.addHandler(capture)
try:
    fresh = app.test_client()
    page = fresh.get("/login").get_data(as_text=True)
    tk5 = get_csrf(page)
    resp = fresh.post("/login", data={
        "email": "a@brydl.ng", "password": "annotator-pass-1",
        "csrf_token": tk5})
    assert resp.headers.get("X-Request-ID")
    msgs = [rec.getMessage() for rec in capture.records]
    assert any(m.startswith("AUDIT auth.login.success") for m in msgs), msgs[-5:]
    req_lines = [m for m in msgs if m.startswith("REQ ")]
    assert req_lines and any("POST /login -> 302" in m for m in req_lines)
    audit_recs = [rec for rec in capture.records
                  if rec.getMessage().startswith("AUDIT")]
    assert audit_recs and all(
        getattr(rec, "request_id", "-") != "-" for rec in audit_recs)
    req_recs = [rec for rec in capture.records
                if rec.getMessage().startswith("REQ ")]
    assert all(getattr(rec, "request_id", "-") != "-" for rec in req_recs)
finally:
    app.logger.removeHandler(capture)
print("5. logging: AUDIT + REQ lines share non-empty request ids,"
      " X-Request-ID echoed OK")

# ---- 6. escalating lockout is audited ----
lc = app.test_client()  # one client: CSRF is session-bound
for i in range(5):
    r = lc.get("/login")
    tk6 = get_csrf(r.get_data(as_text=True))
    lc.post("/login", data={
        "email": "lockme@brydl.ng", "password": "nope", "csrf_token": tk6})
with app.app_context():
    fails = db.session.scalars(db.select(AuditLog).where(
        AuditLog.action == "auth.login.failed",
        AuditLog.actor_name == "lockme@brydl.ng").order_by(
        AuditLog.id)).all()
    assert len(fails) == 5
    last = jsonlib.loads(fails[-1].detail)
    assert last["reason"] == "unknown_email" and "lockout_minutes" in last
r = lc.get("/login")
tk7 = get_csrf(r.get_data(as_text=True))
lc.post("/login", data={
    "email": "lockme@brydl.ng", "password": "nope", "csrf_token": tk7})
with app.app_context():
    locked = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.login.locked_out"))
    assert locked is not None
    assert jsonlib.loads(locked.detail)["minutes_left"] > 0
print("6. lockout: 5 failures + locked_out event audited OK")

# ---- 7. robustness: hostile detail payloads never break writes ----
with app.app_context():
    from audit import log_event
    log_event("test.objects", detail={"obj": object(), "n": 1},
              commit=True)
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "test.objects"))
    assert row is not None and "object at" in row.detail
    log_event("test.plain", commit=True)
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "test.plain"))
    assert row.detail is None and row.actor_name == "anonymous"
print("7. robustness: unserialisable detail + no detail handled OK")

# ---- 8. frontend polish assets ----
for template in glob.glob(ROOT + "/templates/**/*.html",
                          recursive=True):
    src = open(template).read()
    assert not re.search(r"\son[a-z]+=\"", src), template
base = open(ROOT + "/templates/base.html").read()
assert 'id="brydl-progress"' in base and '/admin/audit' in base
assert 'name="description"' in base
css = open(ROOT + "/static/css/style.css").read()
assert "brydl-slide" in css and ".badge.bg-success" in css
assert ".char-counter" in css and 'data-loading="true"' in css
js = open(ROOT + "/static/js/main.js").read()
assert "textarea[maxlength]" in js and "requestSubmit" in js
assert "beforeunload" in js and "bootstrap.Alert" in js
task = open(ROOT + "/templates/annotator/task.html").read()
assert "<kbd>Ctrl</kbd>" in task
print("8. frontend assets: progress bar, counters, tooltips wiring,"
      " CSP hygiene intact OK")

print("\nALL AUDIT TESTS PASSED")
