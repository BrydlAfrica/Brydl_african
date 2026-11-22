"""Test suite for Phase 15: Annotator Recruitment, Screening, Qualification and 9-Stage Onboarding System."""
from __future__ import annotations

import os
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

from app import create_app
from extensions import db
from models import (
    Applicant,
    ApplicantTrialSubmission,
    User,
    utc_now,
)
from onboarding_bank import CALIBRATION_TASKS, GUIDELINES_QUIZ, LANGUAGE_TESTS

failures = 0
checks = 0


def expect(cond, msg):
    global failures, checks
    checks += 1
    if cond:
        print(f"   ok: {msg}")
    else:
        failures += 1
        print(f"  FAIL: {msg}")


def get_csrf(client, url="/onboard/apply"):
    r = client.get(url)
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.get_data(as_text=True))
    return m.group(1) if m else ""


app = create_app()

with app.app_context():
    db.drop_all()
    db.create_all()

    # Seed Admin and PM
    admin = User(name="Ada Admin", email="admin@brydl.ai", role="admin")
    admin.set_password("admin-pass-123")

    pm = User(name="Paul PM", email="pm@brydl.ai", role="project_manager")
    pm.set_password("pm-pass-123")

    db.session.add_all([admin, pm])
    db.session.commit()

client = app.test_client()

print("\n== 1. Stage 1: Public Application & Profiling ==")
r_apply_page = client.get("/onboard/apply")
expect(r_apply_page.status_code == 200, "GET /onboard/apply returns 200")
expect("Annotator Application" in r_apply_page.get_data(as_text=True), "Application title rendered")

csrf = get_csrf(client, "/onboard/apply")

# Submit application
r_apply_post = client.post(
    "/onboard/apply",
    data={
        "csrf_token": csrf,
        "name": "Amina Bello",
        "email": "amina@hausa.ng",
        "password": "candidate-pass-123",
        "country": "Nigeria",
        "state_province": "Kano State",
        "phone_whatsapp": "+2348030001122",
        "primary_language": "hausa",
        "dialect": "Kano Hausa",
        "domain_expertise": "Linguistics",
        "device_type": "laptop",
        "weekly_hours": "20",
        "competencies": ["rlhf", "sft", "redteam", "translation"],
    },
    follow_redirects=True,
)
expect(r_apply_post.status_code == 200, "POST /onboard/apply succeeds and redirects")
with app.app_context():
    app_user = db.session.query(User).filter_by(email="amina@hausa.ng").first()
    expect(app_user is not None, "Applicant user created")
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant is not None, "Applicant profile created")
    expect(applicant.stage == "guidelines", "Applicant stage advanced to 'guidelines'")
    expect(applicant.primary_language == "hausa", "Language stored as 'hausa'")
    expect(applicant.dialect == "Kano Hausa", "Dialect stored as 'Kano Hausa'")

print("\n== 2. Applicant Quarantine Wall ==")
# Applicant trying to access production annotator dashboard must be quarantined
r_quarantine = client.get("/annotator/", follow_redirects=False)
expect(r_quarantine.status_code == 302, "GET /annotator/ redirects quarantined applicant (got 302)")
expect("/onboard/status" in r_quarantine.headers.get("Location", ""), "Redirects to /onboard/status")

print("\n== 3. Stage 2: Guidelines Reading & Gating Quiz ==")
r_guide = client.get("/onboard/guidelines")
expect(r_guide.status_code == 200, "GET /onboard/guidelines returns 200")
expect("Zero-Tolerance AI" in r_guide.get_data(as_text=True), "Guidelines content rendered")

r_quiz_page = client.get("/onboard/guidelines-quiz")
expect(r_quiz_page.status_code == 200, "GET /onboard/guidelines-quiz returns 200")

csrf_quiz = get_csrf(client, "/onboard/guidelines-quiz")

# Test failing quiz (<100%)
failing_data = {f"q_{q['id']}": "a" for q in GUIDELINES_QUIZ}
failing_data["csrf_token"] = csrf_quiz
r_fail_quiz = client.post("/onboard/guidelines-quiz", data=failing_data, follow_redirects=True)
expect(r_fail_quiz.status_code == 200, "Failing quiz handled without 500")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(not applicant.guidelines_passed, "Applicant has not passed guidelines")

# Test passing quiz (100%)
csrf_quiz2 = get_csrf(client, "/onboard/guidelines-quiz")
passing_data = {f"q_{q['id']}": q["correct"] for q in GUIDELINES_QUIZ}
passing_data["csrf_token"] = csrf_quiz2
r_pass_quiz = client.post("/onboard/guidelines-quiz", data=passing_data, follow_redirects=True)
expect(r_pass_quiz.status_code == 200, "Passing quiz succeeds and redirects to test")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.guidelines_passed, "Applicant guidelines_passed is True")
    expect(applicant.stage == "test", "Applicant stage advanced to 'test'")

print("\n== 4. Stage 3: Timed Qualification Assessment ==")
r_test_page = client.get("/onboard/test")
expect(r_test_page.status_code == 200, "GET /onboard/test returns 200")
expect("Hausa Language" in r_test_page.get_data(as_text=True), "Tailored Hausa test rendered")

csrf_test = get_csrf(client, "/onboard/test")

# Submit qualification test
hausa_test = LANGUAGE_TESTS["hausa"]
test_submission_data = {
    "csrf_token": csrf_test,
    "test_q_ha-01": "b",
    "test_q_ha-02": "b",
    "test_q_ha-03": "b",
    "test_q_ha-04": "b",
    "test_q_ha-05": "b",
    "practical_response": "Kamfanin fasahar kuɗi ya ƙaddamar da tsarin basirar na'ura mai ƙwaƙwalwa (AI) don gano haramtattun ma'amaloli.",
}
r_test_post = client.post("/onboard/test", data=test_submission_data, follow_redirects=True)
expect(r_test_post.status_code == 200, "POST /onboard/test succeeds and redirects to status")

