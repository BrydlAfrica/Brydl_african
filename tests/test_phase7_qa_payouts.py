"""Phase 7: QA review console + exact Decimal bounty ledger tests.

Covers:
  1. Exact-Decimal payout formula (zero float drift).
  2. QA breach-flag override flips flags, refreshes breach_count, and
     the Approve path syncs the count to Submission so ledger math
     uses QA-confirmed breaches.
  3. Rejection creates NO payout record.
  4. GET /admin/review/<id> renders transcript + QA flag buttons for
     dynamic_red_team submissions.
"""
import hashlib
import json
import os
import sys
import uuid
from decimal import Decimal

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, ".")

import shutil
for d in ("instance", "backups"):
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
os.makedirs("instance", exist_ok=True)
os.environ.setdefault("REDLAB_ENABLED", "true")
os.environ.setdefault("BRYDL_DISABLE_JOBS", "1")
os.environ.setdefault("SECRET_KEY",
                      "phase7-test-secret-key-32chars-min-xxxxxxxx")
os.environ.setdefault(
    "REDLAB_FERNET_KEY",
    "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM=")
os.environ.setdefault("USD_TO_NGN_RATE", "1600")

from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from extensions import db  # noqa: E402
from app import app as _flask  # noqa: E402,F401
from models import (  # noqa: E402
    Project, Task, Submission, User, Payment,
)
from redlab.auth import _flask_app, SESSION_COOKIE_NAME  # noqa: E402
from redlab.models import ensure_redlab_schema, Message, Session, Target  # noqa: E402
from redlab.crypto import encrypt_secret  # noqa: E402

flask_app = _flask_app()
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


# ---- Cookie/CSRF helpers (Phase 6-correct serializers) ----------------
def _cookie_ser():
    from flask.sessions import session_json_serializer
    sk = flask_app.secret_key
    if isinstance(sk, (list, tuple)):
        sk = sk[0]
    return URLSafeTimedSerializer(
        secret_key=sk, salt="cookie-session",
        serializer=session_json_serializer,
        signer_kwargs={"key_derivation": "hmac",
                       "digest_method": hashlib.sha1},
    )


def _csrf_signer():
    sk = flask_app.secret_key
    if isinstance(sk, (list, tuple)):
        sk = sk[0]
    return URLSafeTimedSerializer(secret_key=sk, salt="wtf-csrf-token")


def make_cookie(uid, raw_csrf, role="admin"):
    payload = {"_user_id": str(uid), "_fresh": True,
               "_user_role": role, "csrf_token": raw_csrf}
    return _cookie_ser().dumps(payload), _csrf_signer().dumps(raw_csrf)


# ---- Seed DB ----------------------------------------------------------
PROJ_ID = 710
TGT_ID = "tgt-p7-000000000000000000000001"
SIDS = {}
ASST_IDS = {}  # sub_id -> list of assistant message ids


def seed_sub(sub_id, task_id, n_turns, pre_flagged, status="submitted",
             conclude_note=None):
    t = Task(id=task_id, project_id=PROJ_ID, status="submitted",
             assigned_to=702, input_data=f"rt {sub_id}")
    db.session.add(t); db.session.flush()
    sid = uuid.uuid4().hex
    sub = Submission(
        id=sub_id, task_id=task_id, annotator_id=702,
        output_data=json.dumps({"kind": "redteam_session"}),
        status=status, redteam_state="qa_pending" if status == "submitted" else "none",
        redteam_breach_count=pre_flagged if status == "submitted" else 0,
    )
    sess = Session(
        id=sid, project_ref=str(PROJ_ID),
        annotator_ref="702", target_id=TGT_ID,
        status="concluded" if status == "submitted" else "open",
        turn_count=n_turns,
        breach_count=pre_flagged,
        conclude_note=conclude_note,
    )
    db.session.add_all([sub, sess]); db.session.flush()
    sub.redlab_session_id = sid
    asst_ids = []
    for i in range(n_turns):
        db.session.add(Message(
            id=uuid.uuid4().hex, session_id=sid,
            turn_index=i * 2, role="user",
            content=f"u{sub_id}-{i}",
            client_turn_id=f"p7-s{sub_id}-u{i}",
        ))
        mid = uuid.uuid4().hex
        asst_ids.append(mid)
        db.session.add(Message(
            id=mid, session_id=sid,
            turn_index=i * 2 + 1, role="assistant",
            content=f"a{sub_id}-{i}",
            is_unsafe_flag=(i < pre_flagged),
        ))
    SIDS[sub_id] = sid
    ASST_IDS[sub_id] = asst_ids
    return sub


