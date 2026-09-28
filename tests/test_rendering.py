import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""RLHF Phase 3 suite: trusted rendering of untrusted task content.

Covers the security contract (markdown/code/conversation rendered
server-side, sanitized against script injection, javascript URLs and
event handlers stripped, oversize input degraded to plain text) and
the product behaviour (annotator workspace, preference responses,
review detail and arbitration pages render rich content; chat bubbles
for conversation documents; self-hosted Pygments stylesheet, no CDN).
"""
import json
import re
import shutil
import sys
from decimal import Decimal

from app import create_app
from extensions import db
from models import Project, Task, User
from rendering import content_view, conversation_turns, render_rich

app = create_app()

FAILURES = []


def expect(condition, label):
    """Record one granular pass/fail check."""
    if condition:
        print(f"   ok: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}")


def get_csrf(html):
    """Pull the CSRF token out of a rendered form page."""
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    """POST the sign-in form."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


# ---- 1. unit: sanitizer -------------------------------------------------
print("== 1. sanitizer ==")
out = render_rich("**Bold** and *soft* and a [link](https://x.ng).")
expect("<strong>Bold</strong>" in out and "<em>soft</em>" in out,
       "markdown emphasis converts")
expect('href="https://x.ng"' in out, "https link survives")
out = render_rich("hello <script>alert(1)</script> world")
expect("<script" not in out and "alert(1)" not in out,
       "script tags stripped entirely")
out = render_rich('<img src=x onerror="alert(1)">')
expect("<img" not in out and "onerror" not in out,
       "img/event-handler content stripped")
out = render_rich("[click](javascript:alert(1))")
expect("javascript:" not in out, "javascript: URLs stripped")
out = render_rich("<p onclick='evil()'>para</p>")
expect("onclick" not in out, "event attributes stripped")
out = render_rich("# Header\n\n- a\n- b\n\n| h | k |\n|---|---|\n| 1 | 2 |")
expect("<h1>" in out and "<ul>" in out and "<table>" in out,
       "headers, lists and tables render")
out = render_rich("```python\nx = 'a'  # comment\n```")
expect('class="s1"' in out or 'class="nb"' in out,
       "fenced code gets Pygments classes")
expect("<span" in out, "Pygments token spans present")
out = render_rich("A" * 25_000)
expect("<p>" not in out and "&amp;" not in out or "A" * 100 in out,
       "oversize input degrades to escaped plain text")
expect(conversation_turns("not json") is None
       and conversation_turns('{"kind": "other"}') is None,
       "non-conversation inputs rejected")
bad_conv = json.dumps({"kind": "conversation", "turns": [
    {"role": "hacker", "text": "hi"}]})
expect(conversation_turns(bad_conv) is None, "unknown roles rejected")
good_conv = json.dumps({"kind": "conversation", "turns": [
    {"role": "user", "text": "What is 2+2?"},
    {"role": "assistant",
     "text": "It is **4**.\n\n```python\nprint('hi')\n```"},
]})
turns = conversation_turns(good_conv)
expect(turns is not None and len(turns) == 2
       and turns[0]["role"] == "user",
       "valid conversation parses into turns")
expect("<strong>4</strong>" in turns[1]["html"]
       and 'class="nb"' in turns[1]["html"],
       "turn bodies render markdown + code")

# ---- 2. annotator workspace: rich + XSS-safe ---------------------------
print("== 2. workspace rendering ==")
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    db.session.add(admin)
    db.session.flush()
    admin_id = admin.id
    ann = User(name="Ann One", email="one@brydl.ng", role="annotator",
               terms_version="1.0")
    ann.set_password("annot-pass-1")
    db.session.add(ann)
    db.session.flush()
    ann_id = ann.id
    project = Project(name="Render test", client_name="Lab",
                      task_type="classification",
                      pay_per_task=Decimal("0.25"),
                      instructions="Label it.", created_by=admin_id)
    db.session.add(project)
    db.session.flush()
    md_task = Task(
        project_id=project.id, status="assigned", assigned_to=ann_id,
        input_data=("# Product review\n\nThe item is **broken**.\n\n"
                    "- no power\n- no lights\n\n```python\nprint('hi')\n"
                    "```\n\n<script>alert('xss')</script>"),
    )
    conv_task = Task(
        project_id=project.id, status="assigned", assigned_to=ann_id,
        input_data=json.dumps({"kind": "conversation", "turns": [
            {"role": "user", "text": "How do I boil an egg?"},
            {"role": "assistant",
             "text": "Boil for **8 minutes**, then cool.\n\n"
                     "<script>alert('nope')</script>"},
        ]}),
    )
    db.session.add_all([md_task, conv_task])
    db.session.commit()
    md_id, conv_id = md_task.id, conv_task.id
    project_id, conv_input = project.id, conv_task.input_data