with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.test_score_composite >= 85, f"Test composite score >= 85 (got {applicant.test_score_composite}%)")
    expect(applicant.admin_decision == "approved", "Auto-decision is 'approved'")
    expect(applicant.stage == "decision", "Stage is 'decision'")
    expect(applicant.assigned_pod == "Hausa Evaluation Pod", "Assigned to Hausa Evaluation Pod")

print("\n== 5. Stage 4/5/6: Onboarding Status Tracker ==")
r_status = client.get("/onboard/status")
expect(r_status.status_code == 200, "GET /onboard/status returns 200")
expect("9-Stage Workforce Qualification Stepper" in r_status.get_data(as_text=True), "Stepper rendered")
expect("Application Approved" in r_status.get_data(as_text=True), "Approved banner rendered")

print("\n== 6. Admin & PM Workforce Talent Hub ==")
# Log in as Admin
admin_client = app.test_client()
csrf_login = get_csrf(admin_client, "/login")
admin_client.post("/login", data={"csrf_token": csrf_login, "email": "admin@brydl.ai", "password": "admin-pass-123"}, follow_redirects=True)

r_admin_apps = admin_client.get("/admin/applicants")
expect(r_admin_apps.status_code == 200, "GET /admin/applicants returns 200")
expect("Workforce Onboarding &amp; Talent Hub" in r_admin_apps.get_data(as_text=True), "Admin Hub rendered")
expect("Amina Bello" in r_admin_apps.get_data(as_text=True), "Applicant listed in directory")

# Admin detail inspection
r_app_detail = admin_client.get(f"/admin/applicants/{applicant.id}")
expect(r_app_detail.status_code == 200, f"GET /admin/applicants/{applicant.id} returns 200")
expect("Applicant Dossier" in r_app_detail.get_data(as_text=True), "Applicant Dossier rendered")

csrf_dec = get_csrf(admin_client, f"/admin/applicants/{applicant.id}")

# Admin decision confirmation
r_dec = admin_client.post(
    f"/admin/applicants/{applicant.id}/decision",
    data={"csrf_token": csrf_dec, "decision": "approved", "notes": "Excellent Hausa orthography and safety reasoning."},
    follow_redirects=True,
)
expect(r_dec.status_code == 200, "POST /admin/applicants/<id>/decision succeeds")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.stage == "agreement", "Stage advanced to 'agreement'")

# PM Directory Access
pm_client = app.test_client()
csrf_pm_login = get_csrf(pm_client, "/login")
pm_client.post("/login", data={"csrf_token": csrf_pm_login, "email": "pm@brydl.ai", "password": "pm-pass-123"}, follow_redirects=True)
r_pm_apps = pm_client.get("/pm/applicants")
expect(r_pm_apps.status_code == 200, "GET /pm/applicants returns 200")

print("\n== 7. Stage 7: Contractor Agreement & NDA E-Signature ==")
r_agree_page = client.get("/onboard/agreement")
expect(r_agree_page.status_code == 200, "GET /onboard/agreement returns 200")

csrf_agree = get_csrf(client, "/onboard/agreement")

r_agree_post = client.post(
    "/onboard/agreement",
    data={
        "csrf_token": csrf_agree,
        "signature_name": "Amina Bello",
        "accept_nda": "on",
        "accept_terms": "on",
    },
    follow_redirects=True,
)
expect(r_agree_post.status_code == 200, "POST /onboard/agreement succeeds and redirects to trial")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.agreement_signed_at is not None, "Agreement signed_at recorded")
    expect(applicant.agreement_signature_hash is not None, "Signature SHA-256 hash recorded")
    expect(applicant.stage == "trial", "Stage advanced to 'trial'")

print("\n== 8. Stage 8: Calibration Trial Batch Sandbox ==")
r_trial_page = client.get("/onboard/trial")
expect(r_trial_page.status_code == 200, "GET /onboard/trial returns 200")
expect("10-Task Supervised Calibration Sandbox" in r_trial_page.get_data(as_text=True), "Trial batch sandbox rendered")

# Submit all 10 calibration tasks
for task in CALIBRATION_TASKS:
    csrf_trial = get_csrf(client, "/onboard/trial")
    r_sub = client.post(
        "/onboard/trial/submit",
        data={
            "csrf_token": csrf_trial,
            "task_index": str(task["index"]),
            "task_prompt": task["prompt"],
            "response_text": f"Accurate Hausa calibration response for task #{task['index']} with proper hooked letters: ɓ, ɗ, ƙ, ƴ.",
        },
        follow_redirects=True,
    )
    expect(r_sub.status_code == 200, f"Submitted calibration task #{task['index']}")

with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.trial_tasks_completed == 10, "All 10 trial tasks completed")
    expect(applicant.trial_passed, "Trial passed is True")
    expect(applicant.stage == "active", "Applicant stage graduated to 'active'")

print("\n== 9. Stage 9: Active Workforce Production Access ==")
# Now that stage == 'active', accessing /annotator/ is permitted
r_active_dash = client.get("/annotator/")
expect(r_active_dash.status_code == 200, "Active annotator successfully accesses /annotator/ (got 200)")

print()
if failures > 0:
    print(f"ONBOARDING TESTS FAILED ({failures}/{checks} checks)")
    sys.exit(1)
print(f"ALL ONBOARDING & 9-STAGE RECRUITMENT TESTS PASSED ({checks} checks)")