with flask_app.app_context():
    ensure_redlab_schema(db.engine); db.create_all()
    qa = User(id=701, email="qa7@example.com", role="admin",
              password_hash="x", is_active=True, name="QA7")
    ann = User(id=702, email="ann7@example.com", role="annotator",
               password_hash="x", is_active=True, name="Ann7")
    db.session.add_all([qa, ann]); db.session.flush()
    proj = Project(
        id=PROJ_ID, name="RedTeam Pay P7", client_name="C7",
        task_type="dynamic_red_team", instructions="rt payout",
        required_annotations=1, created_by=qa.id, status="active",
        redteam_base_pay=Decimal("2.50"),
        redteam_breach_bounty=Decimal("1.10"),
        redteam_session_quota=0,
    )
    db.session.add(proj); db.session.flush()
    db.session.add(Target(
        id=TGT_ID, project_ref=str(PROJ_ID), name="T7",
        endpoint="https://93.184.216.34/v1/chat", model_name="m",
        api_key_ciphertext=encrypt_secret("sk-test-" + "c" * 32),
        key_fingerprint="sk-...cccc", active=True,
    ))
    db.session.flush()
    # sub 7901: 5 assistant turns, first 3 flagged -> $5.80
    seed_sub(7901, 7601, n_turns=5, pre_flagged=3)
    # sub 7902: 1 assistant turn, flagged -> $3.60 (to be rejected)
    seed_sub(7902, 7602, n_turns=1, pre_flagged=1)
    db.session.commit()
    print("seed ok")


CSRF_RAW = "phase7-csrf-raw-" + "a" * 20
COOKIE, CSRF = make_cookie(701, CSRF_RAW, role="admin")
c = flask_app.test_client()
c.set_cookie(SESSION_COOKIE_NAME, COOKIE)

# === 1. Exact-Decimal payout arithmetic ===============================
# Precondition: both subs still 'submitted' (not approved) so we can't
# use _unclaimed_agg directly (it filters status='approved'). Instead,
# exercise the CASE expression directly via a one-off SELECT to prove
# the math is exact-Decimal and the formula matches the spec.
print("\n== 1. Exact-Decimal payout formula ==")
with flask_app.app_context():
    from sqlalchemy import select, case, func
    per_sub = case(
        (Project.task_type == "dynamic_red_team",
         func.coalesce(Project.redteam_base_pay, 0)
         + func.coalesce(Submission.redteam_breach_count, 0)
         * func.coalesce(Project.redteam_breach_bounty, 0)),
        else_=func.coalesce(Project.pay_per_task, 0),
    )
    rows = db.session.execute(
        select(Submission.id, per_sub)
        .join(Submission.task).join(Task.project)
        .where(Submission.id.in_([7901, 7902]))
        .order_by(Submission.id.asc())
    ).all()
    totals = {row[0]: row[1] for row in rows}
    expect(totals[7901] == Decimal("5.80"),
           f"sub7901 base+3*bounty = $5.80 (got {totals[7901]!r})")
    expect(totals[7902] == Decimal("3.60"),
           f"sub7902 base+1*bounty = $3.60 (got {totals[7902]!r})")
    s = totals[7901] + totals[7902]
    expect(s == Decimal("9.40"),
           f"sum = $9.40 exact (got {s!r}, type {type(s).__name__})")
    # Zero float drift: Decimal arithmetic only, no binary artefacts.
    drift = (totals[7901] - Decimal("2.50")
             - Decimal("1.10") * Decimal("3"))
    expect(drift == Decimal("0.00"),
           f"zero float drift (drift={drift!r})")
    # Verify quantized-to-cents (money() normalization).
    from models import money
    expect(money(totals[7901]) == Decimal("5.80"),
           f"money() quantization $5.80 (got {money(totals[7901])!r})")

# === 2. QA breach-flag toggle endpoint + Approve syncs count =========
print("\n== 2. QA flag override + approve sync ==")
asst_sub1 = ASST_IDS[7901]  # first 3 flagged, last 2 not.
# Unflag first breach (asst_sub1[0]).
r = c.post(
    f"/admin/review/redteam/7901/messages/{asst_sub1[0]}/flag",
    data=json.dumps({"is_unsafe": False}),
    headers={"Content-Type": "application/json",
             "X-CSRFToken": CSRF},
)
expect(r.status_code == 200, f"unflag 200 (got {r.status_code})")
expect(r.get_json().get("ok") is True, "unflag ok")
# Flag a previously-unflagged message (asst_sub1[3] — index 3, 4th turn).
r = c.post(
    f"/admin/review/redteam/7901/messages/{asst_sub1[3]}/flag",
    data=json.dumps({"is_unsafe": True}),
    headers={"Content-Type": "application/json",
             "X-CSRFToken": CSRF},
)
expect(r.status_code == 200, f"flag 200 (got {r.status_code})")
with flask_app.app_context():
    cnt = (db.session.query(Message)
           .filter(Message.session_id == SIDS[7901],
                   Message.role == "assistant",
                   Message.is_unsafe_flag.is_(True)).count())
    expect(cnt == 3, f"still 3 flagged after net-zero toggles (got {cnt})")
    sub = db.session.get(Submission, 7901)
    expect(sub.redteam_breach_count == 3,
           "sub.redteam_breach_count updated after toggle "
           f"(got {sub.redteam_breach_count})")

# Now unflag two more so final QA-confirmed count = 1 -> payout $3.60.
for mid in asst_sub1[1:3]:
    r = c.post(
        f"/admin/review/redteam/7901/messages/{mid}/flag",
        data=json.dumps({"is_unsafe": False}),
        headers={"Content-Type": "application/json",
                 "X-CSRFToken": CSRF},
    )
    expect(r.status_code == 200, f"unflag 200 (got {r.status_code})")

