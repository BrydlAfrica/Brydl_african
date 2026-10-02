"""NGN parallel display — helper and templates."""
from __future__ import annotations
import os, re, sys
from decimal import Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); os.chdir(ROOT); os.environ["BRYDL_DISABLE_JOBS"] = "1"
FAIL = []
def expect(c, m):
    if not c: FAIL.append(m); print("  FAIL:", m)
    else: print("   ok:", m)

from app import create_app
from extensions import db
from models import Project, SiteSetting, Task, User, utc_now
from routes import usd_to_ngn

app = create_app()
with app.app_context():
    db.drop_all(); db.create_all()
    ad = User(name="A", email="a@x.com", role="admin"); ad.set_password("x"); db.session.add(ad)
    ann = User(name="N", email="n@x.com", role="annotator", terms_version=app.config.get("TERMS_VERSION", "1.0"))
    ann.set_password("x"); db.session.add(ann)
    db.session.commit()
    pr = Project(name="P", client_name="C", task_type="classification",
                 pay_per_task=Decimal("0.10"), instructions="x",
                 created_by=ad.id, approved_by=ad.id, approved_at=utc_now(), status="active")
    db.session.add(pr); db.session.commit()
    db.session.add(SiteSetting(key="usd_to_ngn_rate", value="1700"))
    db.session.commit()

def tok(h):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', h); return m.group(1) if m else ""
def login(c, e, p):
    h = c.get("/login").get_data(as_text=True)
    return c.post("/login", data={"csrf_token": tok(h), "email": e, "password": p}, follow_redirects=True)

print("== usd_to_ngn helper")
with app.test_request_context("/"):
    expect(usd_to_ngn(Decimal("1")) == Decimal("1700.00"), "1 USD = ₦1700 at configured rate")
    expect(usd_to_ngn(Decimal("0.10")) == Decimal("170.00"), "0.10 USD = ₦170")

# No setting → default 1600
with app.app_context():
    db.session.query(SiteSetting).delete(); db.session.commit()
with app.test_request_context("/"):
    expect(usd_to_ngn(Decimal("1")) == Decimal("1600.00"), "no setting → default ₦1600")
# Restore
with app.app_context():
    db.session.add(SiteSetting(key="usd_to_ngn_rate", value="1700")); db.session.commit()

ac = app.test_client(); nc = app.test_client()
print("== templates show Naira")
expect(login(ac, "a@x.com", "x").status_code == 200, "admin login")
r = ac.get("/admin/"); expect(r.status_code == 200, "admin dashboard")
expect(b"\xe2\x82\xa6" in r.data, "naira sign on admin dashboard")  # ₦ utf-8
r = ac.get("/admin/projects"); expect(r.status_code == 200, "admin projects")
expect(b"\xe2\x82\xa6" in r.data, "naira sign on admin projects list")
r = ac.get("/admin/payments"); expect(r.status_code == 200, "admin payments")
expect(b"live rate" in r.data.lower() or b"live rate" in r.data, "payments page shows live FX rate")

expect(login(nc, "n@x.com", "x").status_code == 200, "ann login")
r = nc.get("/annotator/"); expect(r.status_code == 200, "annotator dashboard")
expect(b"\xe2\x82\xa6" in r.data, "naira sign on annotator earnings tile")
r = nc.get("/annotator/history"); expect(r.status_code == 200, "annotator history")
# history might be empty so no ₦ shown on empty state; that's fine

print()
if FAIL:
    print(f"NGN DISPLAY TESTS FAILED ({len(FAIL)}):")
    for f in FAIL: print(" -", f); sys.exit(1)
print("ALL NGN DISPLAY TESTS PASSED")
