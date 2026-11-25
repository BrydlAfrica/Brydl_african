"""Annotator experience tests: T&C gate, profile, theme, continue, stats."""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import AuditLog, ContributorConsent, Project, Submission, Task, User, utc_now  # noqa: E402
from legal_docs import TERMS_VERSION, compute_document_hash  # noqa: E402

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def flash_of(response, client):
    return client.get(response.headers["Location"]).get_data(as_text=True)


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


# ---- fresh database + dev seed (seeded annotators auto-accepted) -----
proc = subprocess.run([sys.executable, "seed.py"], cwd=ROOT,
                      capture_output=True, text=True, timeout=180)
expect(proc.returncode == 0, "seed.py exits 0")

app = create_app()
with app.app_context():
    # A gated annotator (what the Team page creates) + an empty-queue
    # annotator; seeded annotators keep the model-default acceptance.
    gated = User(name="Gada G", email="gated@brydl.com",
                 role="annotator", terms_version="")  # "" = must accept
    gated.set_password("gated-pass-1")
    empty = User(name="Empty E", email="empty@brydl.com",
                 role="annotator")
    empty.set_password("empty-pass-1")
    db.session.add_all([gated, empty])
    db.session.commit()
    gated_id, empty_id = gated.id, empty.id
    # Seeded acceptance record for empty
    doc_hash = compute_document_hash("terms_and_conditions", TERMS_VERSION)
    empty_consent = ContributorConsent(
        user_id=empty_id,
        document_type="terms_and_conditions",
        version=TERMS_VERSION,
        accepted_at=utc_now(),
        document_hash=doc_hash,
        ip_address="127.0.0.1",
        user_agent="TestDevSeed/1.0",
        consent_method="system_seed",
        is_active=True,
    )
    db.session.add(empty_consent)
    db.session.commit()
    ann1_id = db.session.scalar(db.select(User.id).where(
        User.email == "annotator1@brydl.com"))
    # give the gated annotator a task (assignment flips status, per
    # the ck_tasks_assignment invariant: pending ⇔ unassigned)
    free_task = db.session.scalars(db.select(Task).where(
        Task.assigned_to.is_(None), Task.status == "pending")).first()
    free_task.assigned_to = gated_id
    free_task.status = "assigned"
    db.session.commit()
    gated_task_id = free_task.id

adm = app.test_client()
login(adm, "admin@brydl.com", "admin123")
ann1 = app.test_client()
login(ann1, "annotator1@brydl.com", "annotator123")
gated_c = app.test_client()
login(gated_c, "gated@brydl.com", "gated-pass-1")
empty_c = app.test_client()
login(empty_c, "empty@brydl.com", "empty-pass-1")

# ---- terms gate ------------------------------------------------------
print("== Terms gate ==")
# A FRESH login of an unaccepted annotator lands on the terms page
# FIRST (Build 21: the gate is the visible first screen, not a wall
# you meet after wandering into the workspace).
fresh_gated = app.test_client()
r = login(fresh_gated, "gated@brydl.com", "gated-pass-1")
expect(r.status_code == 302 and "/annotator/terms" in r.headers[
    "Location"], "post-login redirect goes to terms BEFORE the app")
r = gated_c.get("/annotator/")
expect(r.status_code == 302 and "/annotator/terms" in r.headers["Location"],
       "unaccepted annotator is redirected to the terms page")
for path in (f"/annotator/task/{gated_task_id}", "/annotator/history"):
    r = gated_c.get(path)
    assert r.status_code == 302 and "/annotator/terms" in r.headers[
        "Location"], path
expect(True, "task pages + history stay gated until acceptance")
r = gated_c.post(f"/annotator/task/{gated_task_id}", data={
    "answer": "positive"})
with app.app_context():
    n_subs = db.session.scalar(db.select(db.func.count(Submission.id)))
expect(r.status_code == 302 and n_subs == 0,
       "gated submissions are refused (no row created)")

r = gated_c.get("/annotator/terms")
body = r.get_data(as_text=True)
expect(r.status_code == 200 and "Terms &amp; Conditions" in body
       and 'type="checkbox"' in body,
       "terms page shows the agreement with an accept form")
expect("Compensation" in body and "Confidentiality" in body and "Nigeria" in body,
       "terms cover pay, confidentiality and governing law")
r = gated_c.post("/annotator/terms/accept", data={})
expect(r.status_code == 302, "acceptance without CSRF is refused")
with app.app_context():
    expect(db.session.scalar(db.select(User.terms_version).where(
        User.id == gated_id)) == "", "no consent recorded without CSRF")

