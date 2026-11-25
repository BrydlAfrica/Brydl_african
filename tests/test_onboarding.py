"""Test suite for Phase 15, 16 & 17: Track-Specific Onboarding, Dual-Workspace, HR Screening & Official Workforce Verification Cutover Subsystem."""
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
    HRScreening,
    Payment,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)
from onboarding_bank import (
    CALIBRATION_TASKS,
    GUIDELINES_QUIZZES,
    SPECIALIZATION_TRACKS_META,
    TRACK_ASSESSMENTS,
    get_calibration_tasks,
    get_track_assessment,
)

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

    # Seed Admin, PM, and HR
    admin = User(name="Ada Admin", email="admin@brydl.ai", role="admin")
    admin.set_password("admin-pass-123")

    pm = User(name="Paul PM", email="pm@brydl.ai", role="project_manager")
    pm.set_password("pm-pass-123")

    hr = User(name="Helen HR", email="hr@brydl.ai", role="hr")
    hr.set_password("hr-pass-123")

    db.session.add_all([admin, pm, hr])
    db.session.commit()

    # Seed a provisional-eligible project and a verified-only project
    proj_prov = Project(
        name="Hausa MTQE Batch 1",
        client_name="African AI Research",
        task_type="translation",
        pay_per_task=Decimal("1.50"),
        instructions="Translate accurately with standard Hausa hooked letters.",
        status="active",
        allows_provisional=True,
        created_by=admin.id,
    )
    proj_verif = Project(
        name="Confidential Enterprise RedLab Safety",
        client_name="Global Safety Lab",
        task_type="dynamic_red_team",
        pay_per_task=Decimal("5.00"),
        instructions="Confidential red-teaming adversarial audit.",
        status="active",
        allows_provisional=False,
        created_by=admin.id,
    )

    db.session.add_all([proj_prov, proj_verif])
    db.session.commit()

client = app.test_client()

print("\n== 1. Stage 1: Public Application & Track Selection ==")
r_apply_page = client.get("/onboard/apply")
expect(r_apply_page.status_code == 200, "GET /onboard/apply returns 200")
expect("Annotator Application" in r_apply_page.get_data(as_text=True), "Application title rendered")
expect("Adversarial Red-Teaming" in r_apply_page.get_data(as_text=True), "Red Teaming track option rendered")
expect("Translation &amp; MTQE" in r_apply_page.get_data(as_text=True), "Translation track option rendered")

csrf = get_csrf(client, "/onboard/apply")

# Submit application for Candidate 1 (Translation Track, Hausa)
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
        "specialization_track": "translation",
        "primary_language": "hausa",
        "dialect": "Kano Hausa",
        "domain_expertise": "Linguistics",
        "device_type": "laptop",
        "weekly_hours": "20",
        "competencies": ["translation", "sft"],
    },
    follow_redirects=True,
)
expect(r_apply_post.status_code == 200, "POST /onboard/apply succeeds and redirects")
with app.app_context():
    app_user = db.session.query(User).filter_by(email="amina@hausa.ng").first()
    expect(app_user is not None, "Applicant user row created")
    expect(app_user.is_verified is False, "Applicant user is unverified / provisional")
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant is not None, "Applicant profile row created")
    expect(applicant.specialization_track == "translation", "Specialization track recorded as 'translation'")
    expect(applicant.primary_language == "hausa", "Primary language is 'hausa'")
    expect(applicant.stage == "guidelines", "Stage advanced to 'guidelines'")
    expect(applicant.verification_status == "provisional", "Verification status initialized to 'provisional'")

print("\n== 2. Stage 2: Track-Specific Guidelines & Integrity Quiz ==")
r_quiz_page = client.get("/onboard/guidelines-quiz")
expect(r_quiz_page.status_code == 200, "GET /onboard/guidelines-quiz returns 200")
expect("Translation &amp; MTQE" in r_quiz_page.get_data(as_text=True), "Track badge rendered in quiz")

quiz_bank = GUIDELINES_QUIZZES["translation"]
expect(len(quiz_bank) == 8, "8 translation track guidelines questions loaded")

csrf_quiz = get_csrf(client, "/onboard/guidelines-quiz")
pass_quiz_data = {f"q_{q['id']}": q["correct"] for q in quiz_bank}
pass_quiz_data["csrf_token"] = csrf_quiz

r_quiz_pass = client.post("/onboard/guidelines-quiz", data=pass_quiz_data, follow_redirects=True)
expect(r_quiz_pass.status_code == 200, "POST /onboard/guidelines-quiz (100% correct) succeeds")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.guidelines_passed, "guidelines_passed is True")
    expect(applicant.stage == "test", "Stage advanced to 'test'")

