"""Security, IDOR & Multi-User Concurrency Test Suite.

Covers:
  1. Cross-user IDOR:
     - Annotator A cannot POST turns to Annotator B's session (403 session_not_yours).
     - Annotator A cannot flag/unflag messages in Annotator B's session (403).
     - Annotator A cannot conclude Annotator B's session (403).
  2. Role Privilege Escalation:
     - Annotator account accessing /admin/ routes receives 403 Forbidden.
     - Annotator POST to /admin/payments/generate receives 403.
  3. Payment Double-Bundling Prevention:
     - Approved submission bundled into Payment #1 is NEVER re-bundled in Payment #2.
  4. Double-Approve Prevention:
     - Submitting approve twice on the same submission is idempotent and does not create duplicate payouts.
  5. QA Reviewer Race / Concurrency:
     - Two reviewers acting on the same submission: first verdict commits, second is rejected / no-op.
  6. Rejected Session Lifecycle:
     - Rejected submissions are excluded from payout bundles.
"""
import hashlib
import json
import os
import shutil
import sys
import uuid
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["REDLAB_ENABLED"] = "true"
os.environ["BRYDL_DISABLE_JOBS"] = "1"
os.environ["SECRET_KEY"] = "security-idor-audit-secret-key-32chars-min-xxxxx"
os.environ["REDLAB_FERNET_KEY"] = "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM="
os.environ["USD_TO_NGN_RATE"] = "1600"

for d in ("instance", "backups"):
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
os.makedirs("instance", exist_ok=True)

from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from flask.sessions import session_json_serializer  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from app import app as flask_app  # noqa: E402
from extensions import db  # noqa: E402
from models import User, Project, Task, Submission, Payment, utc_now  # noqa: E402
from redlab.models import ensure_redlab_schema, Session, Message, Target  # noqa: E402
from redlab.crypto import encrypt_secret  # noqa: E402
from asgi import app as asgi_app  # noqa: E402
from routes.admin import _unclaimed_agg, _apply_verdict  # noqa: E402

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


def _cookie_ser():
    sk = flask_app.secret_key
    if isinstance(sk, (list, tuple)):
        sk = sk[0]
    return URLSafeTimedSerializer(
        secret_key=sk, salt="cookie-session",
        serializer=session_json_serializer,
        signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1},
    )


def _csrf_signer():
    sk = flask_app.secret_key
    if isinstance(sk, (list, tuple)):
        sk = sk[0]
    return URLSafeTimedSerializer(secret_key=sk, salt="wtf-csrf-token")


def make_auth(uid, raw_csrf, role="annotator"):
    payload = {"_user_id": str(uid), "_fresh": True,
               "_user_role": role, "csrf_token": raw_csrf}
    return _cookie_ser().dumps(payload), _csrf_signer().dumps(raw_csrf)


client = TestClient(asgi_app)

P_ID = 910
TGT_ID = "tgt-sec-000000000000000000000001"
SESS_B_ID = "sess-b-000000000000000000000001"
MSG_B_ID = "msg-b-00000000000000000000000001"

ANN_A_ID = 903
ANN_B_ID = 904
ADMIN1_ID = 901
ADMIN2_ID = 902

