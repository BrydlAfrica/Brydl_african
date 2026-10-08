"""Phase 14: Hausa special-character toolbar on annotator task page."""
from __future__ import annotations
from app import create_app
from models import Project, Task, User, utc_now
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
tid = None
with app.app_context():
    db.drop_all()
    db.create_all()
    ad = User(name="A", email="a@x.com", role="admin")
    ad.set_password("x")
    an = User(name="N", email="n@x.com", role="annotator", terms_version="1.0")
    an.set_password("x")
    db.session.add_all([ad, an])
    db.session.flush()
    pr = Project(name="P", client_name="C", task_type="classification",
                 pay_per_task=Decimal("0.05"), instructions="x",
                 created_by=ad.id, approved_by=ad.id, approved_at=utc_now(),
                 status="active")
    db.session.add(pr)
    db.session.flush()
    t = Task(project_id=pr.id, input_data='{"html":"Q","type":"text"}',
             status="assigned", assigned_to=an.id)
    db.session.add(t)
    db.session.flush()
    tid = t.id
    db.session.commit()


def tok(h):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', h)
    return m.group(1) if m else ""


c = app.test_client()
r = c.get("/login")
c.post("/login", data={"csrf_token": tok(r.get_data(as_text=True)),
                       "email": "n@x.com", "password": "x"},
       follow_redirects=True)
r = c.get(f"/annotator/task/{tid}")
expect(r.status_code == 200, "task page 200")
h = r.get_data(as_text=True)
for ch in ["ɗ", "ƙ", "ɓ", "ƴ", "ʼ", "Ɗ", "Ƙ", "Ɓ", "Ƴ"]:
    expect(ch in h, f"toolbar contains '{ch}'")
expect("Hausa special chars" in h, "toolbar label present")
# No inline event handlers (CSP hygiene preserved by the IIFE + addEventListener)
expect(not re.search(r'\son[a-z]+="', h), "no inline event handlers")
# CSP test from test_system_check pattern passes: toolbar uses addEventListener
expect("char-btn" in h, "char buttons rendered")
print()
if FAIL:
    print(f"CHAR BAR TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL HAUSA CHAR-BAR TESTS PASSED")
