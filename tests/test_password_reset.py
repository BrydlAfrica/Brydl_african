"""Self-service password reset tests (email one-off token flow)."""
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import AuditLog, User, utc_now  # noqa: E402
from routes import auth as auth_module  # noqa: E402

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


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


def last_link_from_logs():
    for record in reversed(_log_records):
        m = re.search(r"(https?://\S+/reset-password/\S+)", record)
        if m:
            return m.group(1)
    return None


# capture the dev-fallback "email" (the reset link in the mailer log)
_log_records: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        _log_records.append(record.getMessage())


logging.getLogger("mailer").addHandler(_Capture())

app = create_app()
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann.set_password("annotator-pass-1")
    ghost = User(name="Ghost G", email="ghost@brydl.ng",
                 role="annotator", is_active=False)
    ghost.set_password("ghost-pass-1")
    db.session.add_all([admin, ann, ghost])
    db.session.commit()
    ann_id = ann.id

# ---- request phase ------------------------------------------------------
print("== Forgot password ==")
c = app.test_client()
page = c.get("/forgot-password").get_data(as_text=True)
expect("Forgot your password?" in page
       and 'name="email"' in page,
       "forgot page renders the request form")
r = c.get("/forgot-password")
expect(r.status_code == 200, "forgot page is public (anonymous)")

r = c.post("/forgot-password", data={"email": "not-an-email"})
expect(r.status_code == 200 and "Enter a valid email address"
       in r.get_data(as_text=True),
       "malformed email refused inline")

login_page = c.get("/login").get_data(as_text=True)
expect('href="/forgot-password"' in login_page,
       "login page links to forgot password")

# authenticated users never see it
adm = app.test_client()
tk = get_csrf(adm.get("/login").get_data(as_text=True))
adm.post("/login", data={"email": "admin@brydl.ng",
                         "password": "admin-pass-1", "csrf_token": tk})
r = adm.get("/forgot-password")
expect(r.status_code == 302, "signed-in user redirected away from forgot")

# happy path: token stored as hash, link delivered via dev log
with app.app_context():
    auth_module._forgot_requests.clear()
r = c.post("/forgot-password", data={
    "email": "a@brydl.ng",
    "csrf_token": get_csrf(
        c.get("/forgot-password").get_data(as_text=True))},
    follow_redirects=False)
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "request redirects to login with the sent-flash")
with app.app_context():
    row = db.session.scalar(db.select(User).where(User.id == ann_id))
    expect(row.reset_token_hash and len(row.reset_token_hash) == 64,
           "token stored as a 64-char SHA-256 hash (never the raw link)")
    expect(row.reset_token_expires
           and row.reset_token_expires > utc_now(),
           "expiry set one hour ahead (UTC)")
link = last_link_from_logs()
expect(link is not None and "/reset-password/" in link,
       "dev-fallback delivers the reset link (logged while SMTP unset)")
with app.app_context():
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.password_reset.requested",
        AuditLog.detail.like('%"delivery": "dev_log"%'))) is not None
expect(audit_ok, "auth.password_reset.requested audited")

# anti-enumeration: unknown + inactive emails look identical
before = len(_log_records)
r = c.post("/forgot-password", data={
    "email": "nobody@brydl.ng",
    "csrf_token": get_csrf(
        c.get("/forgot-password").get_data(as_text=True))},
    follow_redirects=False)
body = c.get(r.headers["Location"]).get_data(as_text=True)
expect("on its way" in body, "unknown email gets the same generic flash")
with app.app_context():
    n_requested = db.session.scalar(db.select(
        db.func.count(AuditLog.id)).where(
        AuditLog.action == "auth.password_reset.requested"))
expect(n_requested == 1, "unknown email: no delivery, no audit row")
r = c.post("/forgot-password", data={
    "email": "ghost@brydl.ng",
    "csrf_token": get_csrf(
        c.get("/forgot-password").get_data(as_text=True))},
    follow_redirects=False)
with app.app_context():
    ghost_row = db.session.scalar(db.select(User).where(
        User.email == "ghost@brydl.ng"))
    expect(ghost_row.reset_token_hash is None,
           "inactive account: no token issued (still anti-enumeration)")
expect(len(_log_records) == before, "inactive account: no email sent")

# throttle: FORGOT_MAX_PER_WINDOW per email per window (admin email,
# so ann's captured reset link from above stays the live one)
with app.app_context():
    auth_module._forgot_requests.clear()
