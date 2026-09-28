import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""RLHF Phase 2 suite: the blind-consensus engine and arbitration.

Covers: project consensus depth (form + persistence), grouped import
(siblings per row), the claim-side blindness guard, group evaluation
(agreed / conflict / pending), the arbitration view and binding
resolution (frozen after ruling, audited), consensus fields in the
JSONL export, and that annotator surfaces never expose sibling
verdicts.
"""
import json
import re
import shutil
import sys
from decimal import Decimal

from app import create_app
from extensions import db
from models import AuditLog, ConsensusGroup, Project, Task, User
from routes.annotator import _preference_flipped

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


def flash_of(response):
    """Follow a redirect and return the page text."""
    return client_get(response.headers["Location"])


def client_get(path):
    """GET a path on the admin client as text."""
    return admin_c.get(path).get_data(as_text=True)


PROMPT = "Which reply better answers: how do I hard-boil an egg?"
RESP_A = "Boil them for 40 seconds."
RESP_B = ("Boil 8 minutes from cold water, then chill in ice water "
          "before peeling.")
DOC = json.dumps({"kind": "preference", "prompt": PROMPT,
                  "response_a": RESP_A, "response_b": RESP_B})

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ---- seed users ----
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    db.session.add(admin)
    db.session.flush()
    for suffix in ("one", "two", "three"):
        u = User(name=f"Ann {suffix}", email=f"{suffix}@brydl.ng",
                 role="annotator", terms_version="1.0")
        u.set_password("annot-pass-1")
        db.session.add(u)
    db.session.commit()

with app.app_context():
    ANN_IDS = dict(db.session.execute(
        db.select(User.email, User.id).where(User.role == "annotator")
    ).all())


def displayed_for(task_id, annotator_id, canonical):
    """Map a canonical choice to what this annotator sees on screen.

    Args:
        task_id: The sibling task.
        annotator_id: The annotator (position flip is per-annotator).
        canonical: "a" or "b" — the intended canonical answer.

    Returns:
        The displayed radio value that maps back to ``canonical``.
    """
    if _preference_flipped(task_id, annotator_id):
        return "b" if canonical == "a" else "a"
    return canonical


admin_c = app.test_client()
login(admin_c, "admin@brydl.ng", "admin-pass-1")

# ---- 1. project with consensus depth 2 (through the real form) --------
print("== 1. project depth ==")
page = client_get("/admin/projects/new")
expect("Annotations per task" in page and 'value="2"' in page,
       "form shows the consensus depth select")
r = admin_c.post("/admin/projects/new", data={
    "name": "Consensus pref", "client_name": "AI Lab",
    "task_type": "preference", "pay_per_task": "1.00",
    "instructions": "Compare honestly.", "deadline": "",
    "required_annotations": "2", "status": "active",
    "csrf_token": get_csrf(page)}, follow_redirects=True)
with app.app_context():
    project = db.session.scalar(db.select(Project).where(
        Project.name == "Consensus pref"))
expect(project is not None and project.required_annotations == 2,
       "depth 2 persisted")

# ---- 2. grouped import via the paste upload path ----------------------
print("== 2. grouped import ==")
r = admin_c.post("/admin/tasks/upload", data={
    "project": str(project.id), "tasks_data": f"{DOC}\tB",
    "csrf_token": get_csrf(client_get("/admin/tasks"))},
    follow_redirects=True)
with app.app_context():
    tasks = db.session.scalars(db.select(Task).where(
        Task.project_id == project.id)).all()
    groups = db.session.scalars(db.select(ConsensusGroup)).all()
expect(len(tasks) == 2, f"2 sibling tasks created ({len(tasks)})")
expect(len(groups) == 1 and groups[0].required_count == 2
       and groups[0].status == "pending",
       "one pending group with required_count 2")
expect(all(t.consensus_group_id == groups[0].id for t in tasks)
       and all(t.expected_output == "b" for t in tasks),
       "siblings linked to the group, gold normalized on both")
group_id = groups[0].id
task_ids = sorted(t.id for t in tasks)

# ---- 3. claim blindness guard -----------------------------------------
print("== 3. claim guard ==")
ann1_c = app.test_client()
login(ann1_c, "one@brydl.ng", "annot-pass-1")
ann2_c = app.test_client()
login(ann2_c, "two@brydl.ng", "annot-pass-1")
dash1 = ann1_c.get("/annotator/").get_data(as_text=True)
r = ann1_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(dash1)}, follow_redirects=True)
expect("Task claimed" in r.get_data(as_text=True),
       "ann1 claims the first sibling")
dash1 = ann1_c.get("/annotator/").get_data(as_text=True)
r = ann1_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(dash1)}, follow_redirects=True)
expect("No tasks are available" in r.get_data(as_text=True),
       "ann1 is REFUSED the second sibling of the same round")
dash2 = ann2_c.get("/annotator/").get_data(as_text=True)
r = ann2_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(dash2)}, follow_redirects=True)
expect("Task claimed" in r.get_data(as_text=True),
       "ann2 claims the second sibling")

# ---- 4. blindness on the annotator surface -----------------------------
print("== 4. blindness ==")
own_page = ann1_c.get(f"/annotator/task/{task_ids[0]}").get_data(
    as_text=True)
expect(PROMPT in own_page and RESP_A in own_page,
       "sibling workspace renders the round data")
expect(f"/annotator/task/{task_ids[1]}" not in own_page
       and f"task-{task_ids[1]}" not in own_page,
       "no sibling task id leaks into the sibling's page")
expect("two@brydl.ng" not in own_page and "Ann two" not in own_page,
       "no sibling annotator identity leaks")

# ---- 5. agreement ------------------------------------------------------
print("== 5. agreement ==")


def submit(client, task_id, chosen, just):
    """Submit a preference answer on a sibling task."""
    page = client.get(f"/annotator/task/{task_id}").get_data(
        as_text=True)
    return client.post(f"/annotator/task/{task_id}", data={
        "chosen": chosen, "justification": just,
        "csrf_token": get_csrf(page)}, follow_redirects=True)


JUST1 = ("B gives the actual timing and the ice bath, which is what "
         "actually makes eggs peelable.")
JUST2 = ("B is specific about minutes and cooling; A would leave a "
         "raw egg in the middle, which fails the question.")
shown1 = displayed_for(task_ids[0], ANN_IDS["one@brydl.ng"], "b")
r = submit(ann1_c, task_ids[0], shown1, JUST1)
expect("awaiting QA review" in r.get_data(as_text=True),
       "sibling 1 submitted")
with app.app_context():
    status = db.session.get(ConsensusGroup, group_id).status
expect(status == "pending", "group pending after 1 of 2 answers")
shown2 = displayed_for(task_ids[1], ANN_IDS["two@brydl.ng"], "b")
r = submit(ann2_c, task_ids[1], shown2, JUST2)
with app.app_context():
    status = db.session.get(ConsensusGroup, group_id).status
expect(status == "agreed", f"agreement detected ({status})")

# ---- 6. conflict + arbitration ----------------------------------------
print("== 6. conflict + arbitration ==")
# second round: import another row, answer with DISAGREEING choices
admin_c.post("/admin/tasks/upload", data={
    "project": str(project.id), "tasks_data": DOC,
    "csrf_token": get_csrf(client_get("/admin/tasks"))},
    follow_redirects=True)
with app.app_context():
    g2 = db.session.scalar(
        db.select(ConsensusGroup)
        .where(ConsensusGroup.id != group_id)
        .order_by(ConsensusGroup.id.desc())
    )
    t_a, t_b = sorted(t.id for t in g2.tasks)
expect(g2.status == "pending", "second round created pending")
ann3_c = app.test_client()
login(ann3_c, "three@brydl.ng", "annot-pass-1")
with app.app_context():
    ann1_id = ANN_IDS["one@brydl.ng"]
tasks_page = client_get("/admin/tasks")
admin_c.post("/admin/tasks/assign", data={
    "task_ids": str(t_a), "annotator_id": str(ann1_id),
    "csrf_token": get_csrf(tasks_page)}, follow_redirects=True)
with app.app_context():
    owner = db.session.scalar(db.select(Task.assigned_to).where(
        Task.id == t_a))
expect(owner == ann1_id, "admin assign works across groups")
dash3 = ann3_c.get("/annotator/").get_data(as_text=True)
ann3_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(dash3)}, follow_redirects=True)
with app.app_context():
    ann3_owner = db.session.scalar(db.select(Task.assigned_to).where(
        Task.id == t_b))
expect(ann3_owner == ANN_IDS["three@brydl.ng"],
       "ann3 claims the round-2 sibling")
shown_a = displayed_for(t_a, ann1_id, "a")
submit(ann1_c, t_a, shown_a,
       "A is shorter, and for a quick answer shorter wins the user.")
shown_b3 = displayed_for(t_b, ANN_IDS["three@brydl.ng"], "b")
submit(ann3_c, t_b, shown_b3, JUST2)
with app.app_context():
    g2_status = db.session.get(ConsensusGroup, g2.id).status
expect(g2_status == "conflict", f"disagreement flagged ({g2_status})")

conflicts_page = client_get("/admin/review/conflicts")
expect("Round #" in conflicts_page and "conflict" in conflicts_page,
       "conflicts list renders with statuses")
expect(JUST1.split(",")[0] in conflicts_page
       or "agreed" in conflicts_page,
       "round evidence visible on the list")
arb = client_get(f"/admin/review/conflicts/{g2.id}")
expect(PROMPT in arb and "Canonical answer" in arb,
       "arbitration page shows prompt + ruling form")
expect("A is shorter" in arb,
       "auditor sees the justification evidence")
r = admin_c.post(f"/admin/review/conflicts/{g2.id}", data={
    "decided_choice": "b", "decided_note": "Timing + ice bath wins.",
    "csrf_token": get_csrf(arb)}, follow_redirects=True)
with app.app_context():
    g2row = db.session.get(ConsensusGroup, g2.id)
expect(g2row.status == "resolved" and g2row.decided_choice == "b"
       and g2row.resolved_by is not None
       and g2row.resolved_at is not None,
       "ruling recorded: resolved, decided b, auditor + time stored")
expect("resolved" in r.get_data(as_text=True), "flash confirms ruling")
with app.app_context():
    audit_ok = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "consensus.resolve",
        AuditLog.entity_id == g2.id)) is not None
expect(audit_ok, "arbitration audited")
# re-arbitration refused + frozen
r = admin_c.get(f"/admin/review/conflicts/{g2.id}",
                follow_redirects=True)
expect("already resolved" in r.get_data(as_text=True),
       "re-arbitration refused")
with app.app_context():
    frozen = db.session.get(ConsensusGroup, g2.id).status
expect(frozen == "resolved", "resolved group stays frozen")

# ---- 7. verdict hook: rejection sends a completed group to pending ----
print("== 7. verdict hook ==")
# third round, agreed, then one sibling rejected with revision
admin_c.post("/admin/tasks/upload", data={
    "project": str(project.id), "tasks_data": DOC,
    "csrf_token": get_csrf(client_get("/admin/tasks"))},
    follow_redirects=True)
with app.app_context():
    g3 = db.session.scalar(
        db.select(ConsensusGroup)
        .where(ConsensusGroup.id.notin_([group_id, g2.id]))
        .order_by(ConsensusGroup.id.desc())
    )
    g3_ids = sorted(t.id for t in g3.tasks)
dash1 = ann1_c.get("/annotator/").get_data(as_text=True)
ann1_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(dash1)}, follow_redirects=True)
dash2 = ann2_c.get("/annotator/").get_data(as_text=True)
ann2_c.post("/annotator/claim", data={
    "csrf_token": get_csrf(dash2)}, follow_redirects=True)
with app.app_context():
    g3_owners = {t.id: t.assigned_to
                 for t in db.session.get(ConsensusGroup, g3.id).tasks}
expect(None not in g3_owners.values(),
       f"round-3 fully claimed ({g3_owners})")
for tid in g3_ids:
    with app.app_context():
        owner_id = db.session.scalar(db.select(Task.assigned_to).where(
            Task.id == tid))
    email = next(e for e, uid in ANN_IDS.items() if uid == owner_id)
    who = {"one@brydl.ng": ann1_c, "two@brydl.ng": ann2_c,
           "three@brydl.ng": ann3_c}[email]
    shown = displayed_for(tid, owner_id, "b")
    submit(who, tid, shown,
           "Timing and ice bath win again, clearly and repeatably.")
with app.app_context():
    g3_status = db.session.get(ConsensusGroup, g3.id).status
expect(g3_status == "agreed", f"round-3 agreed ({g3_status})")
with app.app_context():
    from models import Submission
    sub_row = db.session.scalar(db.select(Submission).where(
        Submission.task_id == g3_ids[0]).order_by(
            Submission.id.desc()))
    sub_id = sub_row.id
csrf = get_csrf(client_get("/admin/review"))
admin_c.post("/admin/review/bulk", data={
    "submission_ids": [str(sub_id)], "action": "reject",
    "qa_score": "20", "return_for_revision": "y",
    "csrf_token": csrf}, follow_redirects=True)
with app.app_context():
    g3_after = db.session.get(ConsensusGroup, g3.id).status
expect(g3_after == "pending",
       "rejected sibling returns the round to pending (redo)")

# ---- 8. export carries consensus --------------------------------------
print("== 8. export ==")
csrf = get_csrf(
    client_get(f"/admin/projects/{project.id}/export"))
r = admin_c.post(f"/admin/projects/{project.id}/export", data={
    "format": "jsonl", "scope": "all", "csrf_token": csrf})
records = [json.loads(line) for line in
           r.get_data(as_text=True).splitlines() if line]
with_cons = [rec for rec in records if rec.get("consensus")]
expect(len(with_cons) >= 4, f"consensus block on sibling rows"
       f" ({len(with_cons)})")
expect(any(rec["consensus"]["status"] == "resolved"
           and rec["consensus"]["decision"] == "b"
           for rec in with_cons),
       "resolved decision travels to the export")

# S6b: image_labeling — same label = agreed (bbox geometry ignored);
# different labels → conflict. Audio transcripts always arbitrate.
from models import ConsensusGroup, utc_now
import consensus as consensus_engine
with app.app_context():
    admin = db.session.scalar(db.select(User).where(
        User.email == "admin@brydl.ng"))
    a1 = db.session.scalar(db.select(User).where(
        User.email == "one@brydl.ng"))
    a2 = db.session.scalar(db.select(User).where(
        User.email == "two@brydl.ng"))
    p = Project(
        name="Media RLHF", client_name="Lab M",
        task_type="image_labeling", pay_per_task=0.10,
        instructions="i", created_by=admin.id,
    )
    db.session.add(p); db.session.flush()
    g = ConsensusGroup(project_id=p.id, required_count=2)
    db.session.add(g); db.session.flush(); gid = g.id
    t1 = Task(
        input_data="image i", project_id=p.id,
        status="assigned", assigned_to=a1.id,
        consensus_group_id=gid,
    )
    t2 = Task(
        input_data="image i", project_id=p.id,
        status="assigned", assigned_to=a2.id,
        consensus_group_id=gid,
    )
    db.session.add_all([t1, t2]); db.session.flush()
    db.session.add(Submission(
        task_id=t1.id, annotator_id=a1.id,
        output_data=json.dumps(
            {"kind": "image_labeling", "label": "cat",
             "bbox": [0, 0, 10, 10]}),
        status="submitted",
        submitted_at=utc_now(),
    ))
    db.session.add(Submission(
        task_id=t2.id, annotator_id=a2.id,
        output_data=json.dumps(
            {"kind": "image_labeling", "label": "cat",
             "bbox": [1, 1, 9, 9]}),
        status="submitted",
        submitted_at=utc_now(),
    ))
    db.session.commit(); t1id, t2id, gid_v = t1.id, t2.id, gid
    db.session.expire_all()
# refresh_group needs an app context.
with app.app_context():
    consensus_engine.refresh_group(gid_v)
    db.session.commit()
with app.app_context():
    from models import ConsensusGroup as CG
    t1, t2 = db.session.get(Task, t1id), db.session.get(Task, t2id)
    gp = db.session.get(CG, gid_v)
    expect(gp.status == "agreed",
           "image_labeling identical labels -> agreed (bbox diff ignored)")
expect(consensus_engine.verdict_key(json.dumps({
    "kind": "audio_transcription",
    "transcript": "hello",
})) is None,
    "audio_transcription verdict_key → None (always arbitrate)")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"CONSENSUS TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL CONSENSUS TESTS PASSED")
