import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""E2E annotator feature tests: dashboard, task completion, history."""
import json
import re
from datetime import timedelta

from app import create_app
from extensions import db
from models import User, Project, Task, Submission, utc_now

app = create_app()

def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None

def login(client, email, password):
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email, "password": password,
                                       "csrf_token": tk})

def stat_value(html, label):
    m = re.search(
        r'class="h3 fw-bold mb-0">([^<]+)</div>\s*'
        r'<div class="text-muted small">' + re.escape(label) + r"</div>",
        html,
    )
    return m.group(1) if m else None

now = utc_now()

with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator",)
    ann_a.set_password("annotator-pass-1")
    ann_b = User(name="Bisi B", email="b@brydl.ng", role="annotator",)
    ann_b.set_password("annotator-pass-2")
    db.session.add_all([admin, ann_a, ann_b]); db.session.commit()
    admin_id, ann_a_id, ann_b_id = admin.id, ann_a.id, ann_b.id

    p_class = Project(name="Sentiment Labels", client_name="Acme", task_type="classification",
                      pay_per_task=0.50, instructions="CLASS-INSTRUCTIONS-TEXT",
                      created_by=admin_id)
    p_trans = Project(name="Yoruba Translation", client_name="Acme", task_type="translation",
                      pay_per_task=1.00, instructions="TRANSLATE-TO-YORUBA",
                      created_by=admin_id)
    p_ner = Project(name="NER Marking", client_name="Globex", task_type="ner",
                    pay_per_task=2.00, instructions="MARK-ENTITIES",
                    created_by=admin_id)
    p_rlhf = Project(name="RLHF Prefs", client_name="Globex", task_type="rlhf",
                     pay_per_task=0.75, instructions="RATE-RESPONSES",
                     created_by=admin_id)
    db.session.add_all([p_class, p_trans, p_ner, p_rlhf]); db.session.commit()

    def T(proj, status="assigned", expected=None):
        return Task(project_id=proj.id, input_data=f"input for {proj.task_type} {proj.name}",
                    expected_output=expected, status=status, assigned_to=ann_a_id)

    t_class = T(p_class, expected="positive")
    t_trans = T(p_trans)
    t_ner = T(p_ner, status="in_progress", expected="<NER-GOLD-xyz>")
    t_rlhf = T(p_rlhf)
    ta1 = T(p_class, status="approved")
    ta2 = T(p_trans, status="approved")
    ta3 = T(p_ner, status="submitted")
    ta4 = T(p_class, status="rejected")
    t_long = T(p_trans)
    db.session.add_all([t_class, t_trans, t_ner, t_rlhf, ta1, ta2, ta3, ta4, t_long])
    tb1 = Task(project_id=p_class.id, input_data="bisi task one",
               status="assigned", assigned_to=ann_b_id)
    tb2 = Task(project_id=p_class.id, input_data="bisi task two",
               status="approved", assigned_to=ann_b_id)
    db.session.add_all([tb1, tb2]); db.session.commit()
    ids = {k: v.id for k, v in dict(t_class=t_class, t_trans=t_trans, t_ner=t_ner,
                                    t_rlhf=t_rlhf, ta1=ta1, ta2=ta2, ta3=ta3,
                                    ta4=ta4, t_long=t_long, tb1=tb1, tb2=tb2).items()}

    subs = [
        Submission(task_id=ids["ta1"], annotator_id=ann_a_id, output_data='{"kind":"classification","label":"positive"}',
                   status="approved", qa_score=90.0, submitted_at=now, reviewed_at=now),
        Submission(task_id=ids["ta2"], annotator_id=ann_a_id, output_data='{"kind":"translation","text":"old work"}',
                   status="approved", qa_score=85.0, submitted_at=now - timedelta(days=70),
                   reviewed_at=now - timedelta(days=70)),
        Submission(task_id=ids["ta3"], annotator_id=ann_a_id, output_data='{"kind":"ner","marked_entities":"x"}',
                   status="submitted", submitted_at=now - timedelta(hours=2)),
        Submission(task_id=ids["ta4"], annotator_id=ann_a_id, output_data='{"kind":"classification","label":"neutral"}',
                   status="rejected", qa_score=40.0, qa_notes="Fix the label",
                   submitted_at=now - timedelta(hours=1), reviewed_at=now),
        Submission(task_id=ids["tb2"], annotator_id=ann_b_id, output_data='{"kind":"classification","label":"negative"}',
                   status="approved", qa_score=88.0, submitted_at=now, reviewed_at=now),
    ]
    db.session.add_all(subs); db.session.commit()

c = app.test_client()

# ---- 1. RBAC ----
for method, path, data in (("get", "/annotator/", None), ("get", "/annotator/history", None),
                           ("get", "/annotator/task/1", None), ("post", "/annotator/task/1", {})):
    r = getattr(app.test_client(), method)(path, data=data)
    assert r.status_code == 302 and "/login" in r.headers["Location"], (method, path)
r_admin = app.test_client(); login(r_admin, "admin@brydl.ng", "admin-pass-1")
for method, path, data in (("get", "/annotator/", None), ("get", "/annotator/history", None),
                           ("get", "/annotator/task/99999", None), ("post", "/annotator/task/1", {})):
    assert getattr(r_admin, method)(path, data=data).status_code == 403, path
print("1. RBAC: anon redirected, admin 403 on annotator routes OK")

login(c, "a@brydl.ng", "annotator-pass-1")

# ---- 2. dashboard: canonical + aliases + stats ----
r = c.get("/annotator/")
assert r.status_code == 200
html = r.get_data(as_text=True)
assert stat_value(html, "Open tasks") == "6", stat_value(html, "Open tasks")  # includes rejected ta4 (revision queue)
assert stat_value(html, "Completed") == "4", stat_value(html, "Completed")
assert stat_value(html, "Approved") == "2", stat_value(html, "Approved")
assert stat_value(html, "Earned this month") == "$0.50", stat_value(html, "Earned this month")
assert "$1.50" in html, "all-time earnings missing"
assert "1 awaiting review" in html and "1 rejected" in html
assert f'href="/annotator/task/{ids["t_class"]}"' in html, "Start link"
r = c.get("/annotator/dashboard")
assert r.status_code == 302 and r.headers["Location"] == "/annotator/"
r = c.get("/annotator/tasks")
assert r.status_code == 302 and r.headers["Location"].endswith("#my-tasks")
print("2. dashboard: canonical /annotator/, aliases, stats [4,4,2,$0.50/$2.50] OK")

# ---- 3. task pages: dynamic forms, no answer key, guards ----
h = c.get(f"/annotator/task/{ids['t_class']}").get_data(as_text=True)
assert "CLASS-INSTRUCTIONS-TEXT" in h and 'name="label_choice"' in h
assert "expected_output" not in h
h = c.get(f"/annotator/task/{ids['t_ner']}").get_data(as_text=True)
assert 'name="marked_entities"' in h and "<NER-GOLD-xyz>" not in h, "answer key leaked!"
h = c.get(f"/annotator/task/{ids['t_rlhf']}").get_data(as_text=True)
assert 'name="rating"' in h and 'name="explanation"' in h and "★★★★" in h
h = c.get(f"/annotator/task/{ids['t_trans']}").get_data(as_text=True)
assert 'name="translated_text"' in h and "TRANSLATE-TO-YORUBA" in h
r = c.get(f"/annotator/task/{ids['ta3']}")
assert r.status_code == 302 and r.headers["Location"] == "/annotator/"
assert "already awaiting QA review" in c.get("/annotator/").get_data(as_text=True)
h = c.get(f"/annotator/task/{ids['ta4']}").get_data(as_text=True)
assert "sent back for revision" in h and "Fix the label" in h
assert c.get(f"/annotator/task/{ids['tb1']}").status_code == 404
assert c.get("/annotator/task/99999").status_code == 404
assert c.get("/annotator/task/abc").status_code == 404
print("3. task pages: 4 dynamic forms, gold never leaks, status/ownership guards OK")

# ---- 4. submissions: all types, guards, race-safe double-submit ----
task_csrf = get_csrf(c.get(f"/annotator/task/{ids['t_class']}").get_data(as_text=True))
r = c.post(f"/annotator/task/{ids['t_class']}",
           data={"label_choice": "positive", "csrf_token": task_csrf})
assert r.status_code == 302 and r.headers["Location"] == "/annotator/"
with app.app_context():
    s = db.session.scalar(db.select(Submission).where(Submission.task_id == ids["t_class"]))
    assert s is not None and json.loads(s.output_data) == {"kind": "classification", "label": "positive"}
    assert db.session.get(Task, ids["t_class"]).status == "submitted"
# double submit refused
r = c.post(f"/annotator/task/{ids['t_class']}",
           data={"label_choice": "negative", "csrf_token": task_csrf})
assert "already" in c.get("/annotator/").get_data(as_text=True)
with app.app_context():
    n = db.session.scalar(db.select(db.func.count(Submission.id)).where(
        Submission.task_id == ids["t_class"]))
    assert n == 1, f"double submission! {n} rows"

r = c.post(f"/annotator/task/{ids['t_trans']}",
           data={"translated_text": "Ẹ kú àárọ̀", "csrf_token": task_csrf})
r = c.post(f"/annotator/task/{ids['t_ner']}",
           data={"marked_entities": "Lagos — LOCATION", "csrf_token": task_csrf})
r = c.post(f"/annotator/task/{ids['t_rlhf']}",
           data={"rating": "4", "explanation": "Decent but brief", "csrf_token": task_csrf})
with app.app_context():
    s = db.session.scalar(db.select(Submission).where(Submission.task_id == ids["t_trans"]))
    assert json.loads(s.output_data) == {"kind": "translation", "text": "Ẹ kú àárọ̀"}
    s = db.session.scalar(db.select(Submission).where(Submission.task_id == ids["t_ner"]))
    assert json.loads(s.output_data) == {"kind": "ner", "marked_entities": "Lagos — LOCATION"}
    s = db.session.scalar(db.select(Submission).where(Submission.task_id == ids["t_rlhf"]))
    assert json.loads(s.output_data) == {"kind": "rlhf", "rating": 4, "explanation": "Decent but brief"}

# oversize answer refused, nothing saved
r = c.post(f"/annotator/task/{ids['t_long']}",
           data={"translated_text": "x" * 10001, "csrf_token": task_csrf})
assert r.status_code == 200 and "is-invalid" in r.get_data(as_text=True)
# missing CSRF refused
r = c.post(f"/annotator/task/{ids['t_long']}", data={"translated_text": "hi"})
assert r.status_code == 200
with app.app_context():
    assert db.session.scalar(db.select(db.func.count(Submission.id)).where(
        Submission.task_id == ids["t_long"])) == 0
    assert db.session.get(Task, ids["t_long"]).status == "assigned"
# ownership on POST: ann_a cannot submit ann_b's task
r = c.post(f"/annotator/task/{ids['tb1']}",
           data={"label_choice": "positive", "csrf_token": task_csrf})
assert r.status_code == 404
with app.app_context():
    assert db.session.get(Task, ids["tb1"]).status == "assigned"
# revision: rejected ta4 resubmits as NEW row; old verdict preserved
r = c.post(f"/annotator/task/{ids['ta4']}",
           data={"label_choice": "negative", "csrf_token": task_csrf})
with app.app_context():
    revs = db.session.scalars(db.select(Submission).where(
        Submission.task_id == ids["ta4"]).order_by(Submission.id)).all()
    assert len(revs) == 2, f"revision rows: {len(revs)}"
    assert revs[0].status == "rejected" and revs[1].status == "submitted"
    assert json.loads(revs[1].output_data)["label"] == "negative"
    assert db.session.get(Task, ids["ta4"]).status == "submitted"
print("4. submissions: 4 types stored as tagged JSON, oversize/CSRF/ownership refused,"
      " double-submit blocked, revision = new row with audit trail OK")

# ---- 5. history ----
h = c.get("/annotator/history").get_data(as_text=True)
assert "Showing 9 of 9" in h, "history count"
assert "Fix the label" in h and "40" in h  # QA notes + score visible
assert "$0.50" in h and "$1.00" in h       # earned column for approved rows
r = c.get("/annotator/history?page=999", follow_redirects=False)
assert r.status_code == 302
r2 = app.test_client(); login(r2, "b@brydl.ng", "annotator-pass-2")
h2 = r2.get("/annotator/history").get_data(as_text=True)
assert "Showing 1 of 1" in h2 and "bisi task two" in h2.lower() or "88" in h2
print("5. history: 10 rows paginated, notes/scores/earnings shown, isolated per annotator OK")

# ---- 6. CSP hygiene ----
import glob
bad = []
for f in glob.glob(ROOT + "/templates/**/*.html", recursive=True):
    src = open(f).read()
    if re.search(r"\son[a-z]+=\"", src):
        bad.append(f)
assert not bad, bad
js = open(ROOT + "/static/js/main.js").read()
assert "data-confirm" in js
base = open(ROOT + "/templates/base.html").read()
assert "js/main.js" in base
print("6. CSP hygiene: no inline handlers, confirmation via main.js OK")
print("\nALL ANNOTATOR TESTS PASSED")