current_terms_v = app.config.get("TERMS_VERSION", "2.1")
page = gated_c.get("/annotator/terms").get_data(as_text=True)
r = gated_c.post("/annotator/terms/accept", data={
    "csrf_token": get_csrf(page)})
body = flash_of(r, gated_c)
expect("terms are accepted" in body, "acceptance flashes success")
with app.app_context():
    row = db.session.get(User, gated_id)
    expect(row.terms_version == current_terms_v
           and row.terms_accepted_at is not None,
           "consent (version + UTC timestamp) stored on the account")
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.terms_accept",
        AuditLog.entity_id == gated_id,
        AuditLog.detail.like(f"%{current_terms_v}%"))) is not None
expect(audit_ok, "user.terms_accept audited")

r = gated_c.get("/annotator/")
expect(r.status_code == 200, "workspace opens after acceptance")
r = gated_c.get("/annotator/terms")
terms_accepted_body = r.get_data(as_text=True)
expect(f"You accepted Version {current_terms_v}" in terms_accepted_body or
       f"You accepted version {current_terms_v}" in terms_accepted_body,
       "terms page reflects acceptance status")

# version bump re-prompts everyone
app.config["TERMS_VERSION"] = "99.0"
r = gated_c.get("/annotator/")
expect(r.status_code == 302 and "/annotator/terms" in r.headers["Location"],
       "bumping the version re-prompts previously accepted annotators")
r = ann1.get("/annotator/")
expect(r.status_code == 302, "bump re-prompts seeded annotators too")
app.config["TERMS_VERSION"] = current_terms_v

# ---- profile -----------------------------------------------------------
print("== Profile ==")
r = anon_probe = app.test_client().get("/annotator/profile")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anon: profile redirects to sign-in")
r = adm.get("/annotator/profile")
expect(r.status_code == 403, "admin: profile is 403")
page = ann1.get("/annotator/profile").get_data(as_text=True)
expect(page is not None and "My Profile" in page and "annotator1@brydl.com"
       in page and "Colour theme" in page,
       "profile shows identity, email (read-only) and theme")

tk = get_csrf(page)
r = ann1.post("/annotator/profile", data={
    "name": "Ngozi Okafor-Okon", "csrf_token": tk, "save_name": "y"})
expect("details have been updated" in flash_of(r, ann1),
       "details save flashes success")
with app.app_context():
    expect(db.session.scalar(db.select(User.name).where(
        User.id == ann1_id)) == "Ngozi Okafor-Okon",
        "name updated in DB")
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.profile_update",
        AuditLog.entity_id == ann1_id)) is not None
expect(audit_ok, "user.profile_update audited")

tk = get_csrf(ann1.get("/annotator/profile").get_data(as_text=True))
r = ann1.post("/annotator/profile", data={
    "name": "", "csrf_token": tk, "save_name": "y"})
expect(r.status_code == 200 and "Name is required"
       in r.get_data(as_text=True),
       "empty name refused inline (re-render with field error)")

# password change
page = ann1.get("/annotator/profile").get_data(as_text=True)
pw_data = {
    "current_password": "wrong-pass", "new_password":
    "brand-new-pass-9", "confirm_password": "brand-new-pass-9",
    "csrf_token": get_csrf(page), "save_password": "y",
}
r = ann1.post("/annotator/profile", data=pw_data)
expect("Current password is incorrect" in flash_of(r, ann1),
       "wrong current password refused")

page = ann1.get("/annotator/profile").get_data(as_text=True)
r = ann1.post("/annotator/profile", data={
    "current_password": "annotator123", "new_password": "short",
    "confirm_password": "short",
    "csrf_token": get_csrf(page), "save_password": "y"})
expect(r.status_code == 200 and "between 8 and 128"
       in r.get_data(as_text=True), "short password refused inline")

r = ann1.post("/annotator/profile", data={
    "current_password": "annotator123", "new_password": "brand-new-pass-9",
    "confirm_password": "different-pass",
    "csrf_token": get_csrf(ann1.get("/annotator/profile").get_data(
        as_text=True)), "save_password": "y"})
expect(r.status_code == 200 and "do not match"
       in r.get_data(as_text=True), "mismatched confirm refused inline")

# a second live session for the SAME account, to watch it die
other = app.test_client()
login(other, "annotator1@brydl.com", "annotator123")
r = ann1.post("/annotator/profile", data={
    "current_password": "annotator123", "new_password": "brand-new-pass-9",
    "confirm_password": "brand-new-pass-9",
    "csrf_token": get_csrf(ann1.get("/annotator/profile").get_data(
        as_text=True)), "save_password": "y"})