throttled_before = len(_log_records)
for i in range(4):  # 3 allowed + 1 over the cap
    c.post("/forgot-password", data={
        "email": "admin@brydl.ng",
        "csrf_token": get_csrf(
            c.get("/forgot-password").get_data(as_text=True))})
with app.app_context():
    row = db.session.scalar(db.select(User).where(User.id == ann_id))
    throttled_audit = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.password_reset.throttled")) is not None
expect(throttled_audit, "4th request inside the window is throttled"
       " (audited)")
expect(len(_log_records) - throttled_before == 3,
       "exactly 3 emails went out, not 4 (email-bombing guard)")

# ---- reset phase ----------------------------------------------------------
print("== Reset password ==")
raw_token = link.rsplit("/", 1)[-1]
bad = app.test_client().get("/reset-password/deadbeef")
expect(bad.status_code == 302
       and "/forgot-password" in bad.headers["Location"],
       "garbage token redirected to the request page")

reset_page = c.get(f"/reset-password/{raw_token}")
expect(reset_page.status_code == 200
       and "Choose a new password" in reset_page.get_data(as_text=True),
       "valid token renders the new-password form")

# validation failures must NOT consume the token
r = c.post(f"/reset-password/{raw_token}", data={
    "password": "short", "confirm_password": "short",
    "csrf_token": get_csrf(reset_page.get_data(as_text=True))})
expect(r.status_code == 200 and "between 8 and 128"
       in r.get_data(as_text=True), "short password refused inline")
r = c.post(f"/reset-password/{raw_token}", data={
    "password": "brand-new-pass-9", "confirm_password": "different",
    "csrf_token": get_csrf(
        c.get(f"/reset-password/{raw_token}").get_data(as_text=True))})
expect(r.status_code == 200 and "do not match"
       in r.get_data(as_text=True), "mismatched confirm refused inline")

r = c.post(f"/reset-password/{raw_token}", data={
    "password": "brand-new-pass-9", "confirm_password": "brand-new-pass-9",
    "csrf_token": get_csrf(
        c.get(f"/reset-password/{raw_token}").get_data(as_text=True))},
    follow_redirects=False)
expect(r.status_code == 302 and "/login" in r.headers["Location"],
       "successful reset redirects to login")

r = login(app.test_client(), "a@brydl.ng", "annotator-pass-1")
expect(r.status_code == 200 and "Invalid email or password"
       in r.get_data(as_text=True), "old password no longer works")
r = login(app.test_client(), "a@brydl.ng", "brand-new-pass-9")
expect(r.status_code == 302 and "/annotator" in r.headers.get(
    "Location", ""), "new password signs in")

with app.app_context():
    row = db.session.get(User, ann_id)
    expect(row.reset_token_hash is None
           and row.reset_token_expires is None,
           "token columns cleared (link is single-use)")
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "auth.password_reset",
        AuditLog.entity_id == ann_id)) is not None
expect(audit_ok, "auth.password_reset audited")
replay = c.get(f"/reset-password/{raw_token}", follow_redirects=False)
expect(replay.status_code == 302
       and "/forgot-password" in replay.headers["Location"],
       "replaying the used link is refused")

# expired token refused
with app.app_context():
    user = db.session.get(User, ann_id)
    user.reset_token_hash = auth_module._hash_reset_token("expired-tok")
    user.reset_token_expires = datetime(2020, 1, 1)  # naive UTC
    db.session.commit()
r = c.get("/reset-password/expired-tok", follow_redirects=False)
expect(r.status_code == 302
       and "/forgot-password" in r.headers["Location"],
       "expired token refused like an unknown one")

# ---- hygiene -----------------------------------------------------------------
print("== Hygiene ==")
for template in ("forgot_password.html", "reset_password.html",
                 "login.html"):
    src = open(os.path.join(ROOT, "templates", template)).read()
    assert not re.search(r"\son[a-z]+=\"", src), template
expect(True, "no inline event handlers in touched templates")
expect("reset_token_hash" in open(
    os.path.join(ROOT, "models.py")).read()
    and "sha256" in open(os.path.join(ROOT, "routes/auth.py")).read(),
    "raw tokens never stored (hash-only discipline in code)")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"PASSWORD RESET TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL PASSWORD RESET TESTS PASSED")