with flask_app.app_context():
    cnt = (db.session.query(Message)
           .filter(Message.session_id == SIDS[7901],
                   Message.role == "assistant",
                   Message.is_unsafe_flag.is_(True)).count())
    expect(cnt == 1, f"QA-confirmed breach_count = 1 (got {cnt})")

# Approve sub1 — _apply_verdict's redteam sync should stamp
# sub.redteam_breach_count = 1 (the count of currently-flagged assistant
# rows), then _apply_verdict flips status to 'approved'.
# Fetch the review page first to obtain the signed CSRF token for the
# POST (flask-wtf signs csrf_token with the session-bound serializer;
# the raw token alone isn't accepted on POST).
r = c.get("/admin/review/7901")
expect(r.status_code == 200, f"GET review 7901 200 (got {r.status_code})")
import re as _re
m = _re.search(r'name="csrf_token"[^>]*value="([^"]+)"',
               r.get_data(as_text=True))
signed_csrf = m.group(1) if m else CSRF_RAW
r = c.post("/admin/review/7901", data={
    "action": "approve", "qa_score": 95, "qa_notes": "good",
    "return_for_revision": "y", "csrf_token": signed_csrf,
}, follow_redirects=False)
expect(r.status_code in (200, 302),
       f"approve sub1 200/302 (got {r.status_code})")
with flask_app.app_context():
    s1 = db.session.get(Submission, 7901)
    expect(s1.status == "approved", f"sub1 approved (got {s1.status})")
    expect(s1.redteam_breach_count == 1,
           f"sub1.redteam_breach_count synced to 1 on approve "
           f"(got {s1.redteam_breach_count})")

# Approving flips sub1 to 'approved', so _unclaimed_agg should now see
# it: amount = $2.50 + 1*$1.10 = $3.60, count=1. sub2 is still 'submitted'
# so excluded (must be approved to enter payout).
with flask_app.app_context():
    from routes.admin import _unclaimed_agg
    agg = _unclaimed_agg(annotator_id=702)
    expect(702 in agg, f"aggregator sees ann (keys={list(agg)})")
    cnt, total, _ = agg[702]
    expect(cnt == 1, f"one approved unclaimed sub (got {cnt})")
    expect(total == Decimal("3.60"),
           f"payout = $3.60 (got {total!r})")

# === 3. Rejection creates NO payout record ============================
print("\n== 3. Rejection creates no payout ==")
r = c.post("/admin/review/7902", data={
    "action": "reject", "qa_score": 20, "qa_notes": "no good",
    "return_for_revision": "y", "csrf_token": CSRF,
}, follow_redirects=False)
expect(r.status_code in (200, 302),
       f"reject sub2 200/302 (got {r.status_code})")
with flask_app.app_context():
    s2 = db.session.get(Submission, 7902)
    expect(s2.status == "rejected", f"sub2 rejected (got {s2.status})")

# Generate a payout run for ann702 — should only contain sub1 ($3.60).
r = c.post("/admin/payments/generate",
           data={"annotator_id": 702, "csrf_token": CSRF},
           follow_redirects=False)
expect(r.status_code in (200, 302),
       f"generate_payments 200/302 (got {r.status_code})")
with flask_app.app_context():
    pays = Payment.query.filter_by(annotator_id=702).all()
    expect(len(pays) == 1, f"one payment row (got {len(pays)})")
    if pays:
        p = pays[0]
        expect(p.amount == Decimal("3.60"),
               f"payment.amount = $3.60 exact (got {p.amount!r})")
        expect(p.tasks_count == 1,
               f"tasks_count = 1 (got {p.tasks_count})")
    # Rejected sub2 must NOT be claimed by any payment.
    s2 = db.session.get(Submission, 7902)
    expect(s2.payment_id is None,
           "rejected sub2 has no payment_id")

# === 4. Review page renders red-team transcript =======================
print("\n== 4. Review page renders red-team transcript ==")
with flask_app.app_context():
    seed_sub(7903, 7603, n_turns=1, pre_flagged=0,
             conclude_note="Bypassed safety filters on turn 1")
    db.session.commit()

r = c.get("/admin/review/7903")
expect(r.status_code == 200, f"GET review 200 (got {r.status_code})")
body = r.get_data(as_text=True)
expect("qa-redteam" in body, "red-team review container present")
expect("rt-qa-flag" in body, "per-turn QA flag button rendered")
expect("qa_redteam_flags.js" in body, "qa flag JS loaded")
expect("$2.50" in body, "base payout shown in preview")
expect("Bypassed safety filters on turn 1" in body,
       "annotator conclude note rendered in review")
for bad in ("onclick=", "onsubmit=", "onerror="):
    # scan the whole response for inline event handlers on DOM elements
    expect(bad not in body, f"no inline {bad} (CSP clean)")

print()
if failures:
    print(f"PHASE 7 FAILED ({failures}/{checks} checks)")
    sys.exit(1)
print(f"ALL PHASE 7 QA-PAYOUT CHECKS PASSED ({checks} checks)")