expect("Password changed" in flash_of(r, ann1), "password change accepted")
r = login(app.test_client(), "annotator1@brydl.com", "annotator123")
expect(r.status_code == 200 and "Invalid email or password"
       in r.get_data(as_text=True), "old password no longer works")
r = login(app.test_client(), "annotator1@brydl.com", "brand-new-pass-9")
expect(r.status_code == 302 and "/annotator" in r.headers.get(
    "Location", ""), "new password works")
with app.app_context():
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.password_change",
        AuditLog.entity_id == ann1_id)) is not None
expect(audit_ok, "user.password_change audited")

# credential-change revocation (auth_version): OTHER live sessions die
r = other.get("/annotator/")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "OTHER live sessions are revoked by a password change")
# ...and the session that made the change is signed out too
r = ann1.get("/annotator/")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "current session signs out after the change")
login(ann1, "annotator1@brydl.com", "brand-new-pass-9")
r = ann1.get("/annotator/")
expect(r.status_code == 200, "re-login with the new password restores work")

# ---- theme -------------------------------------------------------------
print("== Theme ==")
r = ann1.post("/account/theme", data={"theme": "blue"})
expect(r.status_code == 302, "theme change without CSRF refused")
r = ann1.post("/account/theme", data={
    "theme": "dark",
    "csrf_token": get_csrf(ann1.get("/annotator/profile")
                           .get_data(as_text=True))})
expect("Theme switched to dark" in flash_of(r, ann1), "dark theme applies")
expect('data-bs-theme="dark"' in ann1.get("/annotator/").get_data(
    as_text=True), "pages render with data-bs-theme=dark")
css = open(os.path.join(ROOT, "static/css/style.css")).read()
expect("[data-bs-theme=\"dark\"]" in css,
       "stylesheet ships dark-theme token overrides")
r = ann1.post("/account/theme", data={
    "theme": "blue", "csrf_token": get_csrf(
        ann1.get("/annotator/profile").get_data(as_text=True))})
expect("Unknown theme" in flash_of(r, ann1), "invalid theme refused")
with app.app_context():
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.theme",
        AuditLog.entity_id == ann1_id)) is not None,
        "user.theme audited")

# ---- continue working ---------------------------------------------------
print("== Continue working ==")
r = ann1.post("/annotator/continue", data={"csrf_token": get_csrf(
    ann1.get("/annotator/").get_data(as_text=True))})
loc = r.headers.get("Location", "")
expect(r.status_code == 302 and "/annotator/task/" in loc,
       "continue opens a task page")
target_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
with app.app_context():
    oldest = db.session.scalars(db.select(Task.id).where(
        Task.assigned_to == ann1_id,
        Task.status.in_(("assigned", "in_progress", "rejected")),
    ).order_by(Task.created_at.asc(), Task.id.asc())).first()
expect(target_id == oldest, "it picks the OLDEST open task")
r = empty_c.post("/annotator/continue", data={"csrf_token": get_csrf(
    empty_c.get("/annotator/profile").get_data(as_text=True))})
expect("No open tasks right now" in flash_of(r, empty_c),
       "empty queue gets the friendly note")
r = app.test_client().post("/annotator/continue", data={})
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anon: continue redirects to sign-in")
r = adm.post("/annotator/continue", data={})
expect(r.status_code == 403, "admin: continue is 403")

# ---- dashboard stats -----------------------------------------------------
print("== Dashboard stats ==")
with app.app_context():
    t_ids = db.session.scalars(db.select(Task.id).where(
        Task.assigned_to == ann1_id)).all()[:3]
    db.session.add(Submission(
        task_id=t_ids[0], annotator_id=ann1_id,
        output_data="{}", status="approved", qa_score=90,
        submitted_at=utc_now(), reviewed_at=utc_now()))
    db.session.add(Submission(
        task_id=t_ids[1], annotator_id=ann1_id, output_data="{}",
        submitted_at=utc_now()))
    db.session.commit()
body = ann1.get("/annotator/").get_data(as_text=True)
expect("Today: 2 submitted" in body, "today counter matches DB")
expect("Active streak: 1 day" in body, "streak line renders")
expect("&#8358;160" in body, "naira all-time line renders (₦160 at 1600)")
expect("Continue working" in body, "continue button on dashboard")

# ---- bank details (payout account) ---------------------------------------
print("== Bank details ==")
page = ann1.get("/annotator/profile").get_data(as_text=True)
expect("Payout account" in page and "NUBAN" in page,
       "profile shows the payout-account card")

