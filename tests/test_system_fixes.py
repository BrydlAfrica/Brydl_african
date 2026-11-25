import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""System-fixes suite: bank encryption, revision cap, COCO/YOLO export.

1. Bank numbers are Fernet ciphertext in the database, last4 powers
   the masked UI, the payout CSV decrypts, legacy plaintext rows keep
   working, and removal clears everything.
2. A task rejected TASK_MAX_REJECTIONS times auto-returns to the
   pending pool; the annotator is notified with task.escalated and the
   admin flash/audit report it.
3. Project export speaks COCO JSON (valid schema, categories built
   from labels, gold never included) and YOLO text (ZIP: classes.txt,
   per-task files, manifest).
"""
import io
import json
import re
import shutil
import sys
import zipfile
from decimal import Decimal

from app import create_app
from extensions import db
from models import (
    AuditLog,
    Notification,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)
from security_crypto import decrypt_bank_number

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


def admin_csrf(client, path="/admin/tasks"):
    """A fresh CSRF token for admin POSTs."""
    return get_csrf(client.get(path).get_data(as_text=True))


shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ---- seed ----
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    db.session.add(admin)
    db.session.flush()
    ann = User(name="Tunde A", email="a@brydl.ng", role="annotator",
               terms_version="2.1")
    ann.set_password("annot-pass-1")
    db.session.add(ann)
    db.session.flush()
    project = Project(name="Fixes project", client_name="Client",
                      task_type="classification",
                      pay_per_task=Decimal("0.05"),
                      instructions="Label it.", created_by=admin.id)
    db.session.add(project)
    db.session.flush()
    db.session.commit()
    ann.record_consent()
    db.session.commit()
    project_id, ann_id = project.id, ann.id
    ann_email = ann.email

admin_c = app.test_client()
login(admin_c, "admin@brydl.ng", "admin-pass-1")
ann_c = app.test_client()
login(ann_c, "a@brydl.ng", "annot-pass-1")

# ---- 1. bank encryption ---------------------------------------------------
print("== 1. bank number encryption ==")
r = ann_c.post("/annotator/profile/bank-details", data={
    "bank_name": "GTBank", "account_type": "savings",
    "account_number": "0123456789", "account_name": "Tunde Adewale A",
    "save_bank": "y",
    "csrf_token": get_csrf(ann_c.get("/annotator/profile").get_data(
        as_text=True))}, follow_redirects=True)
expect("Payout account saved" in r.get_data(as_text=True),
       "bank save succeeds")
with app.app_context():
    row = db.session.get(User, ann_id)
    stored = row.bank_account_number
expect(stored != "0123456789" and "0123456789" not in str(stored),
       "DB column holds ciphertext, not the NUBAN")
with app.app_context():
    row = db.session.get(User, ann_id)
expect(row.bank_account_last4 == "6789"
       and row.get_bank_account_number() == "0123456789",
       "last4 stored; getter decrypts to the exact number")
with app.app_context():
    raw_plain = db.session.scalar(db.select(User.bank_account_number).where(
        User.id == ann_id))
expect(decrypt_bank_number(raw_plain) == "0123456789",
       "security_crypto round-trips the stored token")
users_html = admin_c.get("/admin/users").get_data(as_text=True)
expect("****6789" in users_html and "0123456789" not in users_html,
       "Team page shows masked last4 only")
# payout CSV decrypts the number
r = admin_c.post("/admin/payments/bank-batch", data={
    "csrf_token": admin_csrf(admin_c, "/admin/payments")},
    follow_redirects=True)
# no pending payouts yet -> friendly info; craft one directly instead
with app.app_context():
    from models import Payment
    pay = Payment(annotator_id=ann_id, amount=Decimal("0.05"),
                  period_start=utc_now(), period_end=utc_now(),
                  tasks_count=1, status="pending")
    db.session.add(pay)
    db.session.commit()
r = admin_c.post("/admin/payments/bank-batch", data={
    "csrf_token": admin_csrf(admin_c, "/admin/payments")})
csv_text = r.get_data(as_text=True)
expect("0123456789" in csv_text, "payout CSV carries the DECRYPTED NUBAN")
# legacy plaintext keeps working
with app.app_context():
    legacy = User(
        name="Legacy", email="legacy@brydl.ng",
        role="annotator", terms_version="2.1",
        bank_account_number="0987654321",
        bank_account_last4="4321",
    )
    legacy.set_password("annot-pass-1")
    db.session.add(legacy)
    db.session.commit()
    legacy_id = legacy.id
with app.app_context():
    row = db.session.get(User, legacy_id)
expect(row.get_bank_account_number() == "0987654321",
       "legacy plaintext row still reads correctly")
r = ann_c.post("/annotator/profile/bank-details", data={
    "bank_name": "GTBank", "account_type": "savings",
    "account_number": "0123456789", "account_name": "Tunde Adewale A",
    "remove_bank": "y",
    "csrf_token": get_csrf(ann_c.get("/annotator/profile").get_data(
        as_text=True))}, follow_redirects=True)
with app.app_context():
    row = db.session.get(User, ann_id)
expect(row.bank_account_number is None
       and row.bank_account_last4 is None,
       "removal clears ciphertext + last4")

# ---- 2. revision loop cap -------------------------------------------------
print("== 2. revision loop cap ==")
app.config["TASK_MAX_REJECTIONS"] = 3
with app.app_context():
    t = Task(project_id=project_id, input_data="capped task",
             status="assigned", assigned_to=ann_id)
    db.session.add(t)
    db.session.flush()
    capped_task_id = t.id
    db.session.commit()

ann_c2 = app.test_client()
login(ann_c2, "a@brydl.ng", "annot-pass-1")


def submit_and_reject(task_id, round_no):
    """One revision round: annotator submits, admin rejects (keep)."""
    page = ann_c2.get(f"/annotator/task/{task_id}").get_data(
        as_text=True)
    ann_c2.post(f"/annotator/task/{task_id}", data={
        "label_choice": "negative", "csrf_token": get_csrf(page)})
    with app.app_context():
        sub_id = db.session.scalar(
            db.select(Submission.id).where(
                Submission.task_id == task_id,
                Submission.status == "submitted",
            )
        )
    r = admin_c.post(
        "/admin/review/bulk",
        data={
            "submission_ids": [str(sub_id)],
            "action": "reject",
            "qa_score": "30",
            "return_for_revision": "y",
            "csrf_token": admin_csrf(admin_c, "/admin/review"),
        },
        follow_redirects=True,
    )
    return r.get_data(as_text=True)


for round_no in (1, 2):
    body = submit_and_reject(capped_task_id, round_no)
    with app.app_context():
        status = db.session.scalar(db.select(Task.status).where(
            Task.id == capped_task_id))
    expect(status == "rejected",
           f"round {round_no}: task stays with the annotator")

body = submit_and_reject(capped_task_id, 3)
expect("hit the 3-rejection limit" in body or
       "rejection limit" in body,
       "third rejection flashes the escalation")
with app.app_context():
    status = db.session.scalar(db.select(Task.status).where(
        Task.id == capped_task_id))
    owner = db.session.scalar(db.select(Task.assigned_to).where(
        Task.id == capped_task_id))
expect(status == "pending" and owner is None,
       "capped task returned to the pending pool, unowned")
with app.app_context():
    kinds = db.session.scalars(db.select(Notification.kind).where(
        Notification.user_id == ann_id).order_by(Notification.id)).all()
expect("task.escalated" in kinds,
       "task.escalated notification sent to the annotator")
with app.app_context():
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.detail.contains('"escalated": 1'))) is not None
expect(audit_ok, "escalation recorded in the audit detail")
# approve path unaffected
with app.app_context():
    t2 = Task(project_id=project_id, input_data="approve path",
              status="assigned", assigned_to=ann_id)
    db.session.add(t2)
    db.session.flush()
    approve_task_id = t2.id
    db.session.commit()
page = ann_c2.get(f"/annotator/task/{approve_task_id}").get_data(
    as_text=True)
ann_c2.post(f"/annotator/task/{approve_task_id}", data={
    "label_choice": "positive", "csrf_token": get_csrf(page)})
with app.app_context():
    sub_id = db.session.scalar(
        db.select(Submission.id).where(
            Submission.task_id == approve_task_id,
            Submission.status == "submitted",
        )
    )
admin_c.post("/admin/review/bulk", data={
    "submission_ids": [str(sub_id)], "action": "approve",
    "qa_score": "95", "csrf_token": admin_csrf(admin_c,
                                               "/admin/review")})
with app.app_context():
    status = db.session.scalar(db.select(Task.status).where(
        Task.id == approve_task_id))
expect(status == "approved", "approval path untouched by the cap")

# ---- 3. COCO + YOLO exports ----------------------------------------------
print("== 3. COCO + YOLO export ==")
r = admin_c.post(f"/admin/projects/{project_id}/export", data={
    "format": "coco", "scope": "reviewed",
    "csrf_token": admin_csrf(admin_c,
                             f"/admin/projects/{project_id}/export")})
coco = json.loads(r.get_data(as_text=True))
expect(all(k in coco for k in ("info", "images", "annotations",
                               "categories")),
       "COCO JSON has the required top-level keys")
expect(len(coco["images"]) >= 1 and len(coco["annotations"]) >= 1,
       f"images={len(coco['images'])} annotations="
       f"{len(coco['annotations'])}")
expect(all(coco["images"][0].get(k) is not None
           for k in ("id", "file_name", "width", "height")),
       "image entries carry the COCO-required fields")
labels = {c["name"] for c in coco["categories"]}
expect({"positive", "negative"} <= labels,
       f"categories built from submitted labels ({sorted(labels)})")
cat_ids = {c["id"] for c in coco["categories"]}
expect(all(a["category_id"] in cat_ids for a in coco["annotations"]),
       "annotation category_ids all resolve to categories")
expect(all(a["image_id"] in {i["id"] for i in coco["images"]}
           for a in coco["annotations"]),
       "annotation image_ids all resolve to images")
coco_text = r.get_data(as_text=True)
expect("gold" not in coco_text.lower(),
       "COCO export never carries gold answers")
r = admin_c.post(f"/admin/projects/{project_id}/export", data={
    "format": "yolo", "scope": "reviewed",
    "csrf_token": admin_csrf(admin_c,
                             f"/admin/projects/{project_id}/export")})
zf = zipfile.ZipFile(io.BytesIO(r.get_data()))
names = zf.namelist()
expect("classes.txt" in names and "README.txt" in names
       and "brydl-manifest.json" in names,
       f"ZIP contains classes.txt, README, manifest ({len(names)} files)")
classes = zf.read("classes.txt").decode().split()
expect(classes == sorted(classes) and set(classes) >= {"positive",
                                                       "negative"},
       "classes.txt is sorted and covers the labels")
task_files = [n for n in names if n.startswith("task_")]
expect(len(task_files) >= 1, f"{len(task_files)} per-task label files")
first_file = zf.read(task_files[0]).decode().strip().splitlines()[0]
parts = first_file.split()
expect(len(parts) == 5 and parts[0].isdigit(),
       "label lines are 5-column YOLO rows with a class index")
manifest = json.loads(zf.read("brydl-manifest.json"))
expect(manifest["tasks"] and "labels_file" in manifest["tasks"][0],
       "manifest maps task ids to label files")
r = admin_c.post(f"/admin/projects/{project_id}/export", data={
    "format": "csv", "scope": "approved",
    "csrf_token": admin_csrf(admin_c,
                             f"/admin/projects/{project_id}/export")})
expect(r.get_data(as_text=True).lstrip("\ufeff").startswith("task_id"),
       "CSV export unchanged")
r = admin_c.post(f"/admin/projects/{project_id}/export", data={
    "format": "bogus", "scope": "approved",
    "csrf_token": admin_csrf(admin_c,
                             f"/admin/projects/{project_id}/export")})
expect(r.get_data(as_text=True).startswith("{"),
       "bogus format falls back to the JSONL default")
with app.app_context():
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "project.export",
        AuditLog.detail.contains('"coco"'))) is not None
expect(audit_ok, "coco export audited")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"SYSTEM FIXES TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL SYSTEM FIXES TESTS PASSED")
