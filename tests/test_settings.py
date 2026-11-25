"""Site Settings: USD→NGN rate, pagination, caps, rate limits.

Covers: admin-only access, all keys round-trip, invalid values
rejected, Decimal coercion for USD→NGN, non-admin blocked, audit log
records changes.
"""
from __future__ import annotations
from app import create_app
from models import AuditLog, SiteSetting, User
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


def direct_decimal(key, default):
    row = db.session.get(SiteSetting, key)
    if row is None or not row.value:
        return default
    try:
        return Decimal(str(row.value))
    except Exception:
        return default


def direct_int(key, default):
    row = db.session.get(SiteSetting, key)
    if row is None or not row.value:
        return default
    try:
        return int(str(row.value).strip())
    except Exception:
        return default


app = create_app()
with app.app_context():
    db.drop_all()
    db.create_all()
    adm = User(name="A", email="a@x.com", role="admin")
    adm.set_password("x")
    pm = User(name="P", email="p@x.com", role="project_manager", terms_version="2.1")
    pm.set_password("x")
    db.session.add_all([adm, pm])
    db.session.commit()


def tok(h):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', h)
    return m.group(1) if m else ""


def login(c, e, p):
    h = c.get("/login").get_data(as_text=True)
    return c.post("/login", data={"csrf_token": tok(h), "email": e, "password": p}, follow_redirects=True)


def gt(c, p): return tok(c.get(p).get_data(as_text=True))


ac, pc = app.test_client(), app.test_client()
print("== login + role walls")
expect(login(ac, "a@x.com", "x").status_code == 200, "admin login")
expect(login(pc, "p@x.com", "x").status_code == 200, "pm login")
expect(pc.get("/admin/settings", follow_redirects=False).status_code == 403, "pm blocked /admin/settings")
anon = app.test_client()
expect(anon.get("/admin/settings", follow_redirects=False).status_code in (302,), "anon redirected")

print("== defaults used before any settings saved")
with app.app_context():
    # Pre-seed with defaults to exercise defaults path.
    expect(db.session.get(SiteSetting, "usd_to_ngn_rate") is None, "no rate row initially")

print("== GET renders form")
r = ac.get("/admin/settings")
expect(r.status_code == 200, "settings GET 200")
expect(b"USD" in r.data or b"Naira" in r.data or b"naira" in r.data.lower(), "rate field visible")

print("== POST updates all keys")
payload = {
    "csrf_token": gt(ac, "/admin/settings"),
    "usd_to_ngn_rate": "1700.50",
    "site_name": "Brydl",
    "support_email": "help@brydl.ng",
    "pagination_per_page": "25",
    "upload_batch_limit": "1000",
    "claim_daily_cap": "30",
    "task_max_rejections": "2",
    "preference_min_justification": "50",
    "api_rate_limit_per_minute": "120",
}
r = ac.post("/admin/settings", data=payload, follow_redirects=True)
expect(r.status_code == 200, "settings POST 200")
with app.app_context():
    expect(direct_decimal("usd_to_ngn_rate", Decimal("0")) == Decimal("1700.50"), "rate round-trip")
    expect(direct_int("pagination_per_page", 0) == 25, "pagination round-trip")
    row = db.session.get(SiteSetting, "site_name")
    expect(row and row.value == "Brydl", "site name round-trip")
    cnt = db.session.query(AuditLog).filter_by(action="settings.update").count()
    expect(cnt >= 9, f"audit log rows exist ({cnt})")

print("== invalid rate rejected (stays at previous)")
bad = dict(payload)
bad["csrf_token"] = gt(ac, "/admin/settings")
bad["usd_to_ngn_rate"] = "abc"
ac.post("/admin/settings", data=bad, follow_redirects=True)
with app.app_context():
    expect(direct_decimal("usd_to_ngn_rate", Decimal("0")) == Decimal("1700.50"), "rate unchanged after invalid")

print("== invalid pagination rejected")
bad = dict(payload)
bad["csrf_token"] = gt(ac, "/admin/settings")
bad["pagination_per_page"] = "-5"
ac.post("/admin/settings", data=bad, follow_redirects=True)
with app.app_context():
    expect(direct_int("pagination_per_page", 0) == 25, "pagination unchanged after invalid")

print("== non-admin POST rejected")
bad2 = dict(payload)
bad2["csrf_token"] = gt(pc, "/profile")
bad2["usd_to_ngn_rate"] = "1"
r = pc.post("/admin/settings", data=bad2, follow_redirects=False)
expect(r.status_code in (403, 302), "pm POST blocked")
with app.app_context():
    expect(direct_decimal("usd_to_ngn_rate", Decimal("0")) != Decimal("1"), "rate not changed by pm")

print()
if FAIL:
    print(f"SETTINGS TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL SETTINGS TESTS PASSED")
