import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""RLHF Phase 5 suite: rubric dimensions + per-response sliders.

Covers: the pure rubric module (dimension parsing + score
validation), the project form (persistence, caps, rejection), the
annotator preference workspace (sliders render, canonical-side
mapping under the deterministic flip, tampered/missing scores
rejected), review + arbitration display, gold secrecy, and the
rubric riding the JSONL export document.
"""
import json
import re
import shutil
import sys
from decimal import Decimal

from app import create_app
from extensions import db
from models import ConsensusGroup, Project, Submission, Task, User
from routes.annotator import _preference_flipped
import rubric as rubmod

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
RESP_A = "Boil them for 40 seconds."
RESP_B = ("Boil 8 minutes from cold water, then chill in ice water "
          "before peeling.")
DOC = json.dumps({"kind": "preference", "prompt": PROMPT,
                  "response_a": RESP_A, "response_b": RESP_B})
DIMS_RAW = "accuracy\ntone"

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ============ Section 1: pure rubric module ============
print("== Section 1: rubric.py unit contract ==")
dims, problems = rubmod.parse_dimensions(" accuracy \n\ntone\n")
expect(dims == ["accuracy", "tone"] and problems == [],
       "parse: strips, skips blanks, keeps order")
dims, problems = rubmod.parse_dimensions("Accuracy\naccuracy")
expect(dims == ["Accuracy"] and len(problems) == 1,
       "parse: case-insensitive duplicates rejected")
dims, problems = rubmod.parse_dimensions("a\nb\nc", max_dimensions=2)
expect(len(problems) == 1 and "at most 2" in problems[0],
       "parse: dimension count cap reported")
dims, problems = rubmod.parse_dimensions("x" * 41, max_name=40)
expect(dims == [] and len(problems) == 1,
       "parse: over-long name rejected")
expect(rubmod.parse_dimensions("")[0] == [], "parse: empty -> no dims")
expect(rubmod.rubric_scores_valid(
    {"accuracy": {"a": 4, "b": 2}}, ["accuracy"]), "valid: exact match")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": 4, "b": 2}}, ["tone"]), "invalid: wrong dims")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": 4}}, ["accuracy"]), "invalid: missing side")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": 4, "b": 9}}, ["accuracy"]),
    "invalid: out-of-range score")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": True, "b": 2}}, ["accuracy"]),
    "invalid: bool rejected as int")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": "4", "b": 2}}, ["accuracy"]),
    "invalid: string rejected as int")
expect(not rubmod.rubric_scores_valid("nope", ["accuracy"]),
       "invalid: non-dict rubric")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": 4, "b": 2}, "extra": {"a": 1, "b": 1}},
    ["accuracy"]), "invalid: extra dimensions")
expect(rubmod.rubric_scores_valid(None, []), "valid: empty -> empty")
expect(not rubmod.rubric_scores_valid(
    {"accuracy": {"a": 4, "b": 2}}, []), "invalid: dims off, rubric on")


# ============ Fixtures ============
with app.app_context():
    admin = User(name="Ada Admin", email="rub-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    ann1 = User(name="Ann One", email="one@brydl.ng", role="annotator",
                terms_version="2.1")
    ann1.set_password("annot-pass-1")
    ann2 = User(name="Ann Two", email="two@brydl.ng", role="annotator",
                terms_version="2.1")
    ann2.set_password("annot-pass-2")
    db.session.add_all([admin, ann1, ann2])
    db.session.flush()
    admin_id, a1_id, a2_id = admin.id, ann1.id, ann2.id

    p_rub = Project(name="Egg rubric", client_name="Lab",
                    task_type="preference", pay_per_task=Decimal("0.30"),
                    required_annotations=1, instructions="Pick.",
                    rubric_dimensions=DIMS_RAW, created_by=admin_id)
    p_plain = Project(name="Egg plain", client_name="Lab",
                      task_type="preference",
                      pay_per_task=Decimal("0.30"),
                      required_annotations=1, instructions="Pick.",
                      created_by=admin_id)
    p_pair = Project(name="Egg pair", client_name="Lab",
                     task_type="preference", pay_per_task=Decimal("0.30"),
                     required_annotations=2, instructions="Pick.",
                     rubric_dimensions=DIMS_RAW, created_by=admin_id)
    db.session.add_all([p_rub, p_plain, p_pair])
    db.session.flush()
    p_rub_id, p_plain_id, p_pair_id = p_rub.id, p_plain.id, p_pair.id

    t1 = Task(input_data=DOC, project_id=p_rub_id, status="assigned",
              assigned_to=a1_id)
    t2 = Task(input_data=DOC, project_id=p_plain_id, status="assigned",
              assigned_to=a1_id)
    t3 = Task(input_data=DOC, project_id=p_rub_id, status="assigned",
              assigned_to=a1_id)
    g1 = Task(input_data=DOC, project_id=p_pair_id, status="assigned",
              assigned_to=a1_id)
    g2 = Task(input_data=DOC, project_id=p_pair_id, status="assigned",
              assigned_to=a2_id)
    grp = ConsensusGroup(project_id=p_pair_id, required_count=2,
                         status="pending")
    db.session.add(grp)
    db.session.flush()
    g1.consensus_group_id = grp.id
    g2.consensus_group_id = grp.id
    db.session.add_all([t1, t2, t3, g1, g2])
    db.session.commit()
    ann1.record_consent()
    ann2.record_consent()
    db.session.commit()
    t1_id, t2_id, t3_id = t1.id, t2.id, t3.id
    g1_id, g2_id = g1.id, g2.id

admin_c = app.test_client()
login(admin_c, "rub-admin@brydl.ng", "admin-pass-1")
ann_c = app.test_client()
login(ann_c, "one@brydl.ng", "annot-pass-1")
ann2_c = app.test_client()
login(ann2_c, "two@brydl.ng", "annot-pass-2")

# ============ Section 2: project form ============
print("== Section 2: project form rubric config ==")
page = admin_c.get("/admin/projects/new").get_data(as_text=True)
expect("rubric_dimensions" in page and "Rubric dimensions" in page,
       "project form shows the rubric field")
csrf = get_csrf(page)
mk = admin_c.post("/admin/projects/new", data={
    "name": "Fresh rubric", "client_name": "Lab",
    "task_type": "preference", "pay_per_task": "0.30",
    "instructions": "Pick better.", "rubric_dimensions": "helpfulness",
    "required_annotations": "1", "csrf_token": csrf,
})
expect(mk.status_code == 302, "project with rubric creates")
with app.app_context():
    fresh = db.session.scalar(db.select(Project).where(
        Project.name == "Fresh rubric"))
    fresh_id = fresh.id
    expect(fresh.rubric_dimensions == "helpfulness",
           "rubric dimensions persisted newline-normalized")
edit_page = admin_c.get(f"/admin/projects/{fresh_id}/edit"
                        ).get_data(as_text=True)
expect("helpfulness" in edit_page, "edit form prefills dimensions")
bad = admin_c.post("/admin/projects/new", data={
    "name": "Too many", "client_name": "Lab",
    "task_type": "preference", "pay_per_task": "0.30",
    "instructions": "Pick.",
    "rubric_dimensions": "\n".join(f"dim{i}" for i in range(7)),
    "required_annotations": "1", "csrf_token": csrf,
}, follow_redirects=True)
expect("at most 6" in bad.get_data(as_text=True),
       "over-cap dimension list rejected with flash")
with app.app_context():
    count = db.session.scalar(db.select(db.func.count(Project.id)).where(
        Project.name == "Too many"))
expect(count == 0, "rejected rubric created no project")

# ============ Section 3: annotator sliders ============
print("== Section 3: annotator preference sliders ==")
with app.app_context():
    flipped1 = _preference_flipped(t1_id, a1_id)
task_page = ann_c.get(f"/annotator/task/{t1_id}").get_data(as_text=True)
expect(task_page.count('class="form-range"') == 4,
       "four sliders render (2 dims x 2 responses)")
expect("accuracy" in task_page and "tone" in task_page,
       "dimension labels render")
first = "rubric_0_b" if flipped1 else "rubric_0_a"
second = "rubric_0_a" if flipped1 else "rubric_0_b"
expect(task_page.index(f'name="{first}"')
       < task_page.index(f'name="{second}"'),
       "left card carries the canonical side the annotator sees")
plain_page = ann_c.get(f"/annotator/task/{t2_id}").get_data(as_text=True)
expect('form-range' not in plain_page,
       "no sliders in a project without dimensions")

JUST = "B is far more complete and correct for the asked question."
csrf_t = get_csrf(task_page)
ok = ann_c.post(f"/annotator/task/{t1_id}", data={
    "chosen": "a", "justification": JUST,
    "rubric_0_a": "4", "rubric_0_b": "2",
    "rubric_1_a": "3", "rubric_1_b": "5",
    "csrf_token": csrf_t})
expect(ok.status_code == 302, "submission with sliders accepted")
with app.app_context():
    sub = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t1_id))
    sub_id = sub.id
    doc = json.loads(sub.output_data)
    expect(doc["rubric"] == {"accuracy": {"a": 4, "b": 2},
                             "tone": {"a": 3, "b": 5}},
           "rubric stored canonical (int scores)")
    expect(doc["chosen"] == ("b" if flipped1 else "a"),
           "choice mapping unaffected by the rubric")

tamp = ann_c.post(f"/annotator/task/{t3_id}", data={
    "chosen": "a", "justification": JUST,
    "rubric_0_a": "9", "rubric_0_b": "2",
    "rubric_1_a": "3", "rubric_1_b": "5",
    "csrf_token": get_csrf(ann_c.get(f"/annotator/task/{t3_id}"
                                     ).get_data(as_text=True))})
expect(tamp.status_code == 200
       and "Scores must be 1 to 5" in tamp.get_data(as_text=True),
       "out-of-range slider value bounces on the form")
missing = ann_c.post(f"/annotator/task/{t3_id}", data={
    "chosen": "a", "justification": JUST,
    "rubric_0_a": "4", "rubric_0_b": "2",
    "rubric_1_a": "3",
    "csrf_token": get_csrf(ann_c.get(f"/annotator/task/{t3_id}"
                                     ).get_data(as_text=True))})
expect("Every rubric slider needs a value" in missing.get_data(
    as_text=True), "missing slider bounces on the form")
with app.app_context():
    count = db.session.scalar(db.select(db.func.count(
        Submission.id)).where(Submission.task_id == t3_id))
expect(count == 0, "bounced submissions created no rows")

# ============ Section 4: review + arbitrate display ============
print("== Section 4: review + arbitration display ==")
detail = admin_c.get(f"/admin/review/{sub_id}").get_data(as_text=True)
expect("accuracy" in detail and "tone" in detail
       and "Rubric" in detail,
       "review detail shows the rubric table")
expect(re.search(r"<td>scores\.a</td>", detail) is None
       and detail.count("<td class=\"text-center\">4</td>") >= 1,
       "rubric scores render as values, not expressions")
plain_sub_page = None
with app.app_context():
    from models import Submission as S
    plain = Submission(task_id=t2_id, annotator_id=a1_id,
                       output_data=json.dumps({
                           "kind": "preference", "chosen": "a",
                           "seen_order": "ab", "justification": JUST}),
                       qa_score=90.0, status="submitted",
                       submitted_at=__import__("models").utc_now(),
                       reviewed_at=None)
    db.session.add(plain)
    db.session.commit()
    plain_id = plain.id
plain_detail = admin_c.get(f"/admin/review/{plain_id}").get_data(
    as_text=True)
expect("Rubric" not in plain_detail.split("Annotator output")[1]
       .split("Your verdict")[0],
       "no rubric table for non-rubric submissions")

# consensus pair: both annotators submit, arbitration shows rubric
for client, tid, uid, shown in (
    (ann_c, g1_id, a1_id, "a"), (ann2_c, g2_id, a2_id, "b")
):
    page = client.get(f"/annotator/task/{tid}").get_data(as_text=True)
    with app.app_context():
        flipped_i = _preference_flipped(tid, uid)
    canonical = shown if not flipped_i else (
        "b" if shown == "a" else "a")
    r = client.post(f"/annotator/task/{tid}", data={
        "chosen": shown, "justification": JUST,
        "rubric_0_a": "4", "rubric_0_b": "2",
        "rubric_1_a": "3", "rubric_1_b": "5",
        "csrf_token": get_csrf(page)})
    assert canonical in ("a", "b")
with app.app_context():
    grp_id = db.session.scalar(db.select(ConsensusGroup.id).where(
        ConsensusGroup.project_id == p_pair_id))
arb = admin_c.get(f"/admin/review/conflicts/{grp_id}").get_data(
    as_text=True)
expect("accuracy: A 4 · B 2" in arb, "arbitration shows rubric badges")

# gold secrecy + JSONL export rides the document
task_page = ann_c.get(f"/annotator/task/{g1_id}").get_data(as_text=True)
expect("seen_order" not in task_page and "rubric\"" not in task_page,
       "sibling/submitter metadata never reaches annotator pages")
# approve the rubric submission first (export scope = approved)
admin_c.post(f"/admin/review/{sub_id}", data={
    "action": "approve", "qa_score": "95",
    "csrf_token": get_csrf(admin_c.get(f"/admin/review/{sub_id}"
                                       ).get_data(as_text=True))})
exp = admin_c.post(f"/admin/projects/{p_rub_id}/export", data={
    "format": "jsonl",
    "csrf_token": get_csrf(admin_c.get(
        f"/admin/projects/{p_rub_id}/export").get_data(as_text=True))})
record = json.loads(exp.get_data(as_text=True).splitlines()[0])
out_doc = record.get("output", record)
if isinstance(out_doc, str):
    out_doc = json.loads(out_doc)
expect(out_doc.get("rubric") == {"accuracy": {"a": 4, "b": 2},
                                 "tone": {"a": 3, "b": 5}},
       "JSONL export carries the rubric inside the output document")

print()
if FAILURES:
    print(f"RUBRIC TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL RUBRIC TESTS PASSED")
