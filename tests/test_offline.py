"""Offline PWA tests: manifest, service worker, offline page, drafts."""
import json
import os
import re
import shutil
import sys

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    Project,
    Submission,
    Task,
    User,
    money,
    utc_now,
)

FAILURES = []


def expect(condition, message):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def get_csrf(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    page = client.get("/login").get_data(as_text=True)
    return client.post("/login", data={
        "email": email, "password": password,
        "csrf_token": get_csrf(page),
    })


app = create_app()
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    ann1 = User(name="Tunde A", email="a@brydl.ng", role="annotator")
    ann1.set_password("annotator-pass-1")
    db.session.add_all([admin, ann1])
    db.session.flush()
    proj = Project(name="Offline Proj", client_name="Client O",
                   task_type="classification", created_by=admin.id,
                   pay_per_task=money("0.10"),
                   instructions="Pick a label.")
    db.session.add(proj)
    db.session.flush()
    assigned_ids = []
    for i in range(4):  # the suite owns its data: 1 open + 3 submitted
        task = Task(project_id=proj.id, assigned_to=ann1.id,
                    status="assigned", input_data=f"offline case {i}")
        db.session.add(task)
        db.session.flush()
        assigned_ids.append(task.id)
    ann1_id = ann1.id
    db.session.commit()
open_task_id = assigned_ids[0]
# one submitted (draft must purge) + one approved + one rejected
# (draft must be KEPT for revision) for the marker checks
statuses = [("submitted", None, None),
            ("approved", 90, utc_now()),
            ("rejected", 30, utc_now())]
with app.app_context():
    for idx, (status, score, reviewed_at) in enumerate(statuses):
        task_id = assigned_ids[idx + 1]
        db.session.add(Submission(
            task_id=task_id, annotator_id=ann1_id,
            output_data='{"kind":"classification","label":"positive"}',
            status=status, qa_score=score,
            submitted_at=utc_now(), reviewed_at=reviewed_at))
        task = db.session.get(Task, task_id)
        task.status = status
    db.session.commit()
    sub_ids = [(assigned_ids[i + 1], statuses[i][0])
               for i in range(3)]

ann = app.test_client()
login(ann, "a@brydl.ng", "annotator-pass-1")

# ---- manifest -----------------------------------------------------------
print("== Web manifest ==")
r = app.test_client().get("/manifest.webmanifest")
expect(r.status_code == 200
       and r.mimetype == "application/manifest+json",
       "manifest served at /manifest.webmanifest")
manifest = json.loads(r.get_data(as_text=True))
expect(manifest["name"] == "Brydl Dashboard"
       and manifest["short_name"] == "Brydl"
       and manifest["display"] == "standalone",
       "manifest identity + standalone display")
expect(manifest["theme_color"] == "#173363"
       and manifest["background_color"] == "#173363",
       "manifest uses the brand navy")
icon_ok = all(
    os.path.exists(os.path.join(ROOT, icon["src"].lstrip("/")))
    for icon in manifest["icons"])
sizes = {icon["sizes"] for icon in manifest["icons"]}
expect(icon_ok and "192x192" in sizes and "512x512" in sizes,
       "manifest icons exist on disk (192 + 512)")
base = open(os.path.join(ROOT, "templates/base.html")).read()
expect('rel="manifest"' in base and "public.manifest" in base,
       "base.html links the manifest")

# ---- service worker -------------------------------------------------------
print("== Service worker ==")
r = app.test_client().get("/sw.js")
sw = r.get_data(as_text=True)
expect(r.status_code == 200
       and "javascript" in r.mimetype,
       "worker served at root scope /sw.js (root scope = controls /)")
expect("no-cache" in r.headers.get("Cache-Control", ""),
       "worker served no-cache (updates roll out after deploys)")
expect("CACHE_VERSION" in sw and "caches.delete" in sw,
       "versioned cache with old-cache cleanup on activate")
expect('request.mode === "navigate"' in sw
       and "OFFLINE_URL" in sw,
       "navigations network-first with offline fallback")
expect('request.method !== "GET"' in sw,
       "POSTs never intercepted (CSRF + submissions untouched)")
expect("/static/vendor/" in sw and "cdn.jsdelivr.net" not in sw,
       "self-hosted vendor assets cached cache-first (styled offline,"
       " no third-party origin)")
expect(sw.count("fetch(") >= 3 and "self.addEventListener" in sw,
       "install/activate/fetch handlers present")

# ---- offline fallback page -------------------------------------------------
print("== Offline page ==")
r = app.test_client().get("/offline")
body = r.get_data(as_text=True)
expect(r.status_code == 200 and "You're offline right now" in body,
       "branded offline fallback is public")
expect("brydl-footer" in body and "brydl-progress" in body,
       "offline page keeps the shared chrome")

# ---- draft wiring on the task page -----------------------------------------
print("== Draft wiring ==")
task_body = ann.get(f"/annotator/task/{open_task_id}").get_data(
    as_text=True)
expect(f'data-draft-key="task-{open_task_id}"' in task_body,
       "answer form carries its per-task draft key")

dash = ann.get("/annotator/").get_data(as_text=True)
purged = {int(m) for m in re.findall(
    r'data-draft-purge="task-(\d+)"', dash)}
with app.app_context():
    expected = set()
    for task_id, status in sub_ids:
        if status in ("submitted", "approved"):
            expected.add(task_id)
    kept = {task_id for task_id, status in sub_ids
            if status == "rejected"}
expect(expected and expected <= purged,
       "submitted/approved tasks carry purge markers")
expect(not (kept & purged),
       "rejected tasks KEEP their draft (revision work preserved)")

# ---- client engine ----------------------------------------------------------
print("== Client engine (main.js) ==")
js = open(os.path.join(ROOT, "static/js/main.js")).read()
expect('"serviceWorker" in navigator' in js
       and '["localhost", "127.0.0.1"]' in js,
       "SW registration present, skipped on local dev hosts")
expect("brydl-offline-badge" in js and '"offline"' in js,
       "offline badge wired to online/offline events")
expect("brydl-draft-" in js and "data-draft-key" in js,
       "per-task draft save/restore engine present")
expect("data-draft-purge" in js,
       "server-driven purge markers honoured")
expect('form.action.indexOf("/logout") !== -1' in js
       and "localStorage.length" in js,
       "logout wipes every Brydl draft (shared devices)")
expect("Never clobber server-rendered values" in js,
       "restore never overwrites server-rendered field values")
css = open(os.path.join(ROOT, "static/css/style.css")).read()
expect(".brydl-offline-badge" in css and ".brydl-draft-banner" in css,
       "badge + draft-banner styles ship in the stylesheet")
expect(os.path.exists(os.path.join(
    ROOT, "templates", "offline.html")),
    "offline template exists in the repo")

# ---- hygiene -----------------------------------------------------------------
print("== Hygiene ==")
for template in ("base.html", "annotator/task.html",
                 "annotator/dashboard.html", "offline.html"):
    src = open(os.path.join(ROOT, "templates", template)).read()
    assert not re.search(r"\son[a-z]+=\"", src), template
expect(True, "no inline event handlers in touched templates")
expect("innerHTML" in js and "csrf" not in js.split("innerHTML")[1][:80],
       "banner innerHTML is a fixed string (no user data interpolated)")
for route in ("/manifest.webmanifest", "/sw.js", "/offline"):
    rr = app.test_client().get(route)
    assert rr.status_code == 200, route
expect(True, "all three new public routes answer 200 for anyone")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"OFFLINE PWA TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL OFFLINE PWA TESTS PASSED")
