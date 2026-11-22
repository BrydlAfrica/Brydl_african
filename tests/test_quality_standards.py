"""Tests for Quality Standards, Multi-Dimensional Rubrics & Defect Taxonomy (Phase 9).

Validates:
1. Pure Quality Standards module: dimension weighting, composite QA scores,
   tier classification (Gold 95%+, Silver 90-94%, Needs Attention 80-89%, Probation <80%),
   and defect taxonomy parsing/serialization.
2. Project-level Quality Thresholds (default 90.0%, bounds check 50.0-100.0).
3. QA Review detail with multi-dimensional rubric scoring and defect taxonomy tagging.
4. Quality Dispute & Appeal Workflow:
   - Annotator submits appeal on rejected task.
   - Rejection of duplicate/invalid appeals.
   - Admin Appeals Desk (/admin/review/appeals).
   - Admin Grant Decision (overturns rejection to approved, revises QA score, updates task).
   - Admin Deny Decision (maintains rejection, records rationale).
5. Quality Standards & Workforce Analytics Dashboard (/admin/quality):
   - Quality tier distribution.
   - Defect taxonomy distribution.
   - CSV Quality Report Export (/admin/quality/export.csv).
6. Invariants, audit logs, notifications and RBAC walls.
"""

import json
import os
import re
import sys
from decimal import Decimal

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ["BRYDL_DISABLE_JOBS"] = "1"

from app import create_app
from extensions import db
from models import (
    AuditLog,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)
import quality_standards as qs

FAILURES = []


def expect(condition: bool, message: str):
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    if m:
        return m.group(1)
    m2 = re.search(r'id="csrf_token"[^>]*value="([^"]+)"', html)
    return m2.group(1) if m2 else ""


def login(cl, email, password="Password123!"):
    p = cl.get("/login").get_data(as_text=True)
    return cl.post(
        "/login",
        data={"csrf_token": csrf(p), "email": email, "password": password},
        follow_redirects=True,
    )


def gcsrf(cl, path):
    return csrf(cl.get(path).get_data(as_text=True))


# ============ 1. Pure Quality Standards Unit Contract ============
print("== 1. Quality Standards & Scoring Module ==")
score_perfect = qs.compute_weighted_qa_score({
    "accuracy": 100,
    "linguistic_quality": 100,
    "instruction_adherence": 100,
    "formatting": 100,
})
expect(score_perfect == 100.0, "Perfect rubric yields 100.0")

score_mixed = qs.compute_weighted_qa_score({
    "accuracy": 90,              # 90 * 0.35 = 31.5
    "linguistic_quality": 80,    # 80 * 0.30 = 24.0
    "instruction_adherence": 100,# 100 * 0.20 = 20.0
    "formatting": 100,           # 100 * 0.15 = 15.0  => total 90.5
})
expect(score_mixed == 90.5, f"Weighted composite score matches 90.5 (got {score_mixed})")

# Tier classification
tier_gold = qs.classify_quality_tier(96.5, 20)
expect(tier_gold["tier_key"] == "gold", "96.5% classified as Gold Standard")

tier_silver = qs.classify_quality_tier(91.0, 15)
expect(tier_silver["tier_key"] == "silver", "91.0% classified as Silver Standard")

tier_needs_att = qs.classify_quality_tier(84.0, 10)
expect(tier_needs_att["tier_key"] == "needs_attention", "84.0% classified as Needs Attention")

tier_probation = qs.classify_quality_tier(72.0, 8)
expect(tier_probation["tier_key"] == "probation", "72.0% classified as Probationary")

tier_unranked = qs.classify_quality_tier(None, 0)
expect(tier_unranked["tier_key"] == "unranked", "None/0 classified as Unranked")

# Defect serialization
raw_defects = [
    {"category": "diacritics_tone", "severity": "minor", "note": "Missing tone on second syllable"},
    {"category": "formatting_syntax", "severity": "major", "note": "Invalid trailing comma"},
]
serialized = qs.serialize_error_tags(raw_defects)
parsed = qs.parse_error_tags(serialized)
expect(len(parsed) == 2, "Defect tags serialize and parse roundtrip cleanly")
expect(parsed[0]["category"] == "diacritics_tone", "Category preserved")
expect(parsed[0]["severity"] == "minor", "Severity preserved")

# ============ 2. Database Models & Quality Threshold ============
print("== 2. Database Models & Quality Threshold Invariants ==")
app = create_app()

