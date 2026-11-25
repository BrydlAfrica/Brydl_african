"""Tests for Phase 8: Career Progression & Specialist Pathways Framework.

Validates:
1. Seniority tiers, specialist badge tracking, language pod squads, and pod leads.
2. Annotator Career & Specialist Hub (/annotator/portfolio) performance metrics,
   earned badge computation, and career promotion application submission.
3. Preventing duplicate active promotion requests.
4. Admin Workforce & Promotions Console (/admin/workforce/promotions) metrics,
   promotion dossier approval & rejection workflows, and direct seniority updates.
5. Role-based access control and security invariants.
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
    CareerPromotionRequest,
    Payment,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)

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


app = create_app()

# Setup database
with app.app_context():
    db.drop_all()
    db.create_all()

    # 1. Models & Defaults
    print("== 1. Data Models & Seniority Tier Defaults ==")
    u_default = User(
        name="Default Seniority",
        email="default@brydl.ng",
        role="annotator",
        is_active=True,
        terms_accepted_at=utc_now(),
    )
    u_default.set_password("Password123!")
    db.session.add(u_default)
    db.session.commit()
    u_default.record_consent()
    db.session.commit()

    expect(u_default.seniority_level == "verified", "Default User seniority_level is 'verified'")
    expect(u_default.specialist_badge is None, "Default User specialist_badge is None")
    expect(u_default.assigned_pod is None, "Default User assigned_pod is None")
    expect(u_default.is_pod_lead is False, "Default User is_pod_lead is False")

    # CareerPromotionRequest model
    promo_req = CareerPromotionRequest(
        user_id=u_default.id,
        target_role="specialist",
        specialization_area="african_languages",
        statement_of_interest="I have 5 years experience in Yoruba and Igbo phonology and NLP.",
        status="pending",
    )
    db.session.add(promo_req)
    db.session.commit()

    loaded_req = db.session.get(CareerPromotionRequest, promo_req.id)
    expect(loaded_req is not None, "CareerPromotionRequest persisted successfully")
    expect(loaded_req.target_role == "specialist", "target_role matches 'specialist'")
    expect(loaded_req.status == "pending", "status defaults to 'pending'")
    expect(loaded_req.user.email == "default@brydl.ng", "user relationship resolves correctly")

    # Seed users for access control & workforce tests
    admin_user = User(
        name="Admin Boss",
        email="admin@brydl.ng",
        role="admin",
        is_active=True,
        terms_accepted_at=utc_now(),
    )
    admin_user.set_password("Password123!")

    prov_user = User(
        name="Provisional Contributor",
        email="provisional@brydl.ng",
        role="annotator",
        is_active=True,
        is_verified=False,
        seniority_level="provisional",
        terms_accepted_at=utc_now(),
    )
    prov_user.set_password("Password123!")

    chioma = User(
        name="Chioma Adeyemi",
        email="chioma@brydl.ng",
        role="annotator",
        is_active=True,
        is_verified=True,
        seniority_level="specialist",
        specialist_badge="Yoruba NLP Specialist",
        assigned_pod="Yoruba Squad",
        is_pod_lead=True,
        terms_accepted_at=utc_now(),
    )
    chioma.set_password("Password123!")

    ibrahim = User(
        name="Ibrahim Danladi",
        email="ibrahim@brydl.ng",
        role="annotator",
        is_active=True,
        is_verified=True,
        seniority_level="verified",
        terms_accepted_at=utc_now(),
    )
    ibrahim.set_password("Password123!")

    amara = User(
        name="Amara Kalu",
        email="amara@brydl.ng",
        role="annotator",
        is_active=True,
        is_verified=True,
        seniority_level="verified",
        terms_accepted_at=utc_now(),
    )
    amara.set_password("Password123!")

    tunde = User(
        name="Tunde Balogun",
        email="tunde@brydl.ng",
        role="annotator",
        is_active=True,
        is_verified=True,
        seniority_level="verified",
        terms_accepted_at=utc_now(),
    )
    tunde.set_password("Password123!")

    db.session.add_all([admin_user, prov_user, chioma, ibrahim, amara, tunde])
    db.session.commit()
    for u in [prov_user, chioma, ibrahim, amara, tunde]:
        u.record_consent()
    db.session.commit()

    admin_id = admin_user.id
    prov_id = prov_user.id
    chioma_id = chioma.id
    ibrahim_id = ibrahim.id
    amara_id = amara.id
    tunde_id = tunde.id

    # Seed tasks, submissions and payments for Chioma
    proj = Project(
        name="Dialect Audio Validation",
        client_name="Test Enterprise",
        task_type="text_classification",
        instructions="Guidelines",
        status="active",
        created_by=admin_id,
    )
    db.session.add(proj)
    db.session.flush()

    task = Task(
        project_id=proj.id,
        input_data=json.dumps({"text": "E kaaro o"}),
        status="approved",
        assigned_to=chioma_id,
    )
    db.session.add(task)
    db.session.flush()

    sub = Submission(
        task_id=task.id,
        annotator_id=chioma_id,
        output_data=json.dumps({"label": "positive"}),
        status="approved",
        qa_score=95,
        reviewed_at=utc_now(),
    )
    db.session.add(sub)

    today = utc_now().date()
    pmt = Payment(
        annotator_id=chioma_id,
        amount=Decimal("15000.00"),
        currency="NGN",
        period_start=today,
        period_end=today,
        payment_method="bank_transfer",
        status="paid",
        paid_at=utc_now(),
    )
    db.session.add(pmt)
    db.session.commit()

# 2. Access Control on /annotator/portfolio
print("== 2. Access Control on /annotator/portfolio ==")
anon_cl = app.test_client()
r_anon = anon_cl.get("/annotator/portfolio")
expect(r_anon.status_code == 302 and "/login" in r_anon.headers.get("Location", ""),
       "Anonymous visitor is redirected to /login (302)")

admin_cl = app.test_client()
login(admin_cl, "admin@brydl.ng")
r_admin = admin_cl.get("/annotator/portfolio")
expect(r_admin.status_code == 403, "Admin user gets 403 Forbidden on /annotator/portfolio")

prov_cl = app.test_client()
login(prov_cl, "provisional@brydl.ng")
r_prov = prov_cl.get("/annotator/portfolio")
expect(r_prov.status_code == 302 and "/onboard/workspace/dashboard" in r_prov.headers.get("Location", ""),
       "Provisional unverified annotator is quarantined to provisional workspace (/onboard/workspace/dashboard)")

# 3. Annotator Portfolio Hub Render & Metrics
print("== 3. Annotator Portfolio Hub Render & Specialist Badges ==")
chioma_cl = app.test_client()
login(chioma_cl, "chioma@brydl.ng")
r_port = chioma_cl.get("/annotator/portfolio")
expect(r_port.status_code == 200, "Verified annotator gets 200 OK on /annotator/portfolio")
html_port = r_port.get_data(as_text=True)

expect("Chioma Adeyemi" in html_port, "Annotator name rendered on portfolio")
expect("Yoruba NLP Specialist" in html_port, "Specialist badge title rendered")
expect("Yoruba Squad" in html_port, "Assigned language squad/pod rendered")
expect("Pod Lead" in html_port, "Pod Lead status badge rendered")
expect("95" in html_port, "Average QA score rendered")
expect("15,000.00" in html_port, "Lifetime milestone earnings rendered")
expect("Verified Contributor" in html_port, "Verified Contributor badge rendered")

# 4. Promotion Application Submission & Duplicate Protection
print("== 4. Career Promotion Submission & Invariants ==")
ibrahim_cl = app.test_client()
login(ibrahim_cl, "ibrahim@brydl.ng")

c_ib = gcsrf(ibrahim_cl, "/annotator/portfolio")
r_sub = ibrahim_cl.post(
    "/annotator/portfolio",
    data={
        "csrf_token": c_ib,
        "target_role": "qa_reviewer",
        "specialization_area": "african_languages",
        "statement_of_interest": "I have completed over 150 tasks with 98% accuracy and am ready to review peer submissions.",
    },
    follow_redirects=True,
)
expect(r_sub.status_code == 200, "Promotion application POST returns 200 after redirect")
html_sub = r_sub.get_data(as_text=True)
expect("Your career promotion application has been submitted" in html_sub,
       "Success flash message displayed after submission")
expect("Application Under Review" in html_sub, "Status banner shows 'Application Under Review'")

# Duplicate submission rejection
c_ib2 = gcsrf(ibrahim_cl, "/annotator/portfolio")
r_dup = ibrahim_cl.post(
    "/annotator/portfolio",
    data={
        "csrf_token": c_ib2,
        "target_role": "pod_lead",
        "specialization_area": "african_languages",
        "statement_of_interest": "I would also like to apply to be a pod lead for Hausa speakers.",
    },
    follow_redirects=True,
)
html_dup = r_dup.get_data(as_text=True)
expect("already have an active promotion application" in html_dup,
       "Duplicate promotion submission blocked while previous request is pending")

# 5. Admin Workforce & Promotions Console
print("== 5. Admin Workforce & Promotions Console ==")
r_admin_console = admin_cl.get("/admin/workforce/promotions")
expect(r_admin_console.status_code == 200, "Admin gets 200 OK on /admin/workforce/promotions")
html_console = r_admin_console.get_data(as_text=True)

expect("Workforce Career &amp; Promotions Console" in html_console,
       "Workforce console header rendered")
expect("Ibrahim Danladi" in html_console, "Pending applicant Ibrahim listed in review queue")
expect("Qa Reviewer" in html_console or "QA Reviewer" in html_console, "Target role Qa Reviewer shown")
expect("African Languages" in html_console, "Specialization track shown")

# 6. Admin Approval of Promotion Request
print("== 6. Admin Promotion Approval Workflow ==")
with app.app_context():
    ibrahim_req = CareerPromotionRequest.query.filter_by(user_id=ibrahim_id, status="pending").first()
    expect(ibrahim_req is not None, "Pending promotion request found in database")
    ibrahim_req_id = ibrahim_req.id

c_admin = gcsrf(admin_cl, "/admin/workforce/promotions")
r_approve = admin_cl.post(
    f"/admin/workforce/promotions/{ibrahim_req_id}/review",
    data={
        "csrf_token": c_admin,
        "action": "approve",
        "specialist_badge": "Hausa QA Specialist",
        "assigned_pod": "Hausa Squad",
        "is_pod_lead": "1",
        "notes": "Verified high accuracy on Hausa evaluation tasks. Promoted to Senior QA Reviewer and Pod Lead.",
    },
    follow_redirects=True,
)
expect(r_approve.status_code == 200, "Approval POST returns 200 after redirect")
html_app = r_approve.get_data(as_text=True)
expect("Promotion approved for Ibrahim Danladi" in html_app, "Approval success banner rendered")

with app.app_context():
    loaded_ib = db.session.get(User, ibrahim_id)
    expect(loaded_ib.seniority_level == "qa_reviewer", "Ibrahim seniority_level updated to 'qa_reviewer'")
    expect(loaded_ib.specialist_badge == "Hausa QA Specialist", "Ibrahim specialist_badge set")
    expect(loaded_ib.assigned_pod == "Hausa Squad", "Ibrahim assigned_pod set")
    expect(loaded_ib.is_pod_lead is True, "Ibrahim is_pod_lead set to True")

# 7. Admin Rejection of Promotion Request
print("== 7. Admin Promotion Rejection Workflow ==")
with app.app_context():
    amara_req = CareerPromotionRequest(
        user_id=amara_id,
        target_role="consensus_arbitrator",
        specialization_area="legal_regulatory",
        statement_of_interest="Experience in regulatory arbitration.",
        status="pending",
    )
    db.session.add(amara_req)
    db.session.commit()
    amara_req_id = amara_req.id

c_admin2 = gcsrf(admin_cl, "/admin/workforce/promotions")
r_reject = admin_cl.post(
    f"/admin/workforce/promotions/{amara_req_id}/review",
    data={
        "csrf_token": c_admin2,
        "action": "reject",
        "notes": "Requires additional task volume and arbitration calibration before qualifying.",
    },
    follow_redirects=True,
)
expect(r_reject.status_code == 200, "Rejection POST returns 200 after redirect")
html_rej = r_reject.get_data(as_text=True)
expect("Promotion application for Amara Kalu has been declined" in html_rej,
       "Rejection notice displayed")

with app.app_context():
    loaded_amara_req = db.session.get(CareerPromotionRequest, amara_req_id)
    expect(loaded_amara_req.status == "rejected", "Promotion request status is 'rejected'")
    expect(loaded_amara_req.reviewed_by_id == admin_id, "reviewed_by_id recorded")
    loaded_amara = db.session.get(User, amara_id)
    expect(loaded_amara.seniority_level == "verified", "Amara seniority_level remains 'verified'")

# 8. Direct Seniority Level Update
print("== 8. Admin Direct Seniority Update ==")
c_admin3 = gcsrf(admin_cl, "/admin/workforce/promotions")
r_direct = admin_cl.post(
    f"/admin/workforce/annotators/{tunde_id}/update-seniority",
    data={
        "csrf_token": c_admin3,
        "seniority_level": "consensus_arbitrator",
        "specialist_badge": "Chief Yoruba Arbitrator",
        "assigned_pod": "Yoruba Pod",
        "is_pod_lead": "1",
    },
    follow_redirects=True,
)
expect(r_direct.status_code == 200, "Direct update POST returns 200 after redirect")
html_dir = r_direct.get_data(as_text=True)
expect("Workforce credentials updated for Tunde Balogun" in html_dir,
       "Credentials updated flash message displayed")

with app.app_context():
    loaded_tunde = db.session.get(User, tunde_id)
    expect(loaded_tunde.seniority_level == "consensus_arbitrator", "Seniority level updated to 'consensus_arbitrator'")
    expect(loaded_tunde.specialist_badge == "Chief Yoruba Arbitrator", "Specialist badge updated")
    expect(loaded_tunde.assigned_pod == "Yoruba Pod", "Assigned pod updated")
    expect(loaded_tunde.is_pod_lead is True, "Pod Lead flag set to True")

print("\n" + "=" * 50)
if FAILURES:
    print(f"FAILED with {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CAREER PROGRESSION TESTS PASSED CLEANLY")
    sys.exit(0)
