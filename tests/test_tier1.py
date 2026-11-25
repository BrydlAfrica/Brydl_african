import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Tier 1 suite: self-claim, notifications, deadlines, quality report.

Covers: race-safe "Claim next task" (barrier test — no task can be
double-claimed), the daily claim cap, paused-project refusal, the
notification lifecycle (assigned -> rejected -> payout paid, unread
badge, mark-read-on-view), project deadlines end to end, and the
admin quality dashboard's numbers.
"""
import io
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import create_app
from extensions import db
from models import (
    AuditLog,
    Notification,
    Payment,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)

app = create_app()

FAILURES = []


def expect(condition, label):
    """Record one granular pass/fail check."""
    if condition:
        print(f"   ok: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}")


def get_csrf(html):
    """Pull the CSRF token out of a rendered form page."""
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    """POST the sign-in form."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


def flash_of(response, client):
    """Follow a redirect and return the flashed segment of the page."""
    return client.get(response.headers["Location"]).get_data(as_text=True)


def csrf_of(client, path="/admin/tasks"):
    """A fresh CSRF token for admin POSTs."""
    return get_csrf(client.get(path).get_data(as_text=True))


# Like every suite: the RUNNER wipes instance/ + backups/ before the
# process starts. Deleting the directory AFTER create_app() strands
# every new pooled SQLite connection (CANTOPEN) — the exact trap the
# stress suite documented. Cleanup happens at the END only.

# ---- seed ----
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    db.session.add(admin)
    db.session.flush()
    anns = []
    for i in range(12):
        u = User(name=f"Ann {i}", email=f"ann{i}@brydl.ng",
                 role="annotator", terms_version="2.1")
        u.set_password("annot-pass-1")
        anns.append(u)
    db.session.add_all(anns)
    db.session.flush()
    project = Project(name="Tier1 project", client_name="Client",
                      task_type="classification",
                      pay_per_task=Decimal("0.05"),
                      instructions="Label it.", created_by=admin.id,
                      deadline=None)
    db.session.add(project)
    db.session.flush()
    pending = []
    for i in range(12):
        t = Task(project_id=project.id, input_data=f"pending text {i}")
        pending.append(t)
    db.session.add_all(pending)
    db.session.commit()
    for a in anns: a.record_consent()
    db.session.commit()
    project_id = project.id
    ann_ids = [u.id for u in anns]
    pending_ids = [t.id for t in pending]

admin_c = app.test_client()
login(admin_c, "admin@brydl.ng", "admin-pass-1")
ann_c = app.test_client()
login(ann_c, "ann0@brydl.ng", "annot-pass-1")

# ---- 1. claim: empty pool refusal, CSRF, happy path, order --------------
print("== 1. claim ==")
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))})
expect(r.status_code == 302, "claim POST redirects (PRG)")
with app.app_context():
    n_assigned = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.status == "assigned"))
expect(n_assigned == 1, "first claim assigned exactly one task")
with app.app_context():
    first_owner = db.session.scalar(db.select(Task.assigned_to).where(
        Task.id == pending_ids[0]))
    claimed_stamp = db.session.scalar(db.select(Task.claimed_at).where(
        Task.id == pending_ids[0]))
expect(first_owner == ann_ids[0] and claimed_stamp is not None,
       "oldest pending task claimed with a claimed_at stamp")
with app.app_context():
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "task.claim")) is not None
expect(audit_ok, "task.claim audited")
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))})
with app.app_context():
    second_owner = db.session.scalar(db.select(Task.assigned_to).where(
        Task.id == pending_ids[1]))
expect(second_owner == ann_ids[0],
       "second claim takes the next-oldest task")
r = ann_c.post("/annotator/claim", data={"csrf_token": "wrong"})
expect("Security check failed" in flash_of(r, ann_c),
       "claim without valid CSRF refused")

# ---- 2. daily cap --------------------------------------------------------
print("== 2. daily claim cap ==")
app.config["CLAIM_DAILY_CAP"] = 2
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))})
expect("daily limit" in flash_of(r, ann_c), "cap reached: friendly stop")
with app.app_context():
    n_assigned = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.status == "assigned"))
expect(n_assigned == 2, "cap enforced: no third assignment")
app.config["CLAIM_DAILY_CAP"] = 20

