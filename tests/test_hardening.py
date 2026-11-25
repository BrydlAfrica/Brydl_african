import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Hardening tests: error pages, textarea caps, paused-project assign
guard, empty-DB graceful rendering, loading-state assets."""
import re

from app import create_app
from extensions import db
from models import User, Project, Task, Submission

app = create_app()

# 500-test route (unhandled exception) BEFORE any request.
def _boom():
    raise RuntimeError("boom-test")


app.add_url_rule("/boom", "boom", _boom)


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
    db.session.add_all([admin, ann_a, ann_b])
    db.session.commit()
    ann_a.record_consent()
    ann_b.record_consent()
    db.session.commit()
    admin_id, ann_a_id, ann_b_id = admin.id, ann_a.id, ann_b.id

c = app.test_client()

# ---- 1. empty database renders gracefully (no projects/tasks yet) ----
login(c, "admin@brydl.ng", "admin-pass-1")
checks = [
    ("/admin/", "the queue is clear"),
    ("/admin/projects", "No projects yet"),
    ("/admin/tasks", "No tasks match the current filters."),
    ("/admin/review", "nothing is waiting on you"),
    ("/admin/payments", "No payments generated yet"),
]
for path, needle in checks:
    r = c.get(path)
    assert r.status_code == 200, (path, r.status_code)
    assert needle in r.get_data(as_text=True), (path, needle)
# upload lives on the tasks page; the bare path is a canonical redirect
r = c.get("/admin/tasks/upload")
assert r.status_code == 302, (r.status_code, r.headers.get("Location"))
assert "/admin/tasks" in r.headers["Location"]
# POSTing an upload with zero active projects is refused gracefully
tk0 = get_csrf(c.get("/admin/tasks").get_data(as_text=True))
r = c.post("/admin/tasks/upload", data={"csrf_token": tk0})
assert r.status_code == 302 and "/admin/projects" in r.headers["Location"]
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "Create an active project before uploading tasks." in fl
r_ann = app.test_client()
login(r_ann, "a@brydl.ng", "annotator-pass-1")
for path in ("/annotator/", "/annotator/history"):
    r = r_ann.get(path)
    assert r.status_code == 200, (path, r.status_code)
assert "No open tasks right now" in r_ann.get("/annotator/").get_data(as_text=True)
print("1. empty DB: every page renders with a 'no data' message OK")

# ---- 2. custom error pages (all extend base.html) ----
anon = app.test_client()
r = anon.get("/definitely-not-a-page")
assert r.status_code == 404
html = r.get_data(as_text=True)
assert "Page not found" in html and "All times shown in UTC" in html
assert 'class="navbar' in html, "must extend base.html"
r = c.get("/admin/also-fake")
assert r.status_code == 404 and "Page not found" in r.get_data(as_text=True)
# 403: annotator in admin area
r = r_ann.get("/admin/")
assert r.status_code == 403
html = r.get_data(as_text=True)
assert "Access denied" in html and "All times shown in UTC" in html
# anon on admin area is a login redirect, NOT a 403 (unchanged)
r = anon.get("/admin/payments")
assert r.status_code == 302 and "/login" in r.headers["Location"]
# 500: unhandled exception renders the custom page, leaks nothing
r = c.get("/boom")
assert r.status_code == 500
html = r.get_data(as_text=True)
assert "Something went wrong" in html
assert "RuntimeError" not in html and "Traceback" not in html
assert "boom-test" not in html
print("2. error pages: 404/403/500 custom + base.html shell, no leaks OK")

# ---- seed for caps + assign tests ----
with app.app_context():
    proj_tr = Project(name="Trans Project", client_name="Acme",
                      task_type="translation", pay_per_task=0.50,
                      instructions="Translate the text.",
                      created_by=admin_id, status="active")
    proj_paused = Project(name="Paused Proj", client_name="Globex",
                          task_type="classification", pay_per_task=0.10,
                          instructions="i", created_by=admin_id,
                          status="paused")
    proj_active = Project(name="Active Proj", client_name="Globex",
                          task_type="classification", pay_per_task=0.10,
                          instructions="i", created_by=admin_id,
                          status="active")
    db.session.add_all([proj_tr, proj_paused, proj_active])
    db.session.flush()
    proj_tr_id, proj_paused_id, proj_active_id = (proj_tr.id,
                                                  proj_paused.id,
                                                  proj_active.id)
    t_tr = Task(project_id=proj_tr_id, input_data="hello world",
                status="assigned", assigned_to=ann_a_id)
    t_p1 = Task(project_id=proj_paused_id, input_data="paused 1")
    t_p2 = Task(project_id=proj_paused_id, input_data="paused 2")
    t_a1 = Task(project_id=proj_active_id, input_data="active 1")
    db.session.add_all([t_tr, t_p1, t_p2, t_a1])
    db.session.commit()
    t_tr_id, t_p1_id, t_p2_id, t_a1_id = t_tr.id, t_p1.id, t_p2.id, t_a1.id
print("   seeded: translation task (assigned), paused project x2 pending,"
      " active project x1 pending")
assert "tasks_data" in c.get(
    "/admin/tasks?upload_open=1").get_data(as_text=True)

# ---- 3. textarea caps: 10,000 characters ----
html = c.get("/admin/projects/new").get_data(as_text=True)
assert 'maxlength="10000"' in html, "client-side cap on instructions"
csrf = get_csrf(html)
r = c.post("/admin/projects/new", data={
    "name": "Over Cap", "client_name": "X", "task_type": "translation",
    "required_annotations": "1",
    "pay_per_task": "0.50", "instructions": "x" * 10001, "csrf_token": csrf})
assert r.status_code == 200
assert "Instructions must be 10,000 characters or less." in r.get_data(
    as_text=True)
with app.app_context():
    assert db.session.scalar(db.select(
        db.func.count(Project.id)).where(Project.name == "Over Cap")) == 0
# exactly 10,000 is accepted
r = c.post("/admin/projects/new", data={
    "name": "Exact Cap", "client_name": "X", "task_type": "translation",
    "required_annotations": "1",
    "pay_per_task": "0.50", "instructions": "y" * 10000, "csrf_token": csrf})
assert r.status_code == 302
with app.app_context():
    p = db.session.scalar(db.select(Project).where(Project.name == "Exact Cap"))
    assert p is not None and len(p.instructions) == 10000
# annotator answer cap: 10,001 rejected client- AND server-side
html = r_ann.get(f"/annotator/task/{t_tr_id}").get_data(as_text=True)
assert 'maxlength="10000"' in html
tk = get_csrf(html)
r = r_ann.post(f"/annotator/task/{t_tr_id}", data={
    "translated_text": "z" * 10001, "csrf_token": tk})
assert r.status_code == 200
assert "Translation must be 10000 characters or less." in r.get_data(
    as_text=True)
with app.app_context():
    assert db.session.scalar(db.select(db.func.count(Submission.id))) == 0
    assert db.session.get(Task, t_tr_id).status == "assigned"
# exactly 10,000 accepted -> submission created
r = r_ann.post(f"/annotator/task/{t_tr_id}", data={
    "translated_text": "w" * 10000, "csrf_token": tk})
assert r.status_code == 302
with app.app_context():
    assert db.session.scalar(db.select(db.func.count(Submission.id))) == 1
    assert db.session.get(Task, t_tr_id).status == "submitted"
print("3. caps: instructions & answers rejected at 10,001, accepted at"
      " 10,000; maxlength attributes present OK")

# ---- 4. paused-project assignment guard ----
html = c.get("/admin/tasks").get_data(as_text=True)
tk = get_csrf(html)
# mixed selection: 1 active-project task + 2 paused-project tasks
r = c.post("/admin/tasks/assign", data={
    "task_ids": [str(t_a1_id), str(t_p1_id), str(t_p2_id)],
    "annotator_id": str(ann_b_id), "csrf_token": tk})
assert r.status_code == 302
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "1 task(s) assigned to Bisi B." in fl, fl
assert "2 skipped (their project is not active)." in fl
with app.app_context():
    assert db.session.get(Task, t_a1_id).assigned_to == ann_b_id
    assert db.session.get(Task, t_a1_id).status == "assigned"
    assert db.session.get(Task, t_p1_id).status == "pending"
    assert db.session.get(Task, t_p1_id).assigned_to is None
    assert db.session.get(Task, t_p2_id).assigned_to is None
# only paused-project tasks -> nothing assigned
r = c.post("/admin/tasks/assign", data={
    "task_ids": [str(t_p1_id), str(t_p2_id)],
    "annotator_id": str(ann_b_id), "csrf_token": tk})
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "0 task(s) assigned to Bisi B." in fl
assert "2 skipped (their project is not active)." in fl
with app.app_context():
    assert db.session.get(Task, t_p1_id).status == "pending"
print("4. paused-project guard: only active-project tasks assigned,"
      " blocked ones reported OK")

# ---- 5. annotator cannot open someone else's task (custom 404) ----
with app.app_context():
    t_other = Task(project_id=proj_active_id, input_data="not yours",
                   status="assigned", assigned_to=ann_a_id)
    db.session.add(t_other)
    db.session.commit()
    t_other_id = t_other.id
r = r_ann.get(f"/annotator/task/{t_other_id}")  # r_ann is Tunde A: owns it
assert r.status_code == 200
r = app.test_client()
login(r, "b@brydl.ng", "annotator-pass-2")
r = r.get(f"/annotator/task/{t_other_id}")  # Bisi B: does not
assert r.status_code == 404
assert "Page not found" in r.get_data(as_text=True)
print("5. foreign task access: 404 (anti-probe) with custom page OK")

# ---- 6. loading states: assets + CSP hygiene ----
css = open(ROOT + "/static/css/style.css").read()
assert 'data-loading="true"' in css and "@keyframes brydl-spin" in css
base = open(ROOT + "/templates/base.html").read()
assert 'css/style.css' in base
js = open(ROOT + "/static/js/main.js").read()
assert 'dataset.busy' in js and "setTimeout" in js and "pageshow" in js
assert 'data-loading' in js and 'aria-busy' in js
import glob
for template in glob.glob(ROOT + "/templates/**/*.html",
                          recursive=True):
    src = open(template).read()
    assert not re.search(r"\son[a-z]+=\"", src), template
print("6. loading states: spinner CSS + double-submit guard wired,"
      " CSP hygiene intact OK")

print("\nALL HARDENING TESTS PASSED")