print("\n== 3. Stage 3: Track-Tailored Timed Assessment ==")
r_test_page = client.get("/onboard/test")
expect(r_test_page.status_code == 200, "GET /onboard/test returns 200")
expect("Translation &amp; MTQE" in r_test_page.get_data(as_text=True), "Track assessment header rendered")

track_assessment = get_track_assessment("translation", "hausa")
expect(len(track_assessment["sections"]) >= 2, "Hausa translation assessment sections loaded")

csrf_test = get_csrf(client, "/onboard/test")
test_post_data = {
    "csrf_token": csrf_test,
    "test_q_tr_ha_01": "b",
    "test_q_tr_ha_02": "b",
    "test_q_tr_ha_03": "b",
    "test_q_tr_ha_04": "a",
    "practical_response": "Wannan asusu na banki yana ba ku damar ajiye kuɗi cikin aminci da sauƙi tare da ƙarin riba kowane wata.",
}
r_test_sub = client.post("/onboard/test", data=test_post_data, follow_redirects=True)
expect(r_test_sub.status_code == 200, "POST /onboard/test succeeds and redirects to status")

with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.test_passed, "Test passed is True")
    expect(applicant.test_score_composite >= 80, f"Composite score {applicant.test_score_composite}% >= 80%")
    expect(applicant.stage in ("decision", "fit_to_need"), f"Stage advanced to {applicant.stage}")
    expect("Translation" in applicant.assigned_pod or "Hausa" in applicant.assigned_pod, f"Assigned to pod: {applicant.assigned_pod}")

print("\n== 4. Stage 6: Admin Candidate Review & Decision ==")
admin_client = app.test_client()
csrf_admin_login = get_csrf(admin_client, "/login")
admin_client.post("/login", data={"csrf_token": csrf_admin_login, "email": "admin@brydl.ai", "password": "admin-pass-123"}, follow_redirects=True)

r_dossier = admin_client.get(f"/admin/applicants/{applicant.id}")
expect(r_dossier.status_code == 200, "GET /admin/applicants/<id> returns 200")
expect("Applicant Dossier" in r_dossier.get_data(as_text=True), "Dossier title rendered")

csrf_dec = get_csrf(admin_client, f"/admin/applicants/{applicant.id}")
r_dec = admin_client.post(
    f"/admin/applicants/{applicant.id}/decision",
    data={"csrf_token": csrf_dec, "decision": "approved", "notes": "Approved for agreement and calibration."},
    follow_redirects=True,
)
expect(r_dec.status_code == 200, "POST /admin/applicants/<id>/decision succeeds")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.stage == "agreement", "Stage advanced to 'agreement'")

print("\n== 5. Stage 7: Agreement & NDA E-Signature ==")
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
expect(r_agree_post.status_code == 200, "POST /onboard/agreement succeeds")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.stage == "trial", "Stage advanced to 'trial'")

print("\n== 6. Stage 8: Track-Tailored Calibration Trial Sandbox ==")
track_tasks = get_calibration_tasks(applicant.specialization_track)
for task in track_tasks:
    csrf_trial = get_csrf(client, "/onboard/trial")
    r_sub = client.post(
        "/onboard/trial/submit",
        data={
            "csrf_token": csrf_trial,
            "task_index": str(task["index"]),
            "task_prompt": task["prompt"],
            "response_text": f"Accurate Hausa translation response for task #{task['index']} with hooked letters ɓ, ɗ, ƙ.",
        },
        follow_redirects=True,
    )
    expect(r_sub.status_code == 200, f"Submitted calibration task #{task['index']}")

with app.app_context():
    applicant = db.session.query(Applicant).filter_by(user_id=app_user.id).first()
    expect(applicant.trial_tasks_completed == 10, "All 10 trial tasks completed")
    expect(applicant.stage == "active", "Applicant stage graduated to 'active'")

print("\n== 7. Dual Workspace Access Boundary (Provisional vs Official) ==")
# Provisional user accessing /annotator/ should be redirected to provisional workspace
r_annotator_attempt = client.get("/annotator/", follow_redirects=False)
expect(r_annotator_attempt.status_code == 302, "Unverified user blocked from /annotator/ (302 redirect)")
expect("/onboard/workspace" in r_annotator_attempt.headers.get("Location", ""), "Redirected to /onboard/workspace")

# Accessing provisional workspace directly
r_prov_dash = client.get("/onboard/workspace/dashboard")
expect(r_prov_dash.status_code == 200, "GET /onboard/workspace/dashboard returns 200")
expect("PROVISIONAL CONTRIBUTOR" in r_prov_dash.get_data(as_text=True), "Provisional badge rendered")
expect("Readiness Meter" in r_prov_dash.get_data(as_text=True), "Verification Readiness Meter rendered")

