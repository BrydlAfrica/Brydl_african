"""Project Manager role, scoping, approval flow, feedback, site settings."""
from __future__ import annotations
from app import create_app
from models import (
    FeedbackReply, FeedbackThread, Notification, Project, ProjectAssignment,
    SiteSetting, Task, User,
)
from extensions import db

import os
import re
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["BRYDL_DISABLE_JOBS"] = "1"

FAILURES = []


def expect(cond: bool, msg: str):
    if not cond:
        FAILURES.append(msg)
        print("  FAIL:", msg)
    else:
        print("   ok:", msg)


app = create_app()
with app.app_context():
    db.drop_all()
    db.create_all()
    admin = User(name="Admin", email="admin@brydl.com", role="admin")
    admin.set_password("admin123")
    pm = User(name="PM Aisha", email="pm@brydl.com", role="project_manager", terms_version="2.1")
    pm.set_password("pm12345!")
    qa = User(name="QA Kemi", email="qa@brydl.com", role="qa", terms_version="2.1")
    qa.set_password("qa12345!")
    ann = User(name="Tunde A", email="ann@brydl.com", role="annotator", terms_version="2.1")
    ann.set_password("ann123!")
    other_proj = Project(name="Existing", client_name="OldCo", task_type="classification",
                         pay_per_task=0.25, instructions="x", created_by=admin.id,
                         approved_by=admin.id, approved_at=__import__("models").utc_now(),
                         status="active")
    db.session.add_all([admin, pm, qa, ann])
    db.session.commit()
    pm_id, admin_id, ann_id = pm.id, admin.id, ann.id
    other_proj = Project(name="Existing", client_name="OldCo", task_type="classification",
                         pay_per_task=0.25, instructions="x", created_by=admin_id,
                         approved_by=admin_id, approved_at=__import__("models").utc_now(),
                         status="active")
    db.session.add(other_proj)
    db.session.commit()
    other_id = other_proj.id


def csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(cl, email, pw):
    p = cl.get("/login").get_data(as_text=True)
    return cl.post("/login", data={"csrf_token": csrf(p), "email": email, "password": pw}, follow_redirects=True)


def gcsrf(cl, path):
    return csrf(cl.get(path).get_data(as_text=True))


admin_cl = app.test_client()
pm_cl = app.test_client()
ann_cl = app.test_client()
qa_cl = app.test_client()

print("== login flows ==")
expect(login(admin_cl, "admin@brydl.com", "admin123").status_code == 200, "admin login")
expect(login(pm_cl, "pm@brydl.com", "pm12345!").status_code == 200, "pm login")
expect(login(ann_cl, "ann@brydl.com", "ann123!").status_code == 200, "ann login")
expect(login(qa_cl, "qa@brydl.com", "qa12345!").status_code == 200, "qa login")

print("== role walls ==")
expect(pm_cl.get("/admin/", follow_redirects=False).status_code == 403, "pm blocked from /admin/")
expect(pm_cl.get("/admin/payments", follow_redirects=False).status_code == 403, "pm blocked from payouts")
expect(ann_cl.get("/pm/", follow_redirects=False).status_code == 403, "annotator blocked from /pm/")
expect(qa_cl.get("/pm/", follow_redirects=False).status_code == 403, "qa blocked from /pm/")

print("== PM proposal + approval flow ==")
r = pm_cl.get("/pm/projects/new")
expect(r.status_code == 200, "new project form")
r = pm_cl.post("/pm/projects/new", data={
    "csrf_token": csrf(r.get_data(as_text=True)),
    "name": "Pidgin v2", "client_name": "ClientX", "task_type": "translation",
    "proposed_pay_per_task": "0.08", "instructions": "Translate to Pidgin.",
    "required_annotations": 1,
}, follow_redirects=True)
expect(r.status_code == 200, "draft saved")
with app.app_context():
    p = db.session.scalars(db.select(Project).where(Project.name == "Pidgin v2")).first()
    expect(p is not None and p.status == "draft", "project draft created")
    proj_id = p.id
    expect(p.pay_per_task is None and p.proposed_pay_per_task is not None,
           "proposed_pay set, pay_per_task null in draft")
pm_cl.post(f"/pm/projects/{proj_id}/submit",
           data={"csrf_token": gcsrf(pm_cl, f"/pm/projects/{proj_id}")},
           follow_redirects=True)
with app.app_context():
    db.session.expire_all()
    expect(db.session.get(Project, proj_id).status == "pending_approval", "status pending_approval")
r = admin_cl.get("/admin/approvals")
expect(b"Pidgin v2" in r.data, "admin sees pending project")
admin_cl.post(f"/admin/approvals/{proj_id}/approve",
              data={"csrf_token": csrf(r.get_data(as_text=True)), "pay_per_task": "0.07"},
              follow_redirects=True)
with app.app_context():
    db.session.expire_all()
    p = db.session.get(Project, proj_id)
    expect(p.status == "active", "project live after approval")
    expect(float(p.pay_per_task) == 0.07, "pay rate locked at 0.07")
    expect(db.session.scalar(db.select(Notification).where(
        Notification.user_id == pm_id, Notification.kind == "project.approved")) is not None,
        "PM got approval notification")
    expect(db.session.scalar(db.select(ProjectAssignment).where(
        ProjectAssignment.project_id == proj_id, ProjectAssignment.user_id == pm_id))
        is not None, "PM auto-assigned to own project")