def bank_post(number, name_bank="GTBank", acct="Tunde Adewale A",
              action="save_bank", acct_type="savings"):
    return ann1.post("/annotator/profile/bank-details", data={
        "bank_name": name_bank, "account_type": acct_type,
        "account_number": number,
        "account_name": acct, action: "y",
        "csrf_token": get_csrf(ann1.get("/annotator/profile")
                               .get_data(as_text=True))})

r = ann1.post("/annotator/profile/bank-details", data={
    "bank_name": "GTBank", "account_number": "0123456789",
    "account_name": "x"})
expect(r.status_code == 302, "bank change without CSRF refused")
r = bank_post("012345678")  # 9 digits
expect("10-digit NUBAN" in flash_of(r, ann1)
       or "10 digits" in flash_of(r, ann1),
       "9-digit account number refused")
r = bank_post("01234567890")  # 11 digits
expect("10-digit NUBAN" in flash_of(r, ann1)
       or "10 digits" in flash_of(r, ann1),
       "11-digit account number refused")
r = bank_post("0123456789")
expect("Payout account saved" in flash_of(r, ann1),
       "valid NUBAN saved")
with app.app_context():
    row = db.session.get(User, ann1_id)
    expect(row.bank_name == "GTBank"
           and row.bank_account_name == "Tunde Adewale A"
           and row.bank_account_type == "savings",
           "bank details + account type stored")
    expect(row.bank_account_number != "0123456789"
           and row.bank_account_last4 == "6789",
           "number stored ENCRYPTED with last4 for display")
    zero = bank_post("0012345678")
    row = db.session.get(User, ann1_id)
    expect(row.get_bank_account_number() == "0012345678"
           and row.bank_account_last4 == "5678",
           "leading zeros survive the encrypted round trip")
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.bank_details_update",
        AuditLog.entity_id == ann1_id)) is not None
expect(audit_ok, "user.bank_details_update audited")
with app.app_context():
    detail = db.session.scalar(db.select(AuditLog.detail).where(
        AuditLog.action == "user.bank_details_update",
        AuditLog.entity_id == ann1_id))
expect("0123456789" not in (detail or "")
       and "0012345678" not in (detail or ""),
       "audit trail never carries the account number")
r = bank_post("", action="remove_bank")
expect("Payout account removed" in flash_of(r, ann1), "removal works")
with app.app_context():
    row = db.session.get(User, ann1_id)
    expect(row.bank_account_number is None
           and row.bank_account_last4 is None
           and row.bank_account_type is None,
           "bank columns (incl. last4/type) cleared on removal")

# ---- profile details: phone number + navbar Profile link -----------------
print("== Profile details (phone) + navbar ==")
body = ann1.get("/annotator/").get_data(as_text=True)
expect('href="/annotator/profile"' in body,
       "navbar shows a Profile link")
page = ann1.get("/annotator/profile").get_data(as_text=True)
expect('name="phone"' in page and "Account type" in page,
       "profile shows phone field and bank account-type select")
tk = get_csrf(page)
r = ann1.post("/annotator/profile", data={
    "name": "Ngozi Okafor-Okon", "phone": "08012345678",
    "csrf_token": tk, "save_name": "y"})
expect("details have been updated" in flash_of(r, ann1),
       "phone + name save flashes success")
with app.app_context():
    row = db.session.get(User, ann1_id)
    expect(row.phone == "08012345678", "phone persisted")
    n_phone_audit = db.session.scalar(db.select(
        db.func.count(AuditLog.id)).where(
        AuditLog.detail.contains("08012345678")))
expect(n_phone_audit == 0, "phone number never enters the audit trail")
tk = get_csrf(ann1.get("/annotator/profile").get_data(as_text=True))
r = ann1.post("/annotator/profile", data={
    "name": "Ngozi Okafor-Okon", "phone": "12ab",
    "csrf_token": tk, "save_name": "y"})
expect(r.status_code == 200 and "Enter 7-15 digits"
       in r.get_data(as_text=True), "invalid phone refused inline")
r = bank_post("0123456789", acct_type="")
expect("Choose the account type" in flash_of(r, ann1),
       "bank save without account type refused")

# ---- hygiene -------------------------------------------------------------
print("== Hygiene ==")
for template in ("annotator/profile.html", "annotator/terms.html",
                 "annotator/dashboard.html"):
    src = open(os.path.join(ROOT, "templates", template)).read()
    assert not re.search(r"\son[a-z]+=\"", src), template
expect(True, "no inline event handlers in new templates")
js = open(os.path.join(ROOT, "static/js/main.js")).read()
expect("Keyboard answer selection" in js,
       "keyboard picker present in main.js")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"ANNOTATOR EXPERIENCE TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL ANNOTATOR EXPERIENCE TESTS PASSED")