# ---- 3. race: 10 annotators claim simultaneously -------------------------
print("== 3. claim race (10 simultaneous annotators) ==")
clients = []
for i in range(1, 10):
    c = app.test_client()
    login(c, f"ann{i}@brydl.ng", "annot-pass-1")
    clients.append(c)


def race_claim(client):
    """One barrier-released claim POST."""
    barrier.wait()
    page = client.get("/annotator/").get_data(as_text=True)
    return client.post("/annotator/claim",
                       data={"csrf_token": get_csrf(page)})


barrier = threading.Barrier(len(clients))
with ThreadPoolExecutor(max_workers=len(clients)) as pool:
    list(pool.map(race_claim, clients))
with app.app_context():
    race_owners = db.session.scalars(db.select(Task.assigned_to).where(
        Task.id.in_(pending_ids[2:11]))).all()
    n_assigned = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.status == "assigned"))
    n_pending = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.status == "pending"))
expect(n_assigned == 11 and n_pending == 1
       and len(race_owners) == 9
       and len(set(race_owners)) == 9,
       f"9 racers each won a DISTINCT task ({n_assigned} assigned of"
       f" 12, {n_pending} left pending, race owners all distinct)")

# ---- 4. paused project stops claiming -----------------------------------
print("== 4. paused project ==")
with app.app_context():
    p = db.session.get(Project, project_id)
    p.status = "paused"
    db.session.commit()
r = ann_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(ann_c.get("/annotator/").get_data(
        as_text=True))})
expect("No tasks are available" in flash_of(r, ann_c),
       "paused project: pool refuses claims")
with app.app_context():
    p = db.session.get(Project, project_id)
    p.status = "active"
    db.session.commit()

# ---- 5. notifications lifecycle -----------------------------------------
print("== 5. notifications ==")
# admin assigns 2 fresh pending tasks to ann3 via the real route
with app.app_context():
    ann3 = db.session.scalar(db.select(User.id).where(
        User.email == "ann3@brydl.ng"))
    fresh = []
    for i in range(2):
        t = Task(project_id=project_id,
                 input_data=f"fresh assign target {i}")
        fresh.append(t)
    db.session.add_all(fresh)
    db.session.commit()
    fresh_ids = [t.id for t in fresh]
r = admin_c.post("/admin/tasks/assign", data={
    "annotator_id": str(ann3),
    "task_ids": [str(fresh_ids[0]), str(fresh_ids[1])],
    "csrf_token": csrf_of(admin_c)}, follow_redirects=True)
body = r.get_data(as_text=True)
expect("assigned" in body.lower() and "2" in body,
       "admin assignment succeeds (2 tasks)")
with app.app_context():
    kinds = db.session.scalars(db.select(Notification.kind).where(
        Notification.user_id == ann3)).all()
expect(kinds == ["task.assigned"], "task.assigned notification created")
# ann3 submits -> admin rejects via bulk route -> notification
with app.app_context():
    assigned_task = db.session.scalar(db.select(Task.id).where(
        Task.assigned_to == ann3, Task.status == "assigned"))
ann3_c = app.test_client()
login(ann3_c, "ann3@brydl.ng", "annot-pass-1")
r = ann3_c.post(f"/annotator/task/{assigned_task}", data={
    "label_choice": "positive",
    "csrf_token": get_csrf(ann3_c.get(
        f"/annotator/task/{assigned_task}").get_data(as_text=True))})
with app.app_context():
    sub_id = db.session.scalar(db.select(Submission.id).where(
        Submission.task_id == assigned_task))
r = admin_c.post("/admin/review/bulk", data={
    "submission_ids": [str(sub_id)], "action": "reject",
    "qa_score": "40", "csrf_token": csrf_of(admin_c, "/admin/review")},
    follow_redirects=True)
with app.app_context():
    kinds = db.session.scalars(db.select(Notification.kind).where(
        Notification.user_id == ann3).order_by(Notification.id)).all()
expect(kinds == ["task.assigned", "task.rejected"],
       "task.rejected notification created")
# badge shows 2 unread, then notifications page marks them read
dash = ann3_c.get("/annotator/").get_data(as_text=True)
expect("/annotator/notifications" in dash,
       "bell link present in the annotator navbar")
with app.app_context():
    unread = db.session.scalar(db.select(db.func.count(
        Notification.id)).where(
        Notification.user_id == ann3,
        Notification.read_at.is_(None)))
expect(unread == 2, "unread badge count is 2")
feed = ann3_c.get("/annotator/notifications").get_data(as_text=True)
expect("Revision requested" in feed and "New work assigned" in feed,
       "feed renders both notifications")