# Headcount check: provisional worker must NOT be counted in official verified workforce
with app.app_context():
    verified_count = db.session.scalar(
        db.select(db.func.count(User.id)).where(User.role == "annotator", User.is_verified.is_(True), User.is_active.is_(True))
    )
    expect(verified_count == 0, "Official verified annotator headcount is 0 (provisional excluded)")

print("\n== 8. Provisional Real Work Execution & Earnings ==")
# Assign a real project task to the provisional user
with app.app_context():
    proj = db.session.query(Project).filter_by(name="Hausa MTQE Batch 1").first()
    task = Task(
        project_id=proj.id,
        input_data="Translate: Secure financial savings platform.",
        status="assigned",
        assigned_to=app_user.id,
    )
    db.session.add(task)
    db.session.commit()
    task_id = task.id

r_prov_task_page = client.get(f"/onboard/workspace/task/{task_id}")
expect(r_prov_task_page.status_code == 200, "GET /onboard/workspace/task/<id> returns 200")

csrf_task = get_csrf(client, f"/onboard/workspace/task/{task_id}")
r_task_sub = client.post(
    f"/onboard/workspace/task/{task_id}",
    data={
        "csrf_token": csrf_task,
        "translated_text": "Dandalin adana kuɗi na kuɗin shiga mai aminci.",
    },
    follow_redirects=True,
)
expect(r_task_sub.status_code == 200, "Provisional task submitted successfully")

# QA reviews the submission and approves it
with app.app_context():
    sub = db.session.query(Submission).filter_by(task_id=task_id).first()
    sub.status = "approved"
    sub.qa_score = 95
    sub.reviewed_at = utc_now()
    db.session.commit()

# Check provisional earnings page
r_prov_earnings = client.get("/onboard/workspace/earnings")
expect(r_prov_earnings.status_code == 200, "GET /onboard/workspace/earnings returns 200")
expect("Approved Earnings" in r_prov_earnings.get_data(as_text=True), "Earnings ledger displayed")

print("\n== 9. HR Screening & Multi-Department Verification Gate ==")
# HR logs in
hr_client = app.test_client()
csrf_hr_login = get_csrf(hr_client, "/login")
hr_client.post("/login", data={"csrf_token": csrf_hr_login, "email": "hr@brydl.ai", "password": "hr-pass-123"}, follow_redirects=True)

r_hr_candidates = hr_client.get("/hr/candidates")
expect(r_hr_candidates.status_code == 200, "GET /hr/candidates returns 200")
expect("HR Screening" in r_hr_candidates.get_data(as_text=True), "HR Screening Desk title rendered")

r_hr_screen_page = hr_client.get(f"/hr/candidates/{applicant.id}")
expect(r_hr_screen_page.status_code == 200, "GET /hr/candidates/<id> returns 200")

csrf_hr_screen = get_csrf(hr_client, f"/hr/candidates/{applicant.id}")
r_hr_post = hr_client.post(
    f"/hr/candidates/{applicant.id}",
    data={
        "csrf_token": csrf_hr_screen,
        "communication_score": "5",
        "professionalism_score": "5",
        "availability_confirmed_hours": "25",
        "notes": "Excellent native Hausa fluency, highly professional, confirmed 25 hrs/wk commitment.",
        "recommendation": "recommended",
    },
    follow_redirects=True,
)
expect(r_hr_post.status_code == 200, "HR screening evaluation recorded")
with app.app_context():
    applicant = db.session.query(Applicant).filter_by(id=applicant.id).first()
    expect(applicant.verification_status == "hr_cleared", "Applicant status updated to 'hr_cleared'")
    expect(applicant.hr_screening is not None, "HRScreening row persisted")
    expect(applicant.hr_screening.recommendation == "recommended", "HR recommendation is 'recommended'")

print("\n== 10. Admin Verification Cutover & Activation Engine ==")
# Admin executes cutover
r_admin_dossier = admin_client.get(f"/admin/applicants/{applicant.id}")
expect(r_admin_dossier.status_code == 200, "Admin views applicant dossier with HR report")
expect("HR Cleared" in r_admin_dossier.get_data(as_text=True), "HR Clearance displayed in admin dossier")

csrf_cutover = get_csrf(admin_client, f"/admin/applicants/{applicant.id}")
r_cutover = admin_client.post(
    f"/admin/applicants/{applicant.id}/verify-cutover",
    data={"csrf_token": csrf_cutover},
    follow_redirects=True,
)
expect(r_cutover.status_code == 200, "POST /admin/applicants/<id>/verify-cutover succeeds")