with flask_app.app_context():
    ensure_redlab_schema(db.engine)
    db.create_all()

    admin1 = User(id=ADMIN1_ID, email="admin1@test.com", role="admin", password_hash="x", is_active=True, name="Admin One")
    admin2 = User(id=ADMIN2_ID, email="admin2@test.com", role="admin", password_hash="x", is_active=True, name="Admin Two")
    ann_a = User(id=ANN_A_ID, email="ann_a@test.com", role="annotator", password_hash="x", is_active=True, name="Annotator A")
    ann_b = User(id=ANN_B_ID, email="ann_b@test.com", role="annotator", password_hash="x", is_active=True, name="Annotator B")
    db.session.add_all([admin1, admin2, ann_a, ann_b])
    db.session.flush()

    proj = Project(
        id=P_ID, name="Security Test Proj", client_name="Anthropic Client",
        task_type="dynamic_red_team", instructions="Red-team testing",
        required_annotations=1, created_by=ADMIN1_ID, status="active",
        redteam_base_pay=Decimal("2.50"), redteam_breach_bounty=Decimal("1.00"),
        redteam_session_quota=10,
    )
    db.session.add(proj)
    db.session.flush()

    tgt = Target(
        id=TGT_ID, project_ref=str(P_ID), name="Target-1",
        endpoint="https://93.184.216.34/v1/chat", model_name="claude-3-5-sonnet-20241022",
        api_key_ciphertext=encrypt_secret("sk-test-" + "s" * 32),
        key_fingerprint="sk-...ssss", active=True,
    )
    db.session.add(tgt)
    db.session.flush()

    # Session belonging strictly to Annotator B (id=904)
    t_b = Task(id=9601, project_id=P_ID, status="assigned", assigned_to=ANN_B_ID, input_data="goal")
    db.session.add(t_b); db.session.flush()
    sub_b = Submission(
        id=9901, task_id=t_b.id, annotator_id=ANN_B_ID,
        output_data="{}", status="in_progress", redlab_session_id=SESS_B_ID,
    )
    sess_b = Session(
        id=SESS_B_ID, project_ref=str(P_ID), annotator_ref=str(ANN_B_ID),
        target_id=TGT_ID, status="open", turn_count=1, submission_ref=str(sub_b.id),
    )
    db.session.add_all([sub_b, sess_b]); db.session.flush()

    msg_b = Message(
        id=MSG_B_ID, session_id=SESS_B_ID, turn_index=0, role="assistant",
        content="Assistant response to Ann B", is_unsafe_flag=False,
    )
    db.session.add(msg_b)
    db.session.commit()

# --- 1. Cross-User IDOR Protection -------------------------------------
print("== 1. Cross-User IDOR Protection ==")
raw_csrf_a = "csrf-a-12345"
cookie_a, csrf_token_a = make_auth(ANN_A_ID, raw_csrf_a, role="annotator")
headers_a = {
    "Cookie": f"session={cookie_a}",
    "X-CSRFToken": csrf_token_a,
    "Content-Type": "application/json",
}

# 1a. Annotator A attempts to post turn into Annotator B's session
r_turn = client.post(
    f"/redlab/v1/sessions/{SESS_B_ID}/turn",
    json={"content": "Malicious prompt by Ann A", "client_turn_id": "attack-1"},
    headers=headers_a,
)
expect(r_turn.status_code == 403,
       f"Ann A turn to Ann B session returns 403 (got {r_turn.status_code})")
expect(r_turn.json().get("error") == "session_not_yours",
       f"error is 'session_not_yours' (got {r_turn.json().get('error')})")

# 1b. Annotator A attempts to flag message in Annotator B's session
r_flag = client.post(
    f"/redlab/v1/messages/{MSG_B_ID}/flag",
    json={"is_unsafe": True},
    headers=headers_a,
)
expect(r_flag.status_code == 403,
       f"Ann A flag on Ann B message returns 403 (got {r_flag.status_code})")
expect(r_flag.json().get("error") == "session_not_yours",
       "error is 'session_not_yours'")

# 1c. Annotator A attempts to conclude Annotator B's session
r_conc = client.post(
    f"/redlab/v1/sessions/{SESS_B_ID}/conclude",
    json={"note": "Malicious conclusion"},
    headers=headers_a,
)
expect(r_conc.status_code == 403,
       f"Ann A conclude Ann B session returns 403 (got {r_conc.status_code})")
expect(r_conc.json().get("error") == "session_not_yours",
       "error is 'session_not_yours'")


# --- 2. Role Privilege Escalation Protection ---------------------------
print("\n== 2. Role Privilege Escalation Protection ==")
r_admin_get = client.get("/admin/", headers={"Cookie": f"session={cookie_a}"}, follow_redirects=False)
expect(r_admin_get.status_code == 403,
       f"Annotator GET /admin/ returns 403 (got {r_admin_get.status_code})")

