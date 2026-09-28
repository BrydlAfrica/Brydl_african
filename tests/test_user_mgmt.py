"""User management (Team page) + seed-admin CLI tests."""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
shutil.rmtree(os.path.join(ROOT, "instance"), ignore_errors=True)
shutil.rmtree(os.path.join(ROOT, "backups"), ignore_errors=True)

from app import create_app
from extensions import db
from models import AuditLog, User

CLI_EMAIL = "chief@brydl.ng"
CLI_PASSWORD = "seed-admin-pass-1"

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def run_cli(args):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app"] + args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


def flash_of(response, client):
    return client.get(response.headers["Location"]).get_data(as_text=True)


# ---- CLI phase: fresh database, no admin yet -------------------------
print("== seed-admin CLI ==")
proc = run_cli(["seed-admin", "--email", CLI_EMAIL,
                "--password", "short"])
expect(proc.returncode != 0 and "8 and 128" in proc.stdout + proc.stderr,
       "refuses a password under 8 characters")
proc = run_cli(["seed-admin", "--email", "not-an-email",
                "--password", CLI_PASSWORD])
expect(proc.returncode != 0 and "usable email" in proc.stdout + proc.stderr,
       "refuses a malformed email")
proc = run_cli(["seed-admin", "--email", CLI_EMAIL.upper(),
                "--password", CLI_PASSWORD])
expect(proc.returncode == 0 and "Admin account created" in proc.stdout,
       "creates the first admin (email normalised)")
proc = run_cli(["seed-admin", "--email", "other@brydl.ng",
                "--password", CLI_PASSWORD])
expect(proc.returncode != 0 and "already exists" in proc.stdout + proc.stderr,
       "refuses a second admin while one is active")
proc = run_cli(["seed-admin", "--email", CLI_EMAIL,
                "--password", CLI_PASSWORD])
expect(proc.returncode != 0, "refuses a duplicate email too")

app = create_app()
with app.app_context():
    row = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "system.seed_admin"))
    expect(row is not None and row.detail is not None
           and CLI_EMAIL in row.detail, "system.seed_admin audited")
    expect(db.session.scalar(db.select(db.func.count(User.id)).where(
        User.role == "admin", User.is_active.is_(True))) == 1,
        "exactly one active admin")

adm = app.test_client()
r = login(adm, CLI_EMAIL, CLI_PASSWORD)
expect(r.status_code == 302 and "/admin" in r.headers.get("Location", ""),
       "seeded admin signs in")

# ---- UI phase: RBAC --------------------------------------------------
print("== Team page: RBAC ==")
anon = app.test_client()
r = anon.get("/admin/users")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anon: team page redirects to sign-in")
r = anon.post("/admin/users/create", data={"name": "X", "email": "x@x.ng",
                                           "role": "annotator"})
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "anon: create redirects to sign-in")
ann_probe = app.test_client()
page = ann_probe.get("/login").get_data(as_text=True)
ann_probe.post("/login", data={
    "email": CLI_EMAIL, "password": CLI_PASSWORD,
    "csrf_token": get_csrf(page)})
# wrong role: use an annotator — create one first via the UI (below) but a
# quick check with admin creds on annotator routes would not exercise this.
# Instead verify the annotator path after creating one.

# ---- create annotator via the UI -------------------------------------
print("== Create account ==")
page = adm.get("/admin/users").get_data(as_text=True)
expect("Team" in page and CLI_EMAIL in page and "Active" in page,
       "team page lists the admin with status")
tk = get_csrf(page)
r = adm.post("/admin/users/create", data={
    "name": "Tunde A", "email": "tunde@brydl.ng", "role": "annotator",
    "csrf_token": tk})
body = flash_of(r, adm)
m = re.search(r"Temporary password \(shown once\):\s*([A-Za-z0-9_\-]+)",
              body)
expect(r.status_code == 302 and m is not None,
       "create flashes the one-off password")
temp_pw = m.group(1) if m else ""
with app.app_context():
    row = db.session.scalar(db.select(User).where(
        User.email == "tunde@brydl.ng"))
    expect(row is not None and row.role == "annotator"
           and row.is_active, "account exists as active annotator")
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.create",
        AuditLog.detail.like("%tunde@brydl.ng%"))) is not None
expect(audit_ok, "user.create audited")
ann = app.test_client()
r = login(ann, "tunde@brydl.ng", temp_pw)
expect(r.status_code == 302 and "/annotator" in r.headers.get("Location", ""),
       "annotator signs in with the one-off password")
r = ann.get("/admin/users")
expect(r.status_code == 403, "annotator: team page is 403")
r = ann.post("/admin/users/create", data={"name": "Y", "email": "y@x.ng",
                                          "role": "admin"})
expect(r.status_code == 403, "annotator: create is 403")

r = adm.post("/admin/users/create", data={
    "name": "Dup", "email": "TUNDE@brydl.ng", "role": "annotator",
    "csrf_token": get_csrf(adm.get("/admin/users").get_data(as_text=True))})
expect("already has an account" in flash_of(r, adm),
       "duplicate email refused (case-insensitive)")
r = adm.post("/admin/users/create", data={
    "name": "Bad", "email": "not-an-email", "role": "annotator",
    "csrf_token": get_csrf(adm.get("/admin/users").get_data(as_text=True))})
expect("Enter a valid email address" in flash_of(r, adm),
       "malformed email refused")

# ---- deactivate / reactivate -----------------------------------------
print("== Deactivate / reactivate ==")
with app.app_context():
    ann_id = db.session.scalar(db.select(User.id).where(
        User.email == "tunde@brydl.ng"))
    chief_id = db.session.scalar(db.select(User.id).where(
        User.email == CLI_EMAIL))