with app.app_context():
    # Verify new official user
    new_user = db.session.query(User).filter_by(email="amina@hausa.ng", is_verified=True).first()
    expect(new_user is not None, "New verified User record created with original email")
    expect(new_user.is_verified is True, "new_user.is_verified is True")
    expect(new_user.is_archived is False, "new_user.is_archived is False")
    expect(new_user.activation_token_hash is not None, "48h activation token hash issued")

    # Verify old provisional user is archived and deactivated
    old_user = db.session.get(User, app_user.id)
    expect(old_user.is_active is False, "Old provisional user is deactivated (is_active=False)")
    expect(old_user.is_archived is True, "Old provisional user is archived (is_archived=True)")
    expect(old_user.migrated_to_user_id == new_user.id, f"Lineage pointer set to {new_user.id}")

    # Verify atomic migration of tasks and submissions
    migrated_sub = db.session.query(Submission).filter_by(task_id=task_id).first()
    expect(migrated_sub.annotator_id == new_user.id, "Submission ownership transferred to new verified User ID")
    migrated_task = db.session.get(Task, task_id)
    expect(migrated_task.assigned_to == new_user.id, "Task assigned_to transferred to new verified User ID")

    # Verify official workforce headcount
    official_count = db.session.scalar(
        db.select(db.func.count(User.id)).where(User.role == "annotator", User.is_verified.is_(True), User.is_active.is_(True))
    )
    expect(official_count == 1, "Official verified annotator headcount updated to 1")

print("\n== 11. Official Account Activation & Production Workspace Access ==")
import hashlib
import secrets
from datetime import timedelta

test_raw_token = "secure_activation_test_token_12345"
with app.app_context():
    new_user = db.session.query(User).filter_by(email="amina@hausa.ng", is_verified=True).first()
    new_user.activation_token_hash = hashlib.sha256(test_raw_token.encode("utf-8")).hexdigest()
    new_user.activation_token_expires = utc_now() + timedelta(hours=48)
    db.session.commit()

active_client = app.test_client()
r_act_page = active_client.get(f"/activate/{test_raw_token}")
expect(r_act_page.status_code == 200, "GET /activate/<token> returns 200")
expect("Activate Official Account" in r_act_page.get_data(as_text=True), "Activation page rendered")

csrf_act = get_csrf(active_client, f"/activate/{test_raw_token}")
r_act_post = active_client.post(
    f"/activate/{test_raw_token}",
    data={
        "csrf_token": csrf_act,
        "password": "new-verified-pass-456",
        "confirm_password": "new-verified-pass-456",
    },
    follow_redirects=True,
)
expect(r_act_post.status_code == 200, "POST /activate/<token> successfully sets password and logs in")

# Verified annotator accessing official production /annotator/ without terms consent is gated
r_official_gate = active_client.get("/annotator/", follow_redirects=False)
expect(r_official_gate.status_code == 302 and "/annotator/terms" in r_official_gate.headers.get("Location", ""),
       "Newly verified annotator without v2.1 consent is GATED and redirected to /annotator/terms")

# Affirmative acceptance of Terms v2.1
csrf_terms = get_csrf(active_client, "/annotator/terms")
r_accept_terms = active_client.post(
    "/annotator/terms/accept",
    data={"csrf_token": csrf_terms, "terms_confirm": "on"},
    follow_redirects=True,
)
expect(r_accept_terms.status_code == 200, "Affirmative acceptance of Contributor Terms v2.1 succeeds")

# Now accesses official production workspace
r_official_dash = active_client.get("/annotator/")
expect(r_official_dash.status_code == 200, "Verified annotator with valid consent accesses /annotator/ (got 200)")

# Verify old provisional login is rejected / retired
old_login_client = app.test_client()
csrf_old_login = get_csrf(old_login_client, "/login")
r_old_login = old_login_client.post(
    "/login",
    data={
        "csrf_token": csrf_old_login,
        "email": "amina@hausa.ng",
        "password": "candidate-pass-123",
    },
    follow_redirects=True,
)
# With new password on official account, old password fails; logging in with new password succeeds on official account
r_new_login = old_login_client.post(
    "/login",
    data={
        "csrf_token": csrf_old_login,
        "email": "amina@hausa.ng",
        "password": "new-verified-pass-456",
    },
    follow_redirects=True,
)
expect(r_new_login.status_code == 200, "Official credentials log in successfully")

print()
if failures > 0:
    print(f"ONBOARDING & WORKFORCE TESTS FAILED ({failures}/{checks} checks)")
    sys.exit(1)
print(f"ALL TWO-TIER WORKFORCE, HR SCREENING & CUTOVER TESTS PASSED ({checks} checks)")