print("== PM dashboard renders with metrics tiles")
r = pm_cl.get("/pm/"); expect(r.status_code == 200, "PM dashboard 200")
expect(b"My projects" in r.data or b"My Projects" in r.data, "projects section present")
expect(b"Earned" in r.data, "earned tile present")
expect(b"Awaiting" in r.data or b"Pending approval" in r.data or b"pending" in r.data.lower(), "pending stat present")
r2 = pm_cl.get("/pm/?page=2"); expect(r2.status_code == 200, "dashboard page 2 renders")

print("== PM dashboard query count stays bounded with 8 projects")
from sqlalchemy import event
import models as M
with app.app_context():
    for i in range(7):
        prj = M.Project(
            name=f"Project {i}", client_name="Cx", task_type="classification",
            pay_per_task=0.05, instructions="x",
            created_by=pm_id, approved_by=pm_id,
            approved_at=M.utc_now(), status="active",
        )
        db.session.add(prj); db.session.flush()
        db.session.add(ProjectAssignment(
            project_id=prj.id, user_id=pm_id,
            role_in_project="project_manager", assigned_by=pm_id,
        ))
        for _ in range(5):
            db.session.add(M.Task(project_id=prj.id, input_data="x", status="pending"))
    db.session.commit()
qc = {"n": 0}
def _count(conn, cur, stmt, params, context, executemany): qc["n"] += 1
with app.app_context():
    event.listen(db.engine, "before_cursor_execute", _count)
r = pm_cl.get("/pm/")
with app.app_context():
    event.remove(db.engine, "before_cursor_execute", _count)
expect(r.status_code == 200, "dashboard renders with 8 projects")
expect(qc["n"] < 25, f"query count bounded ({qc['n']} queries)")

print("== PM uploads tasks ==")
r = pm_cl.post(f"/pm/projects/{proj_id}/upload", data={
    "csrf_token": gcsrf(pm_cl, f"/pm/projects/{proj_id}/upload"),
    "tasks_data": "Line one\nLine two\nLine three",
}, follow_redirects=True)
with app.app_context():
    cnt = db.session.scalar(db.select(db.func.count(Task.id)).where(Task.project_id == proj_id))
    expect(cnt == 3, f"3 tasks stored (got {cnt})")

print("== PM scoping ==")
expect(pm_cl.get(f"/pm/projects/{other_id}", follow_redirects=False).status_code in (403, 302),
       "PM blocked from unassigned project")
admin_cl.post(f"/admin/projects/{other_id}/assign-pm",
              data={"csrf_token": gcsrf(admin_cl, f"/admin/projects/{other_id}/export"),
                    "pm_id": pm_id}, follow_redirects=True)
expect(pm_cl.get(f"/pm/projects/{other_id}", follow_redirects=False).status_code == 200,
       "PM can access project after assignment")

print("== Site Settings (USD-NGN) ==")
r = admin_cl.get("/admin/settings")
expect(b"Naira" in r.data, "settings shows USD-NGN rate")
admin_cl.post("/admin/settings", data={
    "csrf_token": csrf(r.get_data(as_text=True)),
    "usd_to_ngn_rate": "1550", "site_name": "Brydl Dashboard",
    "support_email": "support@brydl.ng", "pagination_per_page": "20",
    "upload_batch_limit": "500", "claim_daily_cap": "20",
    "task_max_rejections": "3", "preference_min_justification": "40",
    "api_rate_limit_per_minute": "60",
}, follow_redirects=True)
with app.app_context():
    expect(db.session.get(SiteSetting, "usd_to_ngn_rate").value == "1550",
           "USD-NGN rate updated")

print("== Feedback + broadcasts ==")
r = pm_cl.get("/feedback")
pm_cl.post("/feedback", data={
    "csrf_token": csrf(r.get_data(as_text=True)),
    "subject": "Need annotators", "body": "Add more hands please.",
}, follow_redirects=True)
with app.app_context():
    th = db.session.scalars(db.select(FeedbackThread).where(
        FeedbackThread.subject == "Need annotators")).first()
    expect(th is not None and th.status == "new", "feedback thread created")
    tid = th.id
r = admin_cl.get("/feedback")
expect(b"Need annotators" in r.data, "admin inbox shows feedback")
admin_cl.post(f"/feedback/{tid}", data={
    "csrf_token": csrf(r.get_data(as_text=True)),
    "body": "Adding 3.",
}, follow_redirects=True)
with app.app_context():
    expect(db.session.scalar(db.select(FeedbackReply).where(
        FeedbackReply.thread_id == tid)) is not None, "admin reply recorded")
admin_cl.post("/admin/messages/broadcast", data={
    "csrf_token": gcsrf(admin_cl, "/feedback"),
    "subject": "Payouts", "body": "Delayed today.",
}, follow_redirects=True)
with app.app_context():
    for uid in (pm_id, ann_id):
        expect(db.session.scalar(db.select(Notification).where(
            Notification.user_id == uid, Notification.kind == "broadcast.new"))
            is not None, f"broadcast to user {uid}")

print("== Profile pages ==")
expect(pm_cl.get("/profile").status_code == 200, "pm profile")
expect(qa_cl.get("/profile").status_code == 200, "qa profile")
expect(ann_cl.get("/feedback").status_code == 200, "annotator feedback")

print()
if FAILURES:
    print(f"PM TESTS FAILED ({len(FAILURES)}):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL PM TESTS PASSED")