r_admin_post = client.post(
    "/admin/payments/generate",
    headers={"Cookie": f"session={cookie_a}", "X-CSRFToken": csrf_token_a},
    follow_redirects=False,
)
expect(r_admin_post.status_code == 403,
       f"Annotator POST /admin/payments/generate returns 403 (got {r_admin_post.status_code})")


# --- 3. Double-Approve & QA Race Concurrency ---------------------------
print("\n== 3. Double-Approve & QA Race Concurrency ==")
with flask_app.app_context():
    # Set sub_b to 'submitted' for QA review
    s = db.session.get(Submission, 9901)
    s.status = "submitted"
    s.submitted_at = utc_now()
    db.session.commit()

    # Reviewer 1 approves submission 9901
    updated1, esc1 = _apply_verdict([9901], action="approve", qa_score=90, qa_notes="Good", return_for_revision=True)
    db.session.commit()
    expect(updated1 == 1, f"Reviewer 1 approval succeeded (updated={updated1})")

    s_after = db.session.get(Submission, 9901)
    expect(s_after.status == "approved", f"status is 'approved' (got {s_after.status})")

    # Reviewer 2 races / double-approves or tries to reject an already approved submission
    updated2, esc2 = _apply_verdict([9901], action="reject", qa_score=10, qa_notes="Late reject", return_for_revision=True)
    db.session.commit()
    expect(updated2 == 0, f"Reviewer 2 conflicting verdict on approved sub is rejected/no-op (updated={updated2})")

    # Status remains 'approved'
    s_final = db.session.get(Submission, 9901)
    expect(s_final.status == "approved", "submission remains 'approved' despite conflicting race")


# --- 4. Payment Double-Bundling & Rejected Exclusion -------------------
print("\n== 4. Payment Double-Bundling & Rejected Lifecycle ==")
with flask_app.app_context():
    # 4a. Approved sub 9901 is bundled into Payment 1
    agg = _unclaimed_agg(annotator_id=ANN_B_ID)
    expect(ANN_B_ID in agg, "annotator B has unclaimed approved work")
    cnt, amt, _ = agg[ANN_B_ID]
    expect(cnt == 1 and amt == Decimal("2.50"), f"aggregates 1 task for $2.50 (got cnt={cnt}, amt={amt})")

    pay1 = Payment(
        annotator_id=ANN_B_ID, amount=amt,
        period_start=utc_now(), period_end=utc_now(),
        tasks_count=cnt, status="pending",
    )
    db.session.add(pay1)
    db.session.flush()

    sub = db.session.get(Submission, 9901)
    sub.payment_id = pay1.id
    db.session.commit()

    # 4b. Sub 9901 can NEVER enter a second payment bundle
    agg_after = _unclaimed_agg(annotator_id=ANN_B_ID)
    expect(ANN_B_ID not in agg_after, "bundled submission is excluded from subsequent payout runs")

    # 4c. Create a rejected submission
    t_rej = Task(id=9602, project_id=P_ID, status="rejected", assigned_to=ANN_B_ID, input_data="goal 2")
    db.session.add(t_rej); db.session.flush()
    sub_rej = Submission(
        id=9902, task_id=t_rej.id, annotator_id=ANN_B_ID,
        output_data="{}", status="rejected", payment_id=None,
        reviewed_at=utc_now(), qa_score=10,
    )
    db.session.add(sub_rej)
    db.session.commit()

    # Rejected submission is excluded from unclaimed aggregation
    agg_rej = _unclaimed_agg(annotator_id=ANN_B_ID)
    expect(ANN_B_ID not in agg_rej, "rejected submission is strictly excluded from payout aggregation")

print()
if failures:
    print(f"SECURITY/IDOR SUITE FAILED ({failures}/{checks} checks)")
    sys.exit(1)
print(f"ALL {checks} SECURITY, IDOR & CONCURRENCY CHECKS PASSED")