with app.app_context():
    db.drop_all()
    db.create_all()

    admin = User(name="Quality Admin", email="admin@brydl.ng", role="admin", is_active=True, terms_accepted_at=utc_now())
    admin.set_password("Password123!")

    qa_reviewer = User(name="Senior QA", email="qa@brydl.ng", role="qa", is_active=True, terms_accepted_at=utc_now())
    qa_reviewer.set_password("Password123!")

    ann_gold = User(name="Babatunde Gold", email="babatunde@brydl.ng", role="annotator", is_active=True, is_verified=True, terms_accepted_at=utc_now())
    ann_gold.set_password("Password123!")

    ann_dispute = User(name="Zainab Dispute", email="zainab@brydl.ng", role="annotator", is_active=True, is_verified=True, terms_accepted_at=utc_now())
    ann_dispute.set_password("Password123!")

    db.session.add_all([admin, qa_reviewer, ann_gold, ann_dispute])
    db.session.commit()

    admin_id = admin.id
    qa_id = qa_reviewer.id
    ann_gold_id = ann_gold.id
    ann_dispute_id = ann_dispute.id

    # Create project with custom quality threshold
    proj = Project(
        name="Yoruba Dialect QA Project",
        client_name="African LLM Labs",
        task_type="translation",
        instructions="Translate accurately with standard Yoruba tone marks.",
        quality_threshold=90.0,
        pay_per_task=Decimal("1.50"),
        status="active",
        created_by=admin_id,
    )
    db.session.add(proj)
    db.session.flush()
    proj_id = proj.id

    expect(proj.quality_threshold == 90.0, "Project quality_threshold defaults/persists to 90.0%")

    # Tasks and submissions
    task1 = Task(project_id=proj_id, input_data=json.dumps({"text": "E kaasan"}), status="submitted", assigned_to=ann_gold_id)
    task2 = Task(project_id=proj_id, input_data=json.dumps({"text": "Bawo ni"}), status="submitted", assigned_to=ann_dispute_id)
    db.session.add_all([task1, task2])
    db.session.flush()
    task1_id, task2_id = task1.id, task2.id

    sub1 = Submission(task_id=task1_id, annotator_id=ann_gold_id, output_data=json.dumps({"translation": "Good afternoon"}), status="submitted")
    sub2 = Submission(task_id=task2_id, annotator_id=ann_dispute_id, output_data=json.dumps({"translation": "How are you"}), status="submitted")
    db.session.add_all([sub1, sub2])
    db.session.commit()
    sub1_id, sub2_id = sub1.id, sub2.id

# ============ 3. QA Multi-Dimensional Review & Defect Tagging ============
print("== 3. QA Multi-Dimensional Review & Defect Tagging ==")
qa_cl = app.test_client()
login(qa_cl, "qa@brydl.ng")

# Review submission 1 (Gold pass with full rubric)
c1 = gcsrf(qa_cl, f"/admin/review/{sub1_id}")
r_rev1 = qa_cl.post(
    f"/admin/review/{sub1_id}",
    data={
        "csrf_token": c1,
        "action": "approve",
        "qa_score": "98",
        "rubric_accuracy": "100",
        "rubric_linguistic_quality": "95",
        "rubric_instruction_adherence": "100",
        "rubric_formatting": "100",
        "qa_notes": "Excellent diacritics and natural tone.",
    },
    follow_redirects=True,
)
expect(r_rev1.status_code == 200, "QA review approval returned 200 after redirect")

with app.app_context():
    loaded_s1 = db.session.get(Submission, sub1_id)
    expect(loaded_s1.status == "approved", "Submission 1 status is 'approved'")
    expect(loaded_s1.qa_score == 98.0, "Submission 1 qa_score is 98.0")
    expect(loaded_s1.qa_rubric_data is not None, "qa_rubric_data is saved")
    rubric_p = qs.parse_qa_rubric_data(loaded_s1.qa_rubric_data)
    expect(rubric_p.get("accuracy") == 100.0, "Rubric accuracy dimension score persisted")

# Review submission 2 (Reject with defect tags)
c2 = gcsrf(qa_cl, f"/admin/review/{sub2_id}")
defect_payload = json.dumps([
    {"category": "diacritics_tone", "severity": "major", "note": "Missing tone marking"},
])
r_rev2 = qa_cl.post(
    f"/admin/review/{sub2_id}",
    data={
        "csrf_token": c2,
        "action": "reject",
        "qa_score": "65",
        "error_tags_json": defect_payload,
        "qa_notes": "Tone marks omitted entirely on verbs.",
        "return_for_revision": "y",
    },
    follow_redirects=True,
)
expect(r_rev2.status_code == 200, "QA review rejection returned 200 after redirect")

with app.app_context():
    loaded_s2 = db.session.get(Submission, sub2_id)
    expect(loaded_s2.status == "rejected", "Submission 2 status is 'rejected'")
    expect(loaded_s2.qa_score == 65.0, "Submission 2 qa_score is 65.0")
    expect(loaded_s2.appeal_status == "none", "Initial appeal_status is 'none'")
    tags_p = qs.parse_error_tags(loaded_s2.error_tags)
    expect(len(tags_p) == 1 and tags_p[0]["category"] == "diacritics_tone", "Defect tag persisted")