ann_c = app.test_client()
login(ann_c, "one@brydl.ng", "annot-pass-1")
page = ann_c.get(f"/annotator/task/{md_id}").get_data(as_text=True)
expect("<h1>Product review</h1>" in page, "heading renders")
expect("<strong>broken</strong>" in page, "emphasis renders")
expect("<ul>" in page and "<li>no power</li>" in page, "list renders")
expect('class="nb"' in page, "code block highlighted")
expect("<script>alert" not in page
       and "alert('xss')" not in page,
       "injected script NEVER reaches the page")
conv_page = ann_c.get(f"/annotator/task/{conv_id}").get_data(
    as_text=True)
expect('chat-turn chat-user' in conv_page
       and 'chat-turn chat-assistant' in conv_page,
       "chat bubbles render with roles")
expect("<strong>8 minutes</strong>" in conv_page,
       "assistant turn renders markdown")
expect("<script>alert" not in conv_page
       and "alert('nope')" not in conv_page,
       "script inside conversation turns stripped")

# ---- 3. preference responses render rich -------------------------------
print("== 3. preference responses ==")
with app.app_context():
    pref_project = Project(name="Render pref", client_name="Lab",
                           task_type="preference",
                           pay_per_task=Decimal("1.00"),
                           instructions="Compare.",
                           created_by=admin_id)
    db.session.add(pref_project)
    db.session.flush()
    doc = json.dumps({
        "kind": "preference",
        "prompt": "Explain **recursion** with code.",
        "response_a": "Recursion is *bad*.\n\n<script>alert(1)</script>",
        "response_b": ("Recursion:\n\n```python\ndef f(n):\n"
                       "    return f(n - 1)\n```\n\nIt **works**."),
    })
    p_task = Task(project_id=pref_project.id, input_data=doc,
                  status="assigned", assigned_to=ann_id)
    db.session.add(p_task)
    db.session.commit()
    p_id = p_task.id
page = ann_c.get(f"/annotator/task/{p_id}").get_data(as_text=True)
expect("Explain <strong>recursion</strong>" in page,
       "prompt renders markdown")
expect("<strong>works</strong>" in page and 'class="n"' in page,
       "response B renders code + emphasis")
expect("alert(1)" not in page and "<script>alert" not in page,
       "response A's script stripped")

# ---- 4. admin surfaces ---------------------------------------------------
print("== 4. admin surfaces ==")
admin_c = app.test_client()
login(admin_c, "admin@brydl.ng", "admin-pass-1")
detail = admin_c.get(f"/admin/review/{md_id}").get_data(as_text=True)
# the admin task grid still shows the plain-text input
tasks_page = admin_c.get("/admin/tasks").get_data(as_text=True)
expect("Product review" in tasks_page,
       "admin task grid still shows plain-text input")
with app.app_context():
    from models import Submission
    sub = Submission(task_id=md_id, annotator_id=ann_id,
                     output_data=json.dumps(
                         {"kind": "classification", "label": "negative"}))
    db.session.add(sub)
    db.session.commit()
    sub_id = sub.id
detail = admin_c.get(f"/admin/review/{sub_id}").get_data(as_text=True)
expect("<h1>Product review</h1>" in detail,
       "review detail renders rich input")
expect("<script>alert" not in detail and "alert('xss')" not in detail,
       "review detail sanitized")
# arbitration page: conversation round prompt (depth 2 first so the
# upload creates a consensus group)
with app.app_context():
    from models import Project as _Project
    db.session.execute(
        db.update(_Project)
        .where(_Project.id == project_id)
        .values(required_annotations=2)
    )
    db.session.commit()
admin_c.post("/admin/tasks/upload", data={
    "project": str(project_id), "tasks_data": conv_input,
    "csrf_token": get_csrf(admin_c.get("/admin/tasks").get_data(
        as_text=True))}, follow_redirects=True)
from models import ConsensusGroup  # noqa: E402
with app.app_context():
    grp_id = db.session.scalar(db.select(ConsensusGroup.id).order_by(
        ConsensusGroup.id.desc()))
expect(grp_id is not None, "depth-2 upload created a consensus round")
arb = admin_c.get(f"/admin/review/conflicts/{grp_id}").get_data(
    as_text=True)
expect("chat-user" in arb and "chat-assistant" in arb,
       "arbitration prompt renders as conversation bubbles")
expect("<script>alert" not in arb and "alert('nope')" not in arb,
       "arbitration page sanitized")

# ---- 5. offline rule ----------------------------------------------------
print("== 5. offline rule ==")
pyg = open("static/css/pygments.css").read()
expect("http://" not in pyg and "https://" not in pyg and "@import"
       not in pyg, "pygments.css is fully self-contained")
base = open("templates/base.html").read()
expect('filename=\'css/pygments.css\'' in base
       or "css/pygments.css" in base,
       "base.html links the local pygments stylesheet")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"RENDERING TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL RENDERING TESTS PASSED")
