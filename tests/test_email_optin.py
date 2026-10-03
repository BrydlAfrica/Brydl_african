"""Phase 13-H: opt-in SMTP mirror of in-app notifications."""
from __future__ import annotations
import notifications
from models import BackgroundJob, Notification, User, utc_now
from extensions import db
from app import create_app
import os
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
with app.app_context():
    db.drop_all()
    db.create_all()
    a = User(name="A", email="a@x.com", role="annotator", terms_version="1.0")
    a.set_password("x")
    db.session.add(a)
    db.session.commit()
    aid = a.id

# Default off
with app.app_context():
    u = db.session.get(User, aid)
    expect(u.email_notifications_opt_in is False, "default opt-in is False")

# Phase 1: opt-in False → no email even with SMTP configured
app.config["SMTP_HOST"] = "smtp.example.com"
app.config["JOBS_ENABLED"] = False
os.environ["BRYDL_DISABLE_JOBS"] = "1"
with app.app_context():
    u = db.session.get(User, aid)
    sent = []

    def fake_send(to, subj, body):
        sent.append((to, subj))
    # Patch the reference notifications.py already imported.
    orig = notifications.send_notification_email
    notifications.send_notification_email = fake_send
    try:
        notifications.notify(u, "task.assigned", subject="Work arrived",
                             body="x", commit=True)
        expect(len(sent) == 0, "no email when opt-in is False")
        sent.clear()
        u.email_notifications_opt_in = True
        db.session.commit()
        notifications.notify(u, "task.assigned", subject="Work arrived",
                             body="x", commit=True)
        expect(len(sent) == 1 and sent[0][1] == "Work arrived",
               "email sent when opt-in is True and SMTP configured")
        n_count = db.session.query(Notification).filter_by(user_id=aid).count()
        expect(n_count == 2, f"in-app rows created regardless ({n_count})")
    finally:
        notifications.send_notification_email = orig
        app.config["SMTP_HOST"] = ""
        app.config["JOBS_ENABLED"] = False

print()
if FAIL:
    print(f"SMTP OPT-IN TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL SMTP OPT-IN TESTS PASSED")