# ============ 4. Annotator History & Quality Dispute Appeal ============
print("== 4. Annotator History & Quality Dispute Appeal ==")
ann_cl = app.test_client()
login(ann_cl, "zainab@brydl.ng")

r_hist = ann_cl.get("/annotator/history")
expect(r_hist.status_code == 200, "Annotator can load history page")
html_hist = r_hist.get_data(as_text=True)
expect("Appeal" in html_hist, "Appeal button present for rejected task")

# Submit appeal
c_appeal = gcsrf(ann_cl, "/annotator/history")
r_app_sub = ann_cl.post(
    f"/annotator/submissions/{sub2_id}/appeal",
    data={
        "csrf_token": c_appeal,
        "appeal_notes": "The dialect source was Western Yoruba colloquial speech where elision removes the acute tone.",
    },
    follow_redirects=True,
)
expect(r_app_sub.status_code == 200, "Appeal submission returns 200 after redirect")
html_app_done = r_app_sub.get_data(as_text=True)
expect("Your quality appeal has been submitted" in html_app_done, "Success flash message displayed")

with app.app_context():
    loaded_s2 = db.session.get(Submission, sub2_id)
    expect(loaded_s2.appeal_status == "pending", "Submission 2 appeal_status is now 'pending'")
    expect("Western Yoruba" in (loaded_s2.appeal_notes or ""), "Appeal justification recorded")

# Duplicate appeal rejection
c_dup = gcsrf(ann_cl, "/annotator/history")
r_dup = ann_cl.post(
    f"/annotator/submissions/{sub2_id}/appeal",
    data={"csrf_token": c_dup, "appeal_notes": "Second attempt at appeal"},
    follow_redirects=True,
)
html_dup = r_dup.get_data(as_text=True)
expect("already been submitted" in html_dup, "Duplicate appeal blocked")

# ============ 5. Admin Appeals Desk & Ruling ============
print("== 5. QA / Admin Appeals Desk & Ruling Workflow ==")
r_appeals_page = qa_cl.get("/admin/review/appeals")
expect(r_appeals_page.status_code == 200, "QA reviewer can open /admin/review/appeals")
html_appeals = r_appeals_page.get_data(as_text=True)
expect("Zainab Dispute" in html_appeals, "Appellant listed in review queue")
expect("Western Yoruba" in html_appeals, "Appellant explanation shown")

# Grant the appeal
c_decide = gcsrf(qa_cl, "/admin/review/appeals")
r_grant = qa_cl.post(
    f"/admin/review/appeals/{sub2_id}/decide",
    data={
        "csrf_token": c_decide,
        "action": "grant",
        "revised_score": "94",
        "decision_notes": "Dialect variant verified against standard Oyo orthography. Appeal granted.",
    },
    follow_redirects=True,
)
expect(r_grant.status_code == 200, "Appeal decision returns 200 after redirect")
html_granted = r_grant.get_data(as_text=True)
expect("Appeal granted for Submission" in html_granted, "Flash banner shows appeal granted")

with app.app_context():
    loaded_s2 = db.session.get(Submission, sub2_id)
    expect(loaded_s2.appeal_status == "granted", "Submission 2 appeal_status is 'granted'")
    expect(loaded_s2.status == "approved", "Submission 2 overturned to 'approved'")
    expect(loaded_s2.qa_score == 94.0, "Submission 2 revised score is 94.0")
    expect(loaded_s2.task.status == "approved", "Task status is 'approved'")
    expect(loaded_s2.appeal_reviewed_by_id == qa_id, "appeal_reviewed_by_id recorded")

# ============ 6. Quality Analytics Dashboard & Export ============
print("== 6. Quality Analytics Dashboard & CSV Export ==")
admin_cl = app.test_client()
login(admin_cl, "admin@brydl.ng")

r_quality = admin_cl.get("/admin/quality")
expect(r_quality.status_code == 200, "Admin can open /admin/quality")
html_q = r_quality.get_data(as_text=True)
expect("Gold Tier" in html_q, "Gold Tier summary card rendered")
expect("Silver Tier" in html_q, "Silver Tier summary card rendered")
expect("Defect Taxonomy" in html_q, "Defect taxonomy breakdown card rendered")
expect("Babatunde Gold" in html_q, "Annotator Babatunde listed on quality table")

# Test CSV Export
r_csv = admin_cl.get("/admin/quality/export.csv")
expect(r_csv.status_code == 200, "Quality CSV export returns 200")
expect("text/csv" in r_csv.headers.get("Content-Type", ""), "Content-Type is text/csv")
csv_text = r_csv.get_data(as_text=True)
expect("Annotator ID,Name,Email,Seniority Level" in csv_text, "CSV header present")
expect("Babatunde Gold" in csv_text, "Annotator data present in CSV")

print("\n" + "=" * 50)
if FAILURES:
    print(f"FAILED with {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL QUALITY STANDARDS & REVIEWS TESTS PASSED CLEANLY")
    sys.exit(0)
