import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""RLHF Phase 6 suite: QA + auditor staff roles and the landing page.

Covers: role vocabulary + DB constraint, per-role access to the QA
surface (review queue/detail/bulk, quality), the auditor surface
(conflicts, arbitration with a binding POST ruling, audit trail),
role routing (sign-in destination, dashboard redirects), 403 walls
between roles, Team-page role choices, and the rewritten landing
page (African AI data labelling copy for clients and staff).
"""
import json
import re
import shutil
import sys
from decimal import Decimal

from app import create_app
from extensions import db
from models import (
    AuditLog,
    ConsensusGroup,
    Project,
    ROLES,
    Submission,
    Task,
    User,
)
from models import utc_now

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


PROMPT = "Which reply better answers: how do I hard-boil an egg?"
DOC = json.dumps({"kind": "preference", "prompt": PROMPT,
                  "response_a": "Boil them for 40 seconds.",
                  "response_b": "Boil 8 minutes, then chill."})
JUST = ("B is far more complete and correct for the asked question.")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ---- Section 1: role vocabulary ----
print("== Section 1: role vocabulary ==")
expect(ROLES == ("admin", "project_manager", "qa", "auditor", "hr", "annotator"),
       "ROLES tuple: admin/project_manager/qa/auditor/hr/annotator")
with app.app_context():
    con = db.session.connection()
    ddl = str(con.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='users'").scalar())
expect("ck_users_role" in ddl and "'qa'" in ddl and "'auditor'" in ddl,
       "ck_users_role carries the widened vocabulary")

# ---- fixtures ----
with app.app_context():
    admin = User(name="Ada Admin", email="roles-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    qa = User(name="Qasi Review", email="qa@brydl.ng", role="qa")
    qa.set_password("qa-pass-1")
    aud = User(name="Ada Olu Audit", email="auditor@brydl.ng",
               role="auditor")
    aud.set_password("aud-pass-1")
    ann = User(name="Ann One", email="one@brydl.ng", role="annotator",
               terms_version="1.0")
    ann.set_password("annot-pass-1")
    db.session.add_all([admin, qa, aud, ann])
    db.session.flush()
    admin_id, qa_id, aud_id, ann_id = (
        admin.id, qa.id, aud.id, ann.id)
    p = Project(name="Roles RLHF", client_name="Lab",
                task_type="preference", pay_per_task=Decimal("0.30"),
                required_annotations=1, instructions="Pick.",
                created_by=admin_id)
    db.session.add(p)
    db.session.flush()
    pid = p.id
    t = Task(input_data=DOC, project_id=pid, status="submitted",
             assigned_to=ann_id)
    db.session.add(t)
    db.session.flush()
    sub = Submission(task_id=t.id, annotator_id=ann_id,
                     output_data=json.dumps({
                         "kind": "preference", "chosen": "b",
                         "seen_order": "ab", "justification": JUST}),
                     status="submitted", submitted_at=utc_now())
    db.session.add(sub)
    grp = ConsensusGroup(project_id=pid, required_count=1,
                         status="conflict")
    db.session.add(grp)
    db.session.commit()
    sub_id, grp_id = sub.id, grp.id

admin_c = app.test_client()
login(admin_c, "roles-admin@brydl.ng", "admin-pass-1")
qa_c = app.test_client()
login(qa_c, "qa@brydl.ng", "qa-pass-1")
aud_c = app.test_client()
login(aud_c, "auditor@brydl.ng", "aud-pass-1")
ann_c = app.test_client()
login(ann_c, "one@brydl.ng", "annot-pass-1")

# ---- Section 2: QA surface ----
print("== Section 2: QA role ==")
r = qa_c.get("/admin/review")
expect(r.status_code == 200 and "QA" in r.get_data(as_text=True),
       "qa opens the review queue")
expect(qa_c.get(f"/admin/review/{sub_id}").status_code == 200,
       "qa opens a submission detail")
expect(qa_c.get("/admin/quality").status_code == 200,
       "qa opens the quality page")
r = qa_c.get("/admin/")
expect(r.status_code == 302
       and r.headers["Location"].endswith("/admin/review"),
       "qa hitting / is routed to the review queue")
# verdict power: approve works for qa
r = qa_c.post(f"/admin/review/{sub_id}", data={
    "action": "approve", "qa_score": "92",
    "csrf_token": get_csrf(qa_c.get(f"/admin/review/{sub_id}"
                                    ).get_data(as_text=True))})
expect(r.status_code == 302, "qa records a verdict")
with app.app_context():
    got = db.session.get(Submission, sub_id)
    expect(got.status == "approved" and got.qa_score == 92.0,
           "qa verdict persisted")
# walls: qa cannot touch money/projects/team/sft/arbitration/audit
for path in ("/admin/projects", "/admin/tasks", "/admin/payments",
             "/admin/users", "/admin/sft", "/admin/review/conflicts",
             "/admin/audit"):
    expect(qa_c.get(path).status_code == 403, f"qa 403 on {path}")
expect(qa_c.get("/annotator/").status_code == 403,
       "qa cannot enter the annotator workspace")

# ---- Section 3: auditor surface ----
print("== Section 3: auditor role ==")
r = aud_c.get("/admin/review/conflicts")
expect(r.status_code == 200, "auditor opens the conflicts queue")
expect(aud_c.get(f"/admin/review/conflicts/{grp_id}").status_code
       == 200, "auditor opens the arbitration page")
expect(aud_c.get("/admin/audit").status_code == 200,
       "auditor opens the audit trail")
r = aud_c.get("/admin/")
expect(r.status_code == 302
       and r.headers["Location"].endswith("/admin/review/conflicts"),
       "auditor hitting / is routed to arbitration")
# binding ruling as auditor
arb_page = aud_c.get(f"/admin/review/conflicts/{grp_id}"
                     ).get_data(as_text=True)
r = aud_c.post(f"/admin/review/conflicts/{grp_id}", data={
    "decided_choice": "a",
    "decided_note": "A is the better answer to the prompt.",
    "csrf_token": get_csrf(arb_page)})
expect(r.status_code == 302, "auditor records the binding ruling")
with app.app_context():
    g = db.session.get(ConsensusGroup, grp_id)
    expect(g.status == "resolved" and g.decided_choice == "a",
           "arbitration frozen as resolved")
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "consensus.resolve",
        AuditLog.entity_id == grp_id)) is not None,
        "ruling audited")
# walls
for path in ("/admin/projects", "/admin/payments", "/admin/users",
             "/admin/sft", "/admin/review", "/admin/quality"):
    expect(aud_c.get(path).status_code == 403,
           f"auditor 403 on {path}")

# ---- Section 4: role walls hold both ways ----
print("== Section 4: cross-role walls ==")
expect(ann_c.get("/admin/review").status_code == 403,
       "annotator still 403 on review")
expect(admin_c.get("/admin/review").status_code == 200,
       "admin keeps full review access")
expect(admin_c.get("/admin/review/conflicts").status_code == 200,
       "admin keeps arbitration access")
expect(admin_c.get("/admin/audit").status_code == 200,
       "admin keeps audit access")
expect(admin_c.get("/admin/sft").status_code == 200,
       "admin keeps SFT access")

# ---- Section 5: sign-in routing + team form ----
print("== Section 5: routing + team roles ==")
fresh = app.test_client()
r = login(fresh, "qa@brydl.ng", "qa-pass-1")
expect(r.status_code == 302
       and r.headers["Location"].endswith("/admin/review"),
       "sign-in sends qa to the review queue")
fresh2 = app.test_client()
r = login(fresh2, "auditor@brydl.ng", "aud-pass-1")
expect(r.status_code == 302
       and r.headers["Location"].endswith("/admin/review/conflicts"),
       "sign-in sends auditor to arbitration")
team = admin_c.get("/admin/users").get_data(as_text=True)
expect('value="qa"' in team and 'value="auditor"' in team,
       "team form offers qa + auditor roles")
r = admin_c.post("/admin/users/create", data={
    "name": "New QA", "email": "qa2@brydl.ng", "role": "qa",
    "csrf_token": get_csrf(team)}, follow_redirects=True)
expect("temporary password" in r.get_data(as_text=True).lower()
       or "created" in r.get_data(as_text=True).lower(),
       "admin creates a qa account")
with app.app_context():
    qa2 = db.session.scalar(db.select(User).where(
        User.email == "qa2@brydl.ng"))
    expect(qa2 is not None and qa2.role == "qa",
           "qa account persisted with role qa")

# ---- Section 6: landing page ----
print("== Section 6: landing page copy ==")
anon = app.test_client()
page = anon.get("/").get_data(as_text=True)
expect("Africa's AI data labelling company" in page,
       "hero states the African AI labeling positioning")
expect("I need data labelled" in page and "I want to work as an annotator" in page,
       "dual-audience CTAs on the hero")
expect('id="clients"' in page and "For clients" in page,
       "clients section present")
expect('id="staff"' in page and "For annotators" in page,
       "staff section present")
expect("RLHF" in page and "consensus" in page,
       "service depth (RLHF/consensus) visible")
expect("Hausa" in page and "Nigerian bank account" in page,
       "African specifics visible")
expect("Sign in to Brydl" in page, "closing CTA present")
r = anon.get("/", follow_redirects=False)
expect(r.status_code == 200, "landing still public for anonymous")
qa_view = app.test_client()
login(qa_view, "qa@brydl.ng", "qa-pass-1")
r = qa_view.get("/", follow_redirects=False)
expect(r.status_code == 302, "signed-in staff still skip the landing")

print()
if FAILURES:
    print(f"ROLES TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL ROLES TESTS PASSED")