tk = get_csrf(adm.get("/admin/users").get_data(as_text=True))
r = adm.post(f"/admin/users/{ann_id}/toggle-active",
             data={"csrf_token": tk})
expect("deactivated" in flash_of(r, adm), "deactivate flashes")
r = ann.get("/annotator/")
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "deactivated annotator's live session is revoked")
with app.app_context():
    expect(db.session.scalar(db.select(User.is_active).where(
        User.id == ann_id)) is False, "is_active flipped in DB")
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.deactivate",
        AuditLog.entity_id == ann_id)) is not None
expect(audit_ok, "user.deactivate audited")

r = adm.post(f"/admin/users/{ann_id}/toggle-active",
             data={"csrf_token": get_csrf(
                 adm.get("/admin/users").get_data(as_text=True))})
expect("reactivated" in flash_of(r, adm), "reactivate flashes")
r = login(app.test_client(), "tunde@brydl.ng", temp_pw)
expect(r.status_code == 302 and "/annotator" in r.headers.get("Location", ""),
       "reactivated annotator can sign in again")

r = adm.post(f"/admin/users/{chief_id}/toggle-active",
             data={"csrf_token": get_csrf(
                 adm.get("/admin/users").get_data(as_text=True))})
expect("cannot deactivate your own account" in flash_of(r, adm),
       "self-deactivation refused")

# ---- password reset ----------------------------------------------------
print("== Reset password ==")
r = adm.post(f"/admin/users/{ann_id}/reset-password",
             data={"csrf_token": get_csrf(
                 adm.get("/admin/users").get_data(as_text=True))})
body = flash_of(r, adm)
m = re.search(r"Temporary password \(shown once\):\s*([A-Za-z0-9_\-]+)",
              body)
new_pw = m.group(1) if m else ""
expect(m is not None, "reset flashes a new one-off password")
r = login(app.test_client(), "tunde@brydl.ng", temp_pw)
expect(r.status_code == 200 and "Invalid email or password"
       in r.get_data(as_text=True), "old password no longer works")
r = login(app.test_client(), "tunde@brydl.ng", new_pw)
expect(r.status_code == 302 and "/annotator" in r.headers.get("Location", ""),
       "new one-off password works")
with app.app_context():
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "user.password_reset",
        AuditLog.entity_id == ann_id)) is not None,
        "user.password_reset audited")

# ---- bank details visible to admin (masked) ------------------------------
print("== Payout account (Team) ==")
page = adm.get("/admin/users").get_data(as_text=True)
expect("Not provided" in page,
       "team page flags annotators without payout details")
# tunde re-signs in (his earlier session died with the deactivation
# test) using the reset password, accepts the terms (Team-created
# accounts start un-accepted), then adds his payout account.
ann2 = app.test_client()
login(ann2, "tunde@brydl.ng", new_pw)
terms_page = ann2.get("/annotator/terms").get_data(as_text=True)
ann2.post("/annotator/terms/accept", data={
    "csrf_token": get_csrf(terms_page)})
# tunde saves bank details through the annotator endpoint
tk_ann = get_csrf(ann2.get("/annotator/profile").get_data(as_text=True))
r = ann2.post("/annotator/profile/bank-details", data={
    "bank_name": "Kuda", "account_type": "current",
    "account_number": "2098765432",
    "account_name": "Tunde A", "save_bank": "y", "csrf_token": tk_ann})
assert "Payout account saved" in ann2.get(
    r.headers["Location"]).get_data(as_text=True), "bank save failed"
# Tunde sorts after the bulk users — find his page (2 accounts/page
# of 20; he lives on the last page)
page2 = adm.get("/admin/users?page=2").get_data(as_text=True)
page = page2 if "****5432" in page2 else adm.get(
    "/admin/users?page=1").get_data(as_text=True)
expect("Kuda" in page and "****5432" in page
       and "Tunde A" in page,
       "team page shows bank + MASKED number (last 4 only)")
expect("2098765432" not in page,
       "FULL account number never rendered in the admin UI")

# ---- pagination at 20/page ---------------------------------------------
print("== Pagination ==")
for i in range(20):
    adm.post("/admin/users/create", data={
        "name": f"Bulk User {i:02d}", "email": f"bulk{i:02d}@brydl.ng",
        "role": "annotator",
        "csrf_token": get_csrf(adm.get("/admin/users").get_data(
            as_text=True))})
page1 = adm.get("/admin/users").get_data(as_text=True)
expect("Showing 20 of 22 account" in page1
       and "Team pages" in page1, "page 1: 20 of 22 with pager")
page2 = adm.get("/admin/users?page=2").get_data(as_text=True)
expect("Bulk User 19" in page2 and "Bulk User 00" not in page2,
       "page 2 holds the remainder")
expect(adm.get("/admin/users?page=99").status_code in (200, 302),
       "out-of-range page handled")

# ---- hygiene ------------------------------------------------------------
template = open(os.path.join(ROOT, "templates/admin/users.html")).read()
expect(not re.search(r"\son[a-z]+=\"", template), "no inline handlers")
base = open(os.path.join(ROOT, "templates/base.html")).read()
expect("/admin/users" in base, "Team nav link present")

shutil.rmtree(os.path.join(ROOT, "instance"), ignore_errors=True)
shutil.rmtree(os.path.join(ROOT, "backups"), ignore_errors=True)
print()
if FAILURES:
    print(f"USER MANAGEMENT TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL USER MANAGEMENT TESTS PASSED")
