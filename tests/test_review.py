import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""E2E QA review feature tests."""
import json
import re

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

with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann_a.set_password("annotator-pass-1")
    ann_b = User(name="Bisi B", email="b@brydl.ng", role="annotator")
    ann_b.set_password("annotator-pass-2")
    db.session.add_all([admin, ann_a, ann_b]); db.session.commit()
    ann_a.record_consent()
    ann_b.record_consent()
    db.session.commit()
    admin_id, ann_a_id, ann_b_id = admin.id, ann_a.id, ann_b.id

    p1 = Project(name="Gold Project", client_name="Acme", task_type="classification",
                 pay_per_task=0.50, instructions="i", created_by=admin_id)
    p2 = Project(name="No Gold Project", client_name="Globex", task_type="translation",
                 pay_per_task=1.00, instructions="i", created_by=admin_id)
    db.session.add_all([p1, p2]); db.session.commit()
    p1_id, p2_id = p1.id, p2.id

    def make(sub_key, project, annotator_id, inp, gold=None):
        t = Task(project_id=project.id, input_data=inp,
                 expected_output=gold, status="submitted",
                 assigned_to=annotator_id)
        db.session.add(t); db.session.flush()
        s = Submission(task_id=t.id, annotator_id=annotator_id,
                       output_data=json.dumps({"kind": "text", "text": f"answer {sub_key}"}))
        db.session.add(s); db.session.flush()
        return t.id, s.id

    pairs = {}
    pairs["s1"] = make("s1", p1, ann_a_id, "input one with gold", gold="GOLD-STANDARD-ONE")
    pairs["s2"] = make("s2", p2, ann_a_id, "input two no gold")
    pairs["s3"] = make("s3", p1, ann_b_id, "input three with gold", gold="GOLD-THREE")
    pairs["s4"] = make("s4", p2, ann_a_id, "input four bulk approve")
    pairs["s5"] = make("s5", p1, ann_a_id, "input five bulk reject")
    pairs["s6"] = make("s6", p1, ann_b_id, "input six bulk reject strip")
    db.session.commit()
    # already-reviewed pair (must never appear in the queue)
    t7 = Task(project_id=p1.id, input_data="done deal", status="approved",
              assigned_to=ann_a_id)
    db.session.add(t7); db.session.flush()
    db.session.add(Submission(task_id=t7.id, annotator_id=ann_a_id,
                              output_data='{"kind":"text","text":"done"}',
                              status="approved", qa_score=95.0,
                              reviewed_at=utc_now()))
    db.session.commit()
    ids = {k: v for k, v in pairs.items()}

c = app.test_client()

# ---- 1. RBAC ----
for method, path, data in (("get", "/admin/review", None),
                           ("get", "/admin/review/1", None),
                           ("post", "/admin/review/1", {}),
                           ("post", "/admin/review/bulk", {})):
    r = getattr(app.test_client(), method)(path, data=data)
    assert r.status_code == 302 and "/login" in r.headers["Location"], (method, path)
r_ann = app.test_client(); login(r_ann, "a@brydl.ng", "annotator-pass-1")
for method, path, data in (("get", "/admin/review", None),
                           ("get", "/admin/review/1", None),
                           ("post", "/admin/review/1", {}),
                           ("post", "/admin/review/bulk", {})):
    assert getattr(r_ann, method)(path, data=data).status_code == 403, path
print("1. RBAC: anon redirected, annotator 403 on all review routes OK")

login(c, "admin@brydl.ng", "admin-pass-1")

# ---- 2. queue ----
r = c.get("/admin/review")
assert r.status_code == 200
html = r.get_data(as_text=True)
assert "Showing 6 of 6" in html, "queue count"
assert "done deal" not in html, "reviewed submission leaked into queue!"
for needle in ("Tunde A", "Bisi B", "Gold Project", "No Gold Project",
               "input one with gold", "answer s3"):
    assert needle in html, needle
s1_url = f'/admin/review/{ids["s1"][1]}"'
assert s1_url in html, "Review link"
# oldest first: s1 appears before s3
assert html.find("input one") < html.find("input three"), "queue order"
# filters
h = c.get(f"/admin/review?project={p1_id}").get_data(as_text=True)
assert "Showing 4 of 4" in h and "No Gold Project" not in h.split("filter-project")[1][:2000]
h = c.get(f"/admin/review?annotator={ann_b_id}").get_data(as_text=True)
assert "Showing 2 of 2" in h and "Tunde A" not in h.split("filter-annotator")[1][:2000]
h = c.get("/admin/review?project=abc&annotator=xyz").get_data(as_text=True)
assert "Showing 6 of 6" in h, "junk filter changed results"
print("2. queue: only submitted, oldest-first, filters, junk-param safety OK")

# ---- 3. detail GET ----
r = c.get(f"/admin/review/{ids['s1'][1]}")
assert r.status_code == 200
h = r.get_data(as_text=True)
assert "input one with gold" in h and "GOLD-STANDARD-ONE" in h
assert "answer s1" in h and "Tunde A" in h
assert 'type="range"' in h and "form-range" in h
assert 'name="action" value="approve"' in h and 'name="action" value="reject"' in h
# no-gold detail shows the note instead
h = c.get(f"/admin/review/{ids['s2'][1]}").get_data(as_text=True)
assert "No gold standard" in h
# already-reviewed redirects
r = c.get(f"/admin/review/{ids['s1'][1]}")  # still submitted
assert r.status_code == 200
print("3. detail: side-by-side with gold, slider, no-gold fallback OK")

# ---- 4. single approve ----
csrf = get_csrf(c.get(f"/admin/review/{ids['s1'][1]}").get_data(as_text=True))
r = c.post(f"/admin/review/{ids['s1'][1]}",
           data={"action": "approve", "qa_score": "90",
                 "qa_notes": "Great work", "csrf_token": csrf})
assert r.status_code == 302 and r.headers["Location"] == "/admin/review"
fl = c.get("/admin/review").get_data(as_text=True)
assert "approved" in fl and "Showing 5 of 5" in fl
with app.app_context():
    s = db.session.get(Submission, ids["s1"][1])
    assert s.status == "approved" and s.qa_score == 90.0
    assert s.qa_notes == "Great work" and s.reviewed_at is not None
    assert db.session.get(Task, ids["s1"][0]).status == "approved"
# double review refused
r = c.post(f"/admin/review/{ids['s1'][1]}",
           data={"action": "reject", "qa_score": "10", "csrf_token": csrf})
fl = c.get("/admin/review").get_data(as_text=True)
assert "already reviewed" in fl
with app.app_context():
    assert db.session.get(Submission, ids["s1"][1]).status == "approved"
print("4. single approve: fields persisted, task approved, re-review refused OK")

# ---- 5. single reject WITH revision ----
r = c.get(f"/admin/review/{ids['s2'][1]}")
csrf = get_csrf(r.get_data(as_text=True))
r = c.post(f"/admin/review/{ids['s2'][1]}",
           data={"action": "reject", "qa_score": "35", "qa_notes": "Tone off",
                 "return_for_revision": "y", "csrf_token": csrf})
fl = c.get("/admin/review").get_data(as_text=True)
assert "rejected" in fl
with app.app_context():
    s = db.session.get(Submission, ids["s2"][1])
    assert s.status == "rejected" and s.qa_score == 35.0 and s.qa_notes == "Tone off"
    t = db.session.get(Task, ids["s2"][0])
    assert t.status == "rejected" and t.assigned_to == ann_a_id
# annotator sees it in queue with revise button and QA banner
r2 = app.test_client(); login(r2, "a@brydl.ng", "annotator-pass-1")
h2 = r2.get("/annotator/").get_data(as_text=True)
assert "Revise needed" in h2 and f'/annotator/task/{ids["s2"][0]}' in h2
h2 = r2.get(f"/annotator/task/{ids['s2'][0]}").get_data(as_text=True)
assert "sent back for revision" in h2 and "Tone off" in h2
print("5. reject w/ revision: task stays owned, annotator queue + banner OK")

# ---- 6. single reject WITHOUT revision + requeue via unassign ----
r = c.get(f"/admin/review/{ids['s3'][1]}")
csrf = get_csrf(r.get_data(as_text=True))
r = c.post(f"/admin/review/{ids['s3'][1]}",
           data={"action": "reject", "qa_score": "20", "csrf_token": csrf})
with app.app_context():
    t = db.session.get(Task, ids["s3"][0])
    # No-revision reject returns the task to the pending pool (the
    # ck_tasks_assignment invariant forbids rejected-but-unowned).
    assert t.status == "pending" and t.assigned_to is None
# a rejected OWNED task (from test 5) can still be requeued via unassign
csrf_tasks = get_csrf(c.get("/admin/tasks").get_data(as_text=True))
c.post(f"/admin/tasks/{ids['s2'][0]}/unassign", data={"csrf_token": csrf_tasks})
with app.app_context():
    t = db.session.get(Task, ids["s2"][0])
    assert t.status == "pending" and t.assigned_to is None
print("6. reject w/o revision: back to pending pool; unassign requeues owned rejects OK")

# ---- 7. bulk ----
csrf = get_csrf(c.get("/admin/review").get_data(as_text=True))
r = c.post("/admin/review/bulk", data={
    "action": "approve", "qa_score": "100",
    "submission_ids": [str(ids["s4"][1])], "csrf_token": csrf})
fl = c.get("/admin/review").get_data(as_text=True)
assert "1 submission(s) approved" in fl
with app.app_context():
    s = db.session.get(Submission, ids["s4"][1])
    assert s.status == "approved" and s.qa_score == 100.0 and s.qa_notes is None
    assert db.session.get(Task, ids["s4"][0]).status == "approved"
r = c.post("/admin/review/bulk", data={
    "action": "reject", "qa_score": "25", "return_for_revision": "y",
    "submission_ids": [str(ids["s5"][1]), str(ids["s6"][1]), "99999"],
    "csrf_token": csrf})
fl = c.get("/admin/review").get_data(as_text=True)
assert "2 submission(s) rejected" in fl and "1 skipped" in fl
with app.app_context():
    s5 = db.session.get(Submission, ids["s5"][1])
    s6 = db.session.get(Submission, ids["s6"][1])
    assert s5.status == "rejected" and s6.status == "rejected"
    t5 = db.session.get(Task, ids["s5"][0])
    t6 = db.session.get(Task, ids["s6"][0])
    assert t5.status == "rejected" and t5.assigned_to == ann_a_id
    assert t6.status == "rejected" and t6.assigned_to == ann_b_id
print("7. bulk: approve + reject with skips, revision flag respected OK")

# ---- 8. bulk guards ----
r = c.post("/admin/review/bulk", data={"action": "approve", "qa_score": "100",
                                       "csrf_token": csrf})
fl = c.get("/admin/review").get_data(as_text=True)
assert "Tick at least one submission" in fl
r = c.post("/admin/review/bulk", data={"action": "approve", "qa_score": "150",
                                       "submission_ids": [str(ids["s4"][1])],
                                       "csrf_token": csrf})
fl = c.get("/admin/review").get_data(as_text=True)
assert "between 0 and 100" in fl
r = c.post("/admin/review/bulk", data={"action": "approve", "qa_score": "100",
                                       "submission_ids": [str(ids["s4"][1])]})
fl = c.get("/admin/review").get_data(as_text=True)
assert "Security check failed" in fl
with app.app_context():
    assert db.session.get(Submission, ids["s4"][1]).status == "approved"  # unchanged
print("8. bulk guards: empty selection, bad score, CSRF refused OK")

# ---- 9. misc ----
assert c.get("/admin/review/99999").status_code == 404
assert c.get("/admin/review/abc").status_code == 404
import glob
bad = []
for f in glob.glob(ROOT + "/templates/**/*.html", recursive=True):
    src = open(f).read()
    if re.search(r"\son[a-z]+=\"", src):
        bad.append(f)
assert not bad, bad
print("9. 404s + template CSP hygiene OK")
print("\nALL QA REVIEW TESTS PASSED")
