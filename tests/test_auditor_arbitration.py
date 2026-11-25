"""Phase 13-F: auditor arbitration polish."""
from __future__ import annotations
from models import (ConsensusGroup, Project,
                    ProjectAssignment, Submission, Task, User, utc_now)
from extensions import db
from app import create_app
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
aid = aid2 = rid = None
with app.app_context():
    db.drop_all()
    db.create_all()
    ad = User(name="Ad", email="a@x.com", role="admin")
    ad.set_password("x")
    au = User(name="Au", email="au@x.com", role="auditor")
    au.set_password("x")
    an = User(name="N", email="n@x.com", role="annotator", terms_version="2.1")
    an2 = User(name="M", email="m@x.com", role="annotator", terms_version="2.1")
    an.set_password("x")
    an2.set_password("x")
    db.session.add_all([ad, au, an, an2])
    db.session.flush()
    pr = Project(name="P", client_name="C", task_type="classification",
                 pay_per_task=Decimal("0.10"), instructions="x",
                 created_by=ad.id, approved_by=ad.id, approved_at=utc_now(),
                 status="active")
    db.session.add(pr)
    db.session.flush()
    db.session.add(ProjectAssignment(project_id=pr.id, user_id=ad.id,
                                     role_in_project="project_manager",
                                     assigned_by=ad.id))

    annotators = [an, an2]

    def _make_group(status, labels, decided=None, note=None,
                    resolved_by=None):
        g = ConsensusGroup(project_id=pr.id, status=status,
                           required_count=len(labels), created_at=utc_now())
        if decided is not None:
            g.decided_choice = decided
            g.decided_note = note
            g.resolved_by = resolved_by
            g.resolved_at = utc_now()
        db.session.add(g)
        db.session.flush()
        for i, label in enumerate(labels):
            ann_i = annotators[i % len(annotators)]
            t = Task(project_id=pr.id, consensus_group_id=g.id,
                     input_data='{"html":"Q","type":"text"}',
                     status="assigned", assigned_to=ann_i.id)
            db.session.add(t)
            db.session.flush()
            db.session.add(Submission(
                task_id=t.id, annotator_id=ann_i.id, status="submitted",
                output_data=f'{{"label":"{label}"}}',
                submitted_at=utc_now(),
            ))
        return g.id

    aid = _make_group("conflict", ["cat", "dog"])
    aid2 = _make_group("conflict", ["bird", "bird"])
    rid = _make_group("resolved", ["fish", "fish"],
                      decided="fish", note="obviously a fish",
                      resolved_by=au.id)
    db.session.commit()


def tok(h):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', h)
    return m.group(1) if m else ""


def login(c, e, p):
    h = c.get("/login").get_data(as_text=True)
    return c.post("/login", data={"csrf_token": tok(h), "email": e,
                                  "password": p}, follow_redirects=True)


ac = app.test_client()
print("== auditor navigation")
expect(login(ac, "au@x.com", "x").status_code == 200, "auditor login")
r = ac.get("/admin/")
expect(r.status_code == 302 and "/review/conflicts" in r.headers["Location"],
       "auditor redirected to conflicts queue from /admin/")

print("== conflicts list page")
r = ac.get("/admin/review/conflicts")
expect(r.status_code == 200, "conflicts page 200")
h = r.get_data(as_text=True)
expect("Conflict" in h and "Pending" in h, "filter tabs present")
expect("need review" in h, "needs-attention banner present")
expect('class="badge' in h, "count badges rendered")

print("== arbitrate GET shows verdicts + ruling form")
r = ac.get(f"/admin/review/conflicts/{aid}")
expect(r.status_code == 200, "arbitrate GET 200")
h = r.get_data(as_text=True)
expect("cat" in h and "dog" in h, "both verdict choices visible")
expect("Your ruling" in h, "ruler header shown")
expect("decided_choice" in h, "ruler form present")
expect("Binding ruling" not in h, "read-only label absent on live round")

print("== in-agreement banner shown when all answers match")
r = ac.get(f"/admin/review/conflicts/{aid2}")
expect(r.status_code == 200, "agreed-group GET 200")
expect("same" in r.get_data(as_text=True), "in-agreement alert appears")

print("== POST ruling records decision and advances to next")
r = ac.get(f"/admin/review/conflicts/{aid}")
token = tok(r.get_data(as_text=True))
r = ac.post(f"/admin/review/conflicts/{aid}",
            data={"csrf_token": token, "decided_choice": "cat",
                  "decided_note": "Looks like a cat."},
            follow_redirects=False)
expect(r.status_code in (302, 303), "POST ruling redirects")
loc = r.headers.get("Location", "")
expect(str(aid2) in loc, f"redirects to next pending group (got {loc})")
with app.app_context():
    g = db.session.get(ConsensusGroup, aid)
    expect(g.status == "resolved", "group marked resolved")
    expect(g.decided_choice == "cat", "decided_choice saved")
    expect(g.decided_note and "cat" in g.decided_note, "note saved")

print("== resolved round redirects by default; read-only via ?view=1")
r = ac.get(f"/admin/review/conflicts/{rid}")
expect(r.status_code == 302, "resolved GET redirects by default")
r = ac.get(f"/admin/review/conflicts/{rid}?view=1", follow_redirects=True)
expect(r.status_code == 200, "resolved GET with ?view=1 renders read-only")
h = r.get_data(as_text=True)
expect("Binding ruling" in h, "read-only ruling section present")
expect("Record binding ruling" not in h, "submit button hidden on resolved")
expect("fish" in h and "obviously a fish" in h, "saved choice + note visible")
expect("Resolved by" in h and "Au" in h, "resolver name shown")
expect("All rounds" in h, "navigation present")

print("== resolved round is immutable (POST does not overwrite)")
r = ac.get(f"/admin/review/conflicts/{rid}?view=1")
token = tok(r.get_data(as_text=True))
r = ac.post(f"/admin/review/conflicts/{rid}",
            data={"csrf_token": token, "decided_choice": "whale"},
            follow_redirects=False)
expect(r.status_code in (200, 302), "re-POST on resolved does not 500")
with app.app_context():
    g = db.session.get(ConsensusGroup, rid)
    expect(g.decided_choice == "fish", "resolved decision unchanged")

print()
if FAIL:
    print(f"AUDITOR ARBITRATION TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL AUDITOR ARBITRATION TESTS PASSED")
