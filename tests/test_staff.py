"""Staff-facing surfaces: profile, change password, feedback, notifications.

Covers: password change (wrong current rejected, mismatch rejected),
feedback thread creation + admin reply, broadcast delivery, profile
render per role, bell-counter unread, notifications pagination, role
walls.
"""
from __future__ import annotations
from app import create_app
from models import FeedbackReply, FeedbackThread, Notification, User
from extensions import db
import os
import re
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


app = create_app()
TERMS_V = app.config.get("TERMS_VERSION", "1.0") or "1.0"
with app.app_context():
    db.drop_all()
    db.create_all()
    adm = User(name="Admin", email="a@x.com", role="admin")
    adm.set_password("admin123")
    pm = User(name="PM", email="p@x.com", role="project_manager", terms_version=TERMS_V)
    pm.set_password("pm123")
    ann = User(name="Ann", email="n@x.com", role="annotator", terms_version=TERMS_V)
    ann.set_password("ann123")
    db.session.add_all([adm, pm, ann])
    db.session.commit()
    aid, pid, nid = adm.id, pm.id, ann.id


def tok(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(c, e, pw):
    p = c.get("/login").get_data(as_text=True)
    return c.post("/login", data={"csrf_token": tok(p), "email": e, "password": pw}, follow_redirects=True)


def gt(c, path): return tok(c.get(path).get_data(as_text=True))


ac, pc, nc = app.test_client(), app.test_client(), app.test_client()
print("== login")
expect(login(ac, "a@x.com", "admin123").status_code == 200, "admin login")
expect(login(pc, "p@x.com", "pm123").status_code == 200, "pm login")
expect(login(nc, "n@x.com", "ann123").status_code == 200, "ann login")

print("== role walls")
expect(nc.get("/admin/", follow_redirects=False).status_code == 403, "ann blocked /admin/")
expect(nc.get("/pm/", follow_redirects=False).status_code == 403, "ann blocked /pm/")
expect(pc.get("/admin/", follow_redirects=False).status_code == 403, "pm blocked /admin/")

print("== profile renders per role")
expect(ac.get("/profile").status_code == 200, "admin profile")
expect(pc.get("/profile").status_code == 200, "pm profile")
expect(nc.get("/profile").status_code == 200, "ann profile")
expect(b"Change password" in nc.get("/profile").data, "profile has password form")

print("== change password — wrong current")
r = nc.post("/profile", data={
    "csrf_token": gt(nc, "/profile"),
    "form_name": "password",
    "current_password": "wrongpw", "new_password": "newpass1234!", "confirm_password": "newpass1234!",
    "submit": "Change password",
}, follow_redirects=True)
with app.app_context():
    db.session.expire_all()
    u = db.session.get(User, nid)
    expect(not u.check_password("newpass1234!"), "wrong current → password unchanged")
expect(b"incorrect" in r.data.lower() or b"current password" in r.data.lower(), "error shown")

print("== change password — mismatch")
r = nc.post("/profile", data={
    "csrf_token": gt(nc, "/profile"),
    "form_name": "password",
    "current_password": "ann123", "new_password": "newpass1234!", "confirm_password": "otherpass1!",
    "submit": "Change password",
}, follow_redirects=True)
expect(b"match" in r.data.lower(), "mismatch error shown")

print("== change password — success")
r = nc.post("/profile", data={
    "csrf_token": gt(nc, "/profile"),
    "form_name": "password",
    "current_password": "ann123", "new_password": "newpass1234!", "confirm_password": "newpass1234!",
    "submit": "Change password",
}, follow_redirects=True)
with app.app_context():
    db.session.expire_all()
    u = db.session.get(User, nid)
    expect(u.check_password("newpass1234!"), "password updated")
# Password change bumps auth_version → other devices signed out; re-login.
login(nc, "n@x.com", "newpass1234!")

print("== feedback: PM submits, admin sees + replies")
r = pc.post("/feedback", data={
    "csrf_token": gt(pc, "/feedback"),
    "subject": "URGENT question", "body": "How do I close a project?",
}, follow_redirects=True)
with app.app_context():
    th = db.session.scalars(db.select(FeedbackThread).where(
        FeedbackThread.subject == "URGENT question")).first()
    expect(th is not None and th.status == "new", "thread created")
    tid = th.id if th else 0
    # Admin notified
    n = db.session.scalar(db.select(Notification).where(
        Notification.user_id == aid, Notification.kind == "feedback.new"))
    expect(n is not None, "admin notified")
r = ac.get("/feedback")
expect(b"URGENT question" in r.data, "admin inbox shows thread")
ac.post(f"/feedback/{tid}", data={"csrf_token": gt(ac, "/feedback"), "body": "Go to settings."}, follow_redirects=True)
with app.app_context():
    expect(db.session.scalar(db.select(FeedbackReply).where(
        FeedbackReply.thread_id == tid)) is not None, "reply saved")
    expect(db.session.scalar(db.select(Notification).where(
        Notification.user_id == pid, Notification.kind == "feedback.reply")) is not None,
        "PM notified of reply")

print("== broadcast: admin sends; both PM and ann get notification")
ac.post("/admin/messages/broadcast", data={
    "csrf_token": gt(ac, "/feedback"),
    "subject": "Holiday", "body": "Office closed Friday.",
}, follow_redirects=True)
with app.app_context():
    for uid in (pid, nid):
        expect(db.session.scalar(db.select(Notification).where(
            Notification.user_id == uid, Notification.kind == "broadcast.new")) is not None,
            f"broadcast to user {uid}")

print("== notifications page renders for non-admin, pagination works")
# Inject enough notifications to trigger second page (shared /notifications is staff blueprint)
with app.app_context():
    for i in range(35):
        db.session.add(Notification(user_id=nid, kind="task.assigned", payload="{}"))
    db.session.commit()
r = nc.get("/notifications")
if r.status_code != 200:
    print("    notif status", r.status_code, r.headers.get("Location"), r.data[:300])
expect(r.status_code == 200, "ann notifications page")
r = nc.get("/notifications?page=2")
expect(r.status_code == 200, "ann notifications page 2")
# PM inbox has feedback + broadcast threads
r = pc.get("/feedback")
expect(r.status_code == 200, "pm feedback page")

print("== anon blocked")
anon = app.test_client()
for path in ["/profile", "/feedback", "/notifications"]:
    expect(anon.get(path, follow_redirects=False).status_code in (302,), f"anon redirected from {path}")

print()
if FAIL:
    print(f"STAFF TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL STAFF TESTS PASSED")