with app.app_context():
    unread = db.session.scalar(db.select(db.func.count(
        Notification.id)).where(
        Notification.user_id == ann3,
        Notification.read_at.is_(None)))
expect(unread == 0, "feed view marks everything read")
# terms-gated annotator cannot open the feed before acceptance
with app.app_context():
    gated = User(name="Gate Me", email="gate@brydl.ng",
                 role="annotator", terms_version="")
    gated.set_password("annot-pass-1")
    db.session.add(gated)
    db.session.commit()
    gated_id = gated.id
gc = app.test_client()
login(gc, "gate@brydl.ng", "annot-pass-1")
r = gc.get("/annotator/notifications")
expect(r.status_code == 302 and "/annotator/terms" in r.headers[
    "Location"], "feed stays terms-gated for new annotators")

# ---- 6. payout paid -> notification --------------------------------------
print("== 6. payout notification ==")
with app.app_context():
    pay = Payment(annotator_id=ann3, amount=Decimal("0.10"),
                  period_start=utc_now(), period_end=utc_now(),
                  tasks_count=1, status="pending")
    db.session.add(pay)
    db.session.commit()
    pay_id = pay.id
r = admin_c.post(f"/admin/payments/{pay_id}/mark-paid", data={
    "payment_method": "bank_transfer",
    "csrf_token": csrf_of(admin_c, "/admin/payments")},
    follow_redirects=True)
expect("paid" in r.get_data(as_text=True).lower(),
       "mark-paid route succeeds")
with app.app_context():
    kinds = db.session.scalars(db.select(Notification.kind).where(
        Notification.user_id == ann3).order_by(Notification.id)).all()
expect(kinds[-1] == "payout.paid", "payout.paid notification created")

# ---- 7. deadlines --------------------------------------------------------
print("== 7. project deadlines ==")
r = admin_c.post("/admin/projects/new", data={
    "name": "Dated project", "client_name": "Client",
    "task_type": "classification", "pay_per_task": "0.05",
    "instructions": "Label it.", "deadline": "2026-12-31",
    "required_annotations": "1",
    "csrf_token": csrf_of(admin_c, "/admin/projects/new")},
    follow_redirects=True)
projects_page = admin_c.get("/admin/projects").get_data(as_text=True)
expect("31 Dec 2026" in projects_page, "deadline shows on projects page")
with app.app_context():
    dated = db.session.scalar(db.select(Project.id).where(
        Project.name == "Dated project"))
    t = Task(project_id=dated, input_data="dated task",
             status="assigned", assigned_to=ann3)
    db.session.add(t)
    db.session.commit()
dash = ann3_c.get("/annotator/").get_data(as_text=True)
expect("Due 31 Dec 2026" in dash, "annotator queue shows the due date")

# ---- 8. quality dashboard ------------------------------------------------
print("== 8. quality dashboard ==")
with app.app_context():
    gold_t = Task(project_id=project_id, input_data="gold task",
                  expected_output="positive", status="assigned",
                  assigned_to=ann3)
    db.session.add(gold_t)
    db.session.flush()
    db.session.add(Submission(
        task_id=gold_t.id, annotator_id=ann3,
        output_data='{"kind": "classification", "label": "positive"}',
        status="approved", qa_score=90, submitted_at=utc_now(),
        reviewed_at=utc_now()))
    db.session.add(Submission(
        task_id=gold_t.id, annotator_id=ann3,
        output_data='{"kind": "classification", "label": "negative"}',
        status="rejected", qa_score=30, submitted_at=utc_now(),
        reviewed_at=utc_now()))
    db.session.commit()
page = admin_c.get("/admin/quality").get_data(as_text=True)
expect("Annotator quality" in page, "quality page renders")
# ann3 at this point: 1 approved (qa 90) + 2 rejected -> 33% rate,
# avg QA 90.0 (approved only), gold 1/2 -> 50%
expect("Ann 3" in page and "33.0" in page,
       "approval rate computed (1 approved / 3 reviewed -> 33%)")
expect("90.0" in page, "avg QA score shown (approved only -> 90.0)")
expect("50.0" in page, "gold accuracy shown (1/2 -> 50%)")
r = ann3_c.get("/admin/quality")
expect(r.status_code == 403, "quality page is admin-only")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"TIER 1 TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL TIER 1 TESTS PASSED")
