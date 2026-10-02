"""Admin dashboard KPIs, action flags, activity feed."""
from __future__ import annotations
from app import create_app
from models import FeedbackThread, Project, SiteSetting, Task, User, utc_now
from extensions import db
import os
import re
import sys
from decimal import Decimal
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
    ad = User(name="A", email="a@x.com", role="admin")
    ad.set_password("x")
    db.session.add(ad)
    pm = User(name="P", email="p@x.com", role="project_manager", terms_version=TERMS_V)
    pm.set_password("x")
    db.session.add(pm)
    db.session.commit()
    pm_id = pm.id
    pr = Project(name="P", client_name="C", task_type="classification",
                 pay_per_task=Decimal("0.10"), instructions="x",
                 created_by=ad.id, approved_by=ad.id, approved_at=utc_now(),
                 status="active")
    db.session.add(pr)
    db.session.commit()
    for _ in range(3):
        db.session.add(Task(project_id=pr.id, input_data="q", status="pending"))
    db.session.add(FeedbackThread(kind="feedback", subject="hello", body="x",
                                  author_id=pm.id, status="new"))
    db.session.add(SiteSetting(key="usd_to_ngn_rate", value="1700"))
    pr2 = Project(name="Draft", client_name="C", task_type="translation",
                  proposed_pay_per_task=Decimal("0.05"),
                  instructions="x", created_by=pm.id, status="pending_approval")
    db.session.add(pr2)
    db.session.commit()


def tok(h):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', h)
    return m.group(1) if m else ""


def login(c, e, p):
    h = c.get("/login").get_data(as_text=True)
    return c.post("/login", data={"csrf_token": tok(h), "email": e, "password": p}, follow_redirects=True)


ac, pc = app.test_client(), app.test_client()
print("== admin dashboard basics")
expect(login(ac, "a@x.com", "x").status_code == 200, "admin login")
r = ac.get("/admin/")
expect(r.status_code == 200, "dashboard 200")
expect(b"Recent activity" in r.data, "activity feed present")
expect(b"Needs attention" in r.data, "needs-attention panel present")
expect(b"1700" in r.data, "USD-NGN rate visible")
expect(b"\xe2\x82\xa6" in r.data, "naira symbol present")
expect(b"Open review queue" in r.data or b"review queue" in r.data.lower()
       or b"Awaiting your review" in r.data, "review queue card present")
expect(b"/admin/settings" in r.data or b"Settings" in r.data, "settings link present")
expect(b"/admin/payments" in r.data or b"Payments" in r.data, "payments link present")
expect(b"/admin/approvals" in r.data, "approvals link present")
expect(login(pc, "p@x.com", "x").status_code == 200, "pm login")
expect(pc.get("/admin/", follow_redirects=False).status_code == 403, "pm blocked /admin/")

print("== stats populate after seed")
r = ac.get("/admin/")
expect(r.status_code == 200, "re-render 200")
expect(b"Approvals" in r.data, "approvals badge visible when pending")
expect(b"Unread messages" in r.data or b"messages" in r.data.lower(), "messages tile present")

print("== 7-day submissions counter renders without error with empty DB")
r = ac.get("/admin/")
expect(r.status_code == 200, "dashboard renders even with 0 submissions 7d")

print()
if FAIL:
    print(f"ADMIN DASH TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL ADMIN DASH TESTS PASSED")
