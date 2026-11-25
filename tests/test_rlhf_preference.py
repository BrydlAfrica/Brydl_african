import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""RLHF Phase 1 suite: the preference (DPO) comparison task type.

Covers the full contract: side-by-side workspace with deterministic
per-annotator position randomization, gold-answer isolation, minimum
justification enforcement, canonical choice mapping on submit, admin
review rendering, quality gold accuracy for preference answers,
preference CSV import (paste + confirm) with gold normalization, and
JSONL export carrying the preference document.
"""
import json
import re
import shutil
import sys
from decimal import Decimal

from app import create_app
from extensions import db
from models import Project, Submission, Task, User
from routes.annotator import _preference_flipped
from routes.admin import _parse_task_lines, _preference_gold

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


PROMPT = "Which answer best explains why the sky is blue?"
RESP_A = "The sky is blue because of elastic air molecules."
RESP_B = ("Sunlight scatters off air molecules, and blue light "
          "scatters most, so the sky looks blue.")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ---- seed ----
with app.app_context():
    admin = User(name="Ada Admin", email="admin@brydl.ng", role="admin")
    admin.set_password("admin-pass-1")
    db.session.add(admin)
    db.session.flush()
    ann1 = User(name="Ada One", email="one@brydl.ng", role="annotator",
                terms_version="2.1")
    ann1.set_password("annot-pass-1")
    ann2 = User(name="Ben Two", email="two@brydl.ng", role="annotator",
                terms_version="2.1")
    ann2.set_password("annot-pass-1")
    db.session.add_all([ann1, ann2])
    db.session.flush()
    project = Project(name="RLHF pref", client_name="AI Lab",
                      task_type="preference",
                      pay_per_task=Decimal("1.25"),
                      instructions="Compare honestly.",
                      created_by=admin.id)
    db.session.add(project)
    db.session.flush()
    doc = json.dumps({"kind": "preference", "prompt": PROMPT,
                      "response_a": RESP_A, "response_b": RESP_B},
                     ensure_ascii=False)
    t1 = Task(project_id=project.id, input_data=doc,
              status="assigned", assigned_to=ann1.id)
    t2 = Task(project_id=project.id, input_data=doc,
              status="assigned", assigned_to=ann2.id)
    db.session.add_all([t1, t2])
    db.session.flush()
    # The gold answer is the canonical truth; make it the LEFT response
    # for ann1's deterministic view order so the annotator choosing the
    # genuinely-better left response hits the gold key.
    t1.expected_output = (
        "a" if not _preference_flipped(t1.id, ann1.id) else "b"
    )
    db.session.commit()
    ann1.record_consent(); ann2.record_consent()
    db.session.commit()
    project_id, t1_id, t2_id = project.id, t1.id, t2.id
    ann1_id, ann2_id = ann1.id, ann2.id

admin_c = app.test_client()
login(admin_c, "admin@brydl.ng", "admin-pass-1")
ann1_c = app.test_client()
login(ann1_c, "one@brydl.ng", "annot-pass-1")

# ---- 1. workspace: comparison UI + position randomization --------------
print("== 1. comparison workspace ==")
page = ann1_c.get(f"/annotator/task/{t1_id}").get_data(as_text=True)
expect(PROMPT in page and RESP_A in page and RESP_B in page,
       "prompt + both responses render")
expect("Response A" in page and "Response B" in page,
       "displayed labels A/B present")
expect("expected_output" not in page and '{"kind"' not in page,
       "no gold reference, no raw JSON in the workspace")
flipped1 = _preference_flipped(t1_id, ann1_id)
first_pos = page.find(RESP_B if flipped1 else RESP_A)
second_pos = page.find(RESP_A if flipped1 else RESP_B)
expect(0 <= first_pos < second_pos,
       f"display order matches the deterministic flip (flipped={flipped1})")
expect("Minimum 40 characters" in page,
       "justification minimum is communicated")

# ---- 2. validation: short justification refused ------------------------
print("== 2. justification minimum ==")
r = ann1_c.post(f"/annotator/task/{t1_id}", data={
    "chosen": "a", "justification": "too short",
    "csrf_token": get_csrf(page)}, follow_redirects=True)
expect("between 40 and" in r.get_data(as_text=True),
       "short justification refused with a clear message")
with app.app_context():
    status = db.session.scalar(db.select(Task.status).where(
        Task.id == t1_id))
expect(status == "assigned", "task still open after refused submit")

# ---- 3. submit: canonical mapping of the displayed choice --------------
print("== 3. canonical choice mapping ==")
just = ("Response B correctly explains Rayleigh scattering while A "
        "only names molecules without connecting them to blueness.")
r = ann1_c.post(f"/annotator/task/{t1_id}", data={
    "chosen": "a",  # LEFT as displayed
    "justification": just,
    "csrf_token": get_csrf(page)}, follow_redirects=True)
expect("awaiting QA review" in r.get_data(as_text=True),
       "submission accepted")
expected_canonical = "b" if flipped1 else "a"
with app.app_context():
    row = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t1_id))
    output = json.loads(row.output_data)
expect(output["chosen"] == expected_canonical
       and output["seen_order"] == ("ba" if flipped1 else "ab")
       and output["justification"] == just
       and output["kind"] == "preference",
       f"stored canonical chosen={output['chosen']} with seen order")

# ---- 4. second annotator, no-gold task, admin review -------------------
print("== 4. review + quality ==")
ann2_c = app.test_client()
login(ann2_c, "two@brydl.ng", "annot-pass-1")
page2 = ann2_c.get(f"/annotator/task/{t2_id}").get_data(as_text=True)
flipped2 = _preference_flipped(t2_id, ann2_id)
ann2_c.post(f"/annotator/task/{t2_id}", data={
    "chosen": "b", "justification": just,
    "csrf_token": get_csrf(page2)}, follow_redirects=True)
detail = admin_c.get("/admin/review").get_data(as_text=True)
with app.app_context():
    sub_ids = db.session.scalars(db.select(Submission.id).order_by(
        Submission.id)).all()
detail = admin_c.get(f"/admin/review/{sub_ids[0]}").get_data(as_text=True)
expect("Response A (canonical)" in detail
       and "Response B (canonical)" in detail,
       "review shows canonical response columns")
expect("Chose: Response" in detail and "Justification:" in detail,
       "review renders the choice + justification readably")
# approve both
csrf = get_csrf(admin_c.get("/admin/review").get_data(as_text=True))
admin_c.post("/admin/review/bulk", data={
    "submission_ids": [str(i) for i in sub_ids],
    "action": "approve", "qa_score": "90", "csrf_token": csrf},
    follow_redirects=True)
with app.app_context():
    statuses = db.session.scalars(db.select(Task.status).where(
        Task.id.in_([t1_id, t2_id]))).all()
expect(statuses == ["approved", "approved"], "both tasks approved")

quality = admin_c.get("/admin/quality").get_data(as_text=True)
with app.app_context():
    gold_row = db.session.scalar(db.select(Submission).where(
        Submission.task_id == t1_id))
    got_chosen = json.loads(gold_row.output_data)["chosen"]
expect("Gold accuracy" in quality,
       "quality page renders gold accuracy")
expected_gold = "a" if not flipped1 else "b"
verdict = "correct" if got_chosen == expected_gold else "miss"
expect(got_chosen == expected_gold,
       "gold key matches the annotator's canonical choice"
       f" (gold {expected_gold}, chose {got_chosen} — {verdict})")

# ---- 5. preference import (unit + confirm route) -----------------------
print("== 5. preference import ==")
good_line = json.dumps({"prompt": PROMPT, "response_a": RESP_A,
                        "response_b": RESP_B}, ensure_ascii=False)
parsed, errors = _parse_task_lines(
    f"{good_line}\tb\n{good_line}\tB\nplain text task\tgold\n"
    f"{good_line}\tc\n"
)
paste_ok = len(parsed) == 3 and len(errors) == 1
detail_msg = ("paste parser: 3 valid lines + 1 gold error" if paste_ok
              else f"paste parser unexpected: {len(parsed)} ok, {errors}")
expect(paste_ok, detail_msg)
if parsed:
    expect(all(_preference_gold(g)[0] in (None, "a", "b")
               for _t, g in parsed),
           "gold cells normalized to a/b")
bad_doc = json.dumps({"prompt": PROMPT, "response_a": RESP_A})
parsed2, errors2 = _parse_task_lines(f"{bad_doc}\tb\n")
expect(parsed2 and not errors2,
       "incomplete preference doc falls back to plain-text task")
expect(_preference_gold("C")[1] != "",
       "invalid preference gold produces an error")
# end-to-end confirm with a preference row
n_before = 0
with app.app_context():
    n_before = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.project_id == project_id))
csrf = get_csrf(admin_c.get("/admin/tasks").get_data(as_text=True))
r = admin_c.post("/admin/tasks/import/confirm", data={
    "project": str(project_id), "n_rows": "1",
    "row_include_0": "y", "row_text_0": good_line, "row_gold_0": "B",
    "csrf_token": csrf}, follow_redirects=True)
with app.app_context():
    n_after = db.session.scalar(db.select(db.func.count(Task.id)).where(
        Task.project_id == project_id))
    imported = db.session.scalars(db.select(Task).where(
        Task.project_id == project_id).order_by(Task.id.desc())).first()
expect(n_after == n_before + 1, "confirm route inserts the row")
expect(imported is not None
       and (imported.expected_output in ("a", "b")),
       f"imported gold normalized ({imported.expected_output!r})")

# ---- 6. JSONL export carries the preference document -------------------
print("== 6. export ==")
csrf = get_csrf(admin_c.get(
    f"/admin/projects/{project_id}/export").get_data(as_text=True))
r = admin_c.post(f"/admin/projects/{project_id}/export", data={
    "format": "jsonl", "scope": "reviewed", "csrf_token": csrf})
body = r.get_data(as_text=True)
records = [json.loads(line) for line in body.splitlines() if line]
prefs = [rec for rec in records
         if isinstance(rec.get("output"), dict)
         and rec["output"].get("kind") == "preference"]
expect(len(prefs) >= 2, f"{len(prefs)} preference records exported")
expect(all("justification" in rec["output"]
           and rec["output"]["chosen"] in ("a", "b")
           for rec in prefs),
       "exported records carry chosen + justification")
expect(all(rec["is_gold"] for rec in prefs if rec["task_id"] == t1_id),
       "gold flag travels with the gold task")

shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)
print()
if FAILURES:
    print(f"RLHF PREFERENCE TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL RLHF PREFERENCE TESTS PASSED")
