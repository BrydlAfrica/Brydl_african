"""Phase 13-E: PM quiz preview + qualified roster."""
from __future__ import annotations
from app import create_app
from models import Project, ProjectAssignment, QualificationQuiz, QuizAttempt, User, utc_now
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
with app.app_context():
    db.drop_all()
    db.create_all()
    pm = User(name="PM", email="pm@x.com", role="project_manager")
    pm.set_password("x")
    ad = User(name="Ad", email="a@x.com", role="admin")
    ad.set_password("x")
    an = User(name="N", email="n@x.com", role="annotator", terms_version="1.0")
    an.set_password("x")
    db.session.add_all([pm, ad, an])
    db.session.flush()
    pr = Project(name="P", client_name="C", task_type="classification",
                 pay_per_task=Decimal("0.10"), instructions="x",
                 created_by=pm.id, approved_by=ad.id, approved_at=utc_now(), status="active")
    db.session.add(pr)
    db.session.flush()
    db.session.add(ProjectAssignment(project_id=pr.id, user_id=pm.id,
                                     role_in_project="project_manager", assigned_by=ad.id))
    qz = QualificationQuiz(project_id=pr.id, title="Quiz", pass_score=80, max_attempts=3,
                           questions_json='[{"q":"1+1?","choices":["1","2","3"],"answer":1}]',
                           status="active", created_by=pm.id, created_at=utc_now(), updated_at=utc_now())
    db.session.add(qz)
    db.session.flush()
    db.session.add(QuizAttempt(quiz_id=qz.id, annotator_id=an.id, status="passed",
                               score_percent=100, answers_json="[]", created_at=utc_now()))
    db.session.add(QuizAttempt(quiz_id=qz.id, annotator_id=an.id, status="failed",
                               score_percent=40, answers_json="[]", created_at=utc_now()))
    db.session.commit()
    pid, qid = pr.id, qz.id


def tok(h):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', h)
    return m.group(1) if m else ""


def login(c, e, p):
    h = c.get("/login").get_data(as_text=True)
    return c.post("/login", data={"csrf_token": tok(h), "email": e, "password": p}, follow_redirects=True)


c = app.test_client()
expect(login(c, "pm@x.com", "x").status_code == 200, "PM login")
r = c.get(f"/pm/projects/{pid}/quiz")
expect(r.status_code == 200, "GET quiz page")
h = r.get_data(as_text=True)
expect("Preview" in h, "preview section present")
expect("1+1?" in h, "question text rendered")
expect("Qualified roster" in h, "roster section")
expect("Recent attempts" in h, "recent attempts section")
expect("passed 100%" in h, "passed attempt shows score")
expect("failed" in h, "failed attempt shows")
# Active quiz → editor fields disabled
expect('disabled' in h, "editor disabled when quiz active")
# Admin-only link not exposed
expect("/admin/" not in h or "/admin/projects/" not in h.split("Qualified roster")[1][:500],
       "no admin route leakage in roster half")
# Save button disabled when active
expect(">Save draft</button>" not in h or 'disabled' in h.split(">Save draft</button>")[0][-80:],
       "Save draft disabled for active quiz")

# Non-PM (annotator) cannot access
c2 = app.test_client()
login(c2, "n@x.com", "x")
r = c2.get(f"/pm/projects/{pid}/quiz")
expect(r.status_code in (403, 302), "annotator blocked from PM quiz view")

print()
if FAIL:
    print(f"PM QUIZ PREVIEW TESTS FAILED ({len(FAIL)}):")
    for f in FAIL:
        print(" -", f)
        sys.exit(1)
print("ALL PM QUIZ PREVIEW/ROSTER TESTS PASSED")
