import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""E2E task upload & assignment tests — clean rewrite."""
import re

from app import create_app
from extensions import db
from models import User, Project, Task

app = create_app()

def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None

def login(client, email, password):
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email, "password": password,
                                       "csrf_token": tk})

LONG_TEXT = "x" * 80

with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann_a = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann_a.set_password("annotator-pass-1")
    ann_b = User(name="Bisi B", email="b@brydl.ng", role="annotator")
    ann_b.set_password("annotator-pass-2")
    ghost = User(name="Ghost G", email="g@brydl.ng", role="annotator", is_active=False)
    ghost.set_password("ghost-pass-11")
    db.session.add_all([admin, ann_a, ann_b, ghost])
    db.session.commit()
    admin_id, ann_a_id, ann_b_id, ghost_id = admin.id, ann_a.id, ann_b.id, ghost.id

    pa = Project(name="Active Proj", client_name="Acme", task_type="ner",
                 pay_per_task=0.50, instructions="i", created_by=admin_id)
    pb = Project(name="Paused Proj", client_name="Globex", task_type="ner",
                 pay_per_task=0.50, instructions="i", status="paused",
                 created_by=admin_id)
    db.session.add_all([pa, pb])
    db.session.commit()
    pa_id, pb_id = pa.id, pb.id
    db.session.add_all(
        [Task(project_id=pa_id, input_data=LONG_TEXT)]
        + [Task(project_id=pa_id, input_data=f"job {i}") for i in range(24)]
    )
    db.session.add_all([
        Task(project_id=pb_id, input_data="assigned one",
             status="assigned", assigned_to=ann_a_id),
        Task(project_id=pb_id, input_data="wip one",
             status="in_progress", assigned_to=ann_a_id),
        Task(project_id=pb_id, input_data="submitted one",
             status="submitted", assigned_to=ann_a_id),
        Task(project_id=pb_id, input_data="approved one",
             status="approved", assigned_to=ann_a_id),
        Task(project_id=pb_id, input_data="rejected one",
             status="rejected", assigned_to=ann_a_id),
    ])
    db.session.commit()
    print(f"SEED DONE: pa={pa_id} pb={pb_id} "
          f"tasks={db.session.scalar(db.select(db.func.count(Task.id)))}")

c = app.test_client()

# ---- 1. RBAC ----
for method, path, data in (
    ("get", "/admin/tasks", None),
    ("get", "/admin/tasks/upload", None),
    ("post", "/admin/tasks/assign", {}),
    ("post", "/admin/tasks/1/unassign", {}),
):
    r = getattr(app.test_client(), method)(path, data=data)
    assert r.status_code == 302 and "/login" in r.headers["Location"], (method, path)
r2 = app.test_client()
login(r2, "a@brydl.ng", "annotator-pass-1")
for method, path, data in (
    ("get", "/admin/tasks", None),
    ("get", "/admin/tasks/upload", None),
    ("post", "/admin/tasks/assign", {}),
    ("post", "/admin/tasks/1/unassign", {}),
):
    assert getattr(r2, method)(path, data=data).status_code == 403, path
print("1. RBAC OK")

login(c, "admin@brydl.ng", "admin-pass-1")
page = c.get("/admin/tasks").get_data(as_text=True)
assert "Task listing failed" not in page
csrf = get_csrf(page)

# ---- 2. upload ----
r = c.get("/admin/tasks/upload", follow_redirects=True)
assert "Bulk upload tasks" in r.get_data(as_text=True)
card = c.get("/admin/tasks").get_data(as_text=True)
m = re.search(r'id="uploadCard".*?</form>', card, re.DOTALL)
card_html = m.group(0) if m else ""
assert f'value="{pa_id}"' in card_html
assert f'value="{pb_id}"' not in card_html
assert f'value="{pb_id}"' in card  # filter dropdown keeps all projects
print("2a. upload form OK")

def upload(text, project=str(pa_id)):
    return c.post("/admin/tasks/upload", data={
        "project": project, "tasks_data": text, "csrf_token": csrf})

upload("")
assert "Paste at least one task." in c.get(
    "/admin/tasks", follow_redirects=True).get_data(as_text=True)
upload("\n   \n\n")
assert "Paste at least one task." in c.get(
    "/admin/tasks", follow_redirects=True).get_data(as_text=True)
r = upload("hello", project=str(pb_id))
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "Not a valid choice" in fl
r = upload("hello", project="99999")
fl = c.get(r.headers["Location"]).get_data(as_text=True)
assert "Not a valid choice" in fl
r = upload("good line one\n\n\tbad-tab-first\ngood line two")
fl = c.get("/admin/tasks", follow_redirects=True).get_data(as_text=True)
assert "Line 3: task text is empty before the tab separator." in fl
assert "Nothing was saved" in fl
with app.app_context():
    n = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.project_id == pa_id))
    assert n == 25, f"atomicity broken: pa has {n}"
r = upload("\n".join(f"task {i}" for i in range(501)))
fl = c.get("/admin/tasks", follow_redirects=True).get_data(as_text=True)
assert "Batch too large: 501" in fl
with app.app_context():
    n = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.project_id == pa_id))
    assert n == 25, f"oversize saved: pa has {n}"
r = upload("translate this\tgold-answer-here\nsecond task")
fl = c.get("/admin/tasks", follow_redirects=True).get_data(as_text=True)
assert "2 task(s) created in" in fl
with app.app_context():
    gold = db.session.scalar(db.select(Task).where(
        Task.input_data == "translate this"))
    assert gold is not None and gold.expected_output == "gold-answer-here"
    total = db.session.scalar(db.select(db.func.count(Task.id)))
    print(f"AFTER UPLOAD: total={total} (expect 32)")
    assert total == 32
print("2. upload rejections + atomicity + success OK")

# ---- 3. list, filters, pagination ----
html = c.get("/admin/tasks").get_data(as_text=True)
with app.app_context():
    total_now = db.session.scalar(db.select(db.func.count(Task.id)))
print(f"SECTION 3: DB total={total_now} | page says: "
      f"{re.search(r'Showing[^<]*', html).group(0) if 'Showing' in html else 'NO FOOTER'}")
assert html.count('class="text-muted small">#') == 20
assert "Showing 20 of 32" in html
html2 = c.get("/admin/tasks?page=2").get_data(as_text=True)
assert "Showing 12 of 32" in html2
assert f'title="{LONG_TEXT}"' in html2
assert "x" * 50 + "…" in html2
r = c.get("/admin/tasks?page=999", follow_redirects=False)
assert r.status_code == 302 and "page=2" in r.headers["Location"]
h = c.get(f"/admin/tasks?project={pa_id}").get_data(as_text=True)
assert "Showing 20 of 27" in h
h = c.get(f"/admin/tasks?annotator={ann_a_id}").get_data(as_text=True)
assert "Showing 5 of 5" in h
h = c.get(f"/admin/tasks?project={pb_id}&status=submitted").get_data(as_text=True)
assert "Showing 1 of 1" in h and "submitted one" in h
h = c.get(f"/admin/tasks?project={pb_id}&status=pending").get_data(as_text=True)
assert "No tasks match" in h
print("3. list/filters/pagination OK")

# ---- 4. assign ----
with app.app_context():
    pend_ids = [t.id for t in db.session.scalars(db.select(Task).where(
        Task.project_id == pa_id, Task.status == "pending").limit(2))]
    assigned_id = db.session.scalar(db.select(Task.id).where(
        Task.input_data == "assigned one"))
r = c.post("/admin/tasks/assign", data={"csrf_token": csrf})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "Tick at least one pending task" in fl
r = c.post("/admin/tasks/assign", data={
    "csrf_token": csrf, "annotator_id": str(ann_b_id),
    "task_ids": [str(pend_ids[0]), str(pend_ids[1]), str(assigned_id),
                 "abc", "-5", "999999"]})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "2 task(s) assigned to Bisi B." in fl and "3 skipped" in fl
with app.app_context():
    for tid in pend_ids:
        t = db.session.get(Task, tid)
        assert t.status == "assigned" and t.assigned_to == ann_b_id
with app.app_context():
    more = [t.id for t in db.session.scalars(db.select(Task).where(
        Task.project_id == pa_id, Task.status == "pending").limit(1))]
r = c.post("/admin/tasks/assign", data={"csrf_token": csrf,
                                        "annotator_id": str(ghost_id),
                                        "task_ids": [str(more[0])]})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "Choose an active annotator" in fl
r = c.post("/admin/tasks/assign", data={"csrf_token": csrf, "annotator_id": "1",
                                        "task_ids": [str(more[0])]})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "Choose an active annotator" in fl
r = c.post("/admin/tasks/assign", data={"annotator_id": str(ann_b_id),
                                        "task_ids": [str(more[0])]})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "Security check failed" in fl
with app.app_context():
    assert db.session.get(Task, more[0]).status == "pending"
print("4. assign OK")

# ---- 5. unassign ----
with app.app_context():
    wip_id = db.session.scalar(db.select(Task.id).where(
        Task.input_data == "wip one"))
    sub_id = db.session.scalar(db.select(Task.id).where(
        Task.input_data == "submitted one"))
csrf2 = get_csrf(c.get("/admin/tasks").get_data(as_text=True))
r = c.post(f"/admin/tasks/{wip_id}/unassign", data={"csrf_token": csrf2})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "returned to the pending pool" in fl
with app.app_context():
    t = db.session.get(Task, wip_id)
    assert t.status == "pending" and t.assigned_to is None
r = c.post(f"/admin/tasks/{assigned_id}/unassign", data={"csrf_token": csrf2})
with app.app_context():
    t = db.session.get(Task, assigned_id)
    assert t.status == "pending" and t.assigned_to is None
r = c.post(f"/admin/tasks/{sub_id}/unassign", data={"csrf_token": csrf2})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "belongs to the QA flow" in fl
with app.app_context():
    assert db.session.get(Task, sub_id).status == "submitted"
r = c.post(f"/admin/tasks/{wip_id}/unassign", data={"csrf_token": csrf2})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "already unassigned" in fl
assert c.post("/admin/tasks/99999/unassign",
              data={"csrf_token": csrf2}).status_code == 404
r = c.post(f"/admin/tasks/{wip_id}/unassign", data={"csrf_token": ""})
fl = c.get("/admin/tasks").get_data(as_text=True)
assert "Security check failed" in fl
with app.app_context():
    assert db.session.get(Task, wip_id).status == "pending"
print("5. unassign OK")
print("\nALL TASK MANAGEMENT TESTS PASSED")
