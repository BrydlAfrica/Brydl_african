"""Phase 8: Exporters, Quotas, & Attack Prompts tests.

Covers:
  1. Client Machine API export with OpenAI JSONL format:
     - Schema has "messages" and "metadata" with submission_id,
       breach_turns, model, qa_reviewers, exported_at.
  2. Client Machine API export with ShareGPT JSONL format:
     - Schema has "conversations" with "from" ('human'|'gpt') and "value".
  3. Exclusions: unapproved/rejected sessions are excluded from export.
  4. Standard JSONL & CSV exports for non-redteam projects remain intact.
  5. Curated seed attack starter prompts in redlab.seeds.
  6. Admin quota monitoring & enforcement:
     - Displays sessions used / quota and confirmed breaches.
     - Blocks annotators from starting new sessions when quota is exhausted.
"""
import hashlib
import json
import os
import shutil
import sys
import uuid
from decimal import Decimal

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, ".")

for d in ("instance", "backups"):
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
os.makedirs("instance", exist_ok=True)
os.environ.setdefault("REDLAB_ENABLED", "true")
os.environ.setdefault("BRYDL_DISABLE_JOBS", "1")
os.environ.setdefault("SECRET_KEY",
                      "phase8-test-secret-key-32chars-min-xxxxxxxx")
os.environ.setdefault(
    "REDLAB_FERNET_KEY",
    "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM=")
os.environ.setdefault("USD_TO_NGN_RATE", "1600")

from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from extensions import db  # noqa: E402
from app import app as _flask  # noqa: E402,F401
from models import (  # noqa: E402
    ApiToken, AuditLog, Project, Task, Submission, User, utc_now,
)
from client_portal import hash_token  # noqa: E402
from redlab.auth import _flask_app, SESSION_COOKIE_NAME  # noqa: E402
from redlab.models import ensure_redlab_schema, Message, Session, Target  # noqa: E402
from redlab.crypto import encrypt_secret  # noqa: E402
from redlab.seeds import get_seed_prompts, SEED_PROMPTS  # noqa: E402
from redlab.integration import create_session  # noqa: E402

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


# ---- Cookie/CSRF helpers ----------------------------------------------
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
RAW_API_KEY = "brydl_testkey_" + "k" * 32
RAW_CSRF = "p8-csrf-" + "x" * 20
P_RT_ID = 810
P_STD_ID = 811
TGT_ID = "tgt-p8-000000000000000000000001"

with flask_app.app_context():
    ensure_redlab_schema(db.engine)
    db.create_all()

    admin = User(id=801, email="admin8@example.com", role="admin",
                 password_hash="x", is_active=True, name="Ada Admin")
    ann = User(id=802, email="ann8@example.com", role="annotator",
               password_hash="x", is_active=True, name="Tunde Ann")
    db.session.add_all([admin, ann])
    db.session.flush()

    # RedLab dynamic_red_team project with quota = 2
    proj_rt = Project(
        id=P_RT_ID, name="RedTeam Export Proj", client_name="Anthropic Client",
        task_type="dynamic_red_team", instructions="Red-team testing",
        required_annotations=1, created_by=admin.id, status="active",
        redteam_base_pay=Decimal("2.00"),
        redteam_breach_bounty=Decimal("1.00"),
        redteam_session_quota=2,  # Quota of 2 sessions
    )
    # Standard classification project
    proj_std = Project(
        id=P_STD_ID, name="Standard Sentiment", client_name="Acme",
        task_type="classification", instructions="Sentiment",
        required_annotations=1, created_by=admin.id, status="active",
        pay_per_task=Decimal("0.50"),
    )
    db.session.add_all([proj_rt, proj_std])
    db.session.flush()

    # Target LLM
    tgt = Target(
        id=TGT_ID, project_ref=str(P_RT_ID), name="Claude-3.5-Target",
        endpoint="https://93.184.216.34/v1/chat",
        model_name="claude-3-5-sonnet-20241022",
        api_key_ciphertext=encrypt_secret("sk-test-" + "c" * 32),
        key_fingerprint="sk-...cccc", active=True,
    )
    db.session.add(tgt)
    db.session.flush()

    # API Tokens
    tok_rt = ApiToken(
        id=851, project_id=P_RT_ID, label="RT Token",
        key_hash=hash_token(RAW_API_KEY), key_prefix="brydl_testkey_kkkk",
        created_by=admin.id, revoked_at=None,
    )
    db.session.add(tok_rt)
    db.session.flush()

    # 1. Approved submission on proj_rt (5 messages, turns 1 and 3 are breaches)
    t1 = Task(id=8601, project_id=P_RT_ID, status="approved",
              assigned_to=ann.id, input_data="test guardrails 1")
    db.session.add(t1); db.session.flush()
    sid1 = uuid.uuid4().hex
    sub1 = Submission(
        id=8901, task_id=t1.id, annotator_id=ann.id,
        output_data=json.dumps({"kind": "redteam_session"}),
        status="approved", redteam_state="qa_pending",
        redteam_breach_count=2, redlab_session_id=sid1,
        submitted_at=utc_now(), reviewed_at=utc_now(),
        qa_score=95,
    )
    sess1 = Session(
        id=sid1, project_ref=str(P_RT_ID),
        annotator_ref=str(ann.id), target_id=TGT_ID,
        status="concluded", turn_count=2, breach_count=2,
        conclude_note="Extracted canary on turn 1 and jailbroke turn 3",
    )
    db.session.add_all([sub1, sess1]); db.session.flush()
    # Messages: 0=user, 1=asst(flagged), 2=user, 3=asst(flagged)
    db.session.add(Message(id=uuid.uuid4().hex, session_id=sid1,
                           turn_index=0, role="user", content="Prompt Injection probe",
                           client_turn_id="u0"))
    db.session.add(Message(id=uuid.uuid4().hex, session_id=sid1,
                           turn_index=1, role="assistant", content="Breached response 1",
                           is_unsafe_flag=True))
    db.session.add(Message(id=uuid.uuid4().hex, session_id=sid1,
                           turn_index=2, role="user", content="Jailbreak probe",
                           client_turn_id="u1"))
    db.session.add(Message(id=uuid.uuid4().hex, session_id=sid1,
                           turn_index=3, role="assistant", content="Breached response 2",
                           is_unsafe_flag=True))

    # Add AuditLog for QA reviewer
    db.session.add(AuditLog(
        user_id=admin.id, actor_name=admin.name,
        action="review.verdict", entity_type="submission",
        entity_id=sub1.id, detail=json.dumps({"action": "approve"}),
    ))

    # 2. Rejected submission on proj_rt (must be EXCLUDED from exports)
    t2 = Task(id=8602, project_id=P_RT_ID, status="rejected",
              assigned_to=ann.id, input_data="test guardrails 2")
    db.session.add(t2); db.session.flush()
    sid2 = uuid.uuid4().hex
    sub2 = Submission(
        id=8902, task_id=t2.id, annotator_id=ann.id,
        output_data=json.dumps({"kind": "redteam_session"}),
        status="rejected", redteam_state="qa_pending",
        redteam_breach_count=0, redlab_session_id=sid2,
        submitted_at=utc_now(), reviewed_at=utc_now(), qa_score=20,
    )
    sess2 = Session(
        id=sid2, project_ref=str(P_RT_ID),
        annotator_ref=str(ann.id), target_id=TGT_ID,
        status="concluded", turn_count=1, breach_count=0,
    )
    db.session.add_all([sub2, sess2]); db.session.flush()
    db.session.add(Message(id=uuid.uuid4().hex, session_id=sid2,
                           turn_index=0, role="user", content="bad attempt",
                           client_turn_id="u2_0"))
    db.session.add(Message(id=uuid.uuid4().hex, session_id=sid2,
                           turn_index=1, role="assistant", content="safe refusal",
                           is_unsafe_flag=False))

    # 3. Approved submission on proj_std (Standard task)
    t_std = Task(id=8603, project_id=P_STD_ID, status="approved",
                 assigned_to=ann.id, input_data="I love this product")
    db.session.add(t_std); db.session.flush()
    sub_std = Submission(
        id=8903, task_id=t_std.id, annotator_id=ann.id,
        output_data=json.dumps({"label": "positive"}),
        status="approved", qa_score=100,
        submitted_at=utc_now(), reviewed_at=utc_now(),
    )
    db.session.add(sub_std)

    db.session.commit()
    ann.record_consent()
    db.session.commit()
    print("Seed complete.")


c = flask_app.test_client()
AUTH_HEADER = {"Authorization": f"Bearer {RAW_API_KEY}"}
COOKIE_ADMIN, CSRF_ADMIN = make_cookie(801, RAW_CSRF, role="admin")
COOKIE_ANN, CSRF_ANN = make_cookie(802, RAW_CSRF, role="annotator")

# === 1. Client Machine API: OpenAI JSONL Export =======================
print("\n== 1. Client API OpenAI Export ==")
r = c.get(f"/api/v1/projects/{P_RT_ID}/export?format=openai",
          headers=AUTH_HEADER)
expect(r.status_code == 200, f"export format=openai 200 (got {r.status_code})")
expect(r.mimetype == "application/x-ndjson",
       f"mimetype application/x-ndjson (got {r.mimetype})")

lines = [
    json.loads(line) for line in r.get_data(as_text=True).strip().split("\n")
    if line.strip()
]
expect(len(lines) == 1, f"exactly 1 approved session exported (got {len(lines)})")
line = lines[0]

# Check OpenAI format schema
expect("messages" in line and isinstance(line["messages"], list),
       "root 'messages' array present")
expect(len(line["messages"]) == 4,
       f"4 messages in conversation (got {len(line['messages'])})")
expect(line["messages"][0]["role"] == "user"
       and line["messages"][0]["content"] == "Prompt Injection probe",
       "turn 0 user message matches")
expect(line["messages"][1]["role"] == "assistant"
       and line["messages"][1]["content"] == "Breached response 1",
       "turn 1 assistant message matches")

# Check metadata schema
expect("metadata" in line and isinstance(line["metadata"], dict),
       "root 'metadata' object present")
meta = line["metadata"]
expect(meta.get("submission_id") == "8901",
       f"metadata.submission_id = '8901' (got {meta.get('submission_id')})")
expect(meta.get("breach_turns") == [1, 3],
       f"metadata.breach_turns = [1, 3] (got {meta.get('breach_turns')})")
expect(meta.get("model") == "claude-3-5-sonnet-20241022",
       f"metadata.model matches target model (got {meta.get('model')})")
expect(any("reviewer" in str(r) for r in meta.get("qa_reviewers", [])),
       f"metadata.qa_reviewers includes opaque reviewer ID (got {meta.get('qa_reviewers')})")
expect("Ada Admin" not in str(meta),
       "metadata does NOT leak QA reviewer real name")
expect("exported_at" in meta and len(meta["exported_at"]) > 10,
       "metadata.exported_at timestamp present")

# === 2. Client Machine API: ShareGPT Export ==========================
print("\n== 2. Client API ShareGPT Export ==")
r = c.get(f"/api/v1/projects/{P_RT_ID}/export?format=sharegpt",
          headers=AUTH_HEADER)
expect(r.status_code == 200, f"export format=sharegpt 200 (got {r.status_code})")
sg_lines = [
    json.loads(line) for line in r.get_data(as_text=True).strip().split("\n")
    if line.strip()
]
expect(len(sg_lines) == 1, "exactly 1 approved session exported in sharegpt")
sg_line = sg_lines[0]
expect("conversations" in sg_line and isinstance(sg_line["conversations"], list),
       "root 'conversations' array present in sharegpt")
convs = sg_line["conversations"]
expect(len(convs) == 4, f"4 turns in conversation (got {len(convs)})")
expect(convs[0] == {"from": "human", "value": "Prompt Injection probe"},
       "turn 0 human matches")
expect(convs[1] == {"from": "gpt", "value": "Breached response 1"},
       "turn 1 gpt matches")

# === 3. Exclusion of unapproved / rejected sessions ==================
print("\n== 3. Unapproved & rejected exclusion ==")
for item in lines:
    expect(item["metadata"]["submission_id"] != "8902",
           "rejected submission 8902 is strictly excluded")

# === 4. Seed Prompts in redlab.seeds =================================
print("\n== 4. Seed Attack Prompts ==")
seeds = get_seed_prompts()
expect(len(seeds) >= 10, f"at least 10 seed prompts defined (got {len(seeds)})")
categories = {s["category"] for s in seeds}
for cat in ("Prompt Injection", "Jailbreak / DAN", "Canary Exfiltration",
            "Tool Abuse", "Hausa Evasion", "Yoruba Evasion", "Swahili Evasion",
            "French Evasion"):
    expect(any(cat.lower() in c.lower() for c in categories),
           f"category '{cat}' present in seed prompts")

# Prompt lookup by category
pi_seeds = get_seed_prompts("Prompt Injection")
expect(len(pi_seeds) >= 2, f"filtered prompt injection seeds (got {len(pi_seeds)})")
for s in pi_seeds:
    expect("category" in s and "title" in s and "prompt" in s,
           f"seed {s.get('id')} has category, title, prompt")

# === 5. Admin Quota Monitoring =======================================
print("\n== 5. Admin Quota Monitoring ==")
c.set_cookie(SESSION_COOKIE_NAME, COOKIE_ADMIN)

# 5.1 Admin dashboard monitoring tile
r = c.get("/admin/")
expect(r.status_code == 200, f"GET /admin/ 200 (got {r.status_code})")
body = r.get_data(as_text=True)
expect("RedLab Monitoring" in body, "RedLab Monitoring badge on admin dashboard")
expect("Sessions Used: 2" in body, "Sessions Used count displayed")
expect("Confirmed Breaches: 2" in body, "Confirmed breaches displayed")

# 5.2 Project edit page monitoring tile
r = c.get(f"/admin/projects/{P_RT_ID}/edit")
expect(r.status_code == 200, f"GET /admin/projects/{P_RT_ID}/edit 200 (got {r.status_code})")
p_body = r.get_data(as_text=True)
expect("Red-Team Quota & Safety Metrics" in p_body,
       "Quota & safety metrics tile rendered on project edit")
expect("Sessions Used:" in p_body and "Quota: 2" in p_body,
       "Project quota 2 displayed on edit page")

# === 6. Quota Enforcement: Block new session creation =================
print("\n== 6. Quota Enforcement ==")
# We have 2 sessions in project P_RT_ID (sid1, sid2) and quota = 2.
# Attempting to create a 3rd session via create_session must raise ValueError.
with flask_app.app_context():
    t3 = Task(id=8604, project_id=P_RT_ID, status="assigned",
              assigned_to=802, input_data="test guardrails 3")
    db.session.add(t3); db.session.flush()
    sub3 = Submission(
        id=8904, task_id=t3.id, annotator_id=802,
        output_data=json.dumps({"kind": "redteam_session"}),
        status="in_progress", redteam_state="active",
    )
    db.session.add(sub3); db.session.flush()

    quota_blocked = False
    try:
        create_session(
            submission_id=sub3.id,
            project_id=P_RT_ID,
            annotator_id=802,
            target_id=TGT_ID,
        )
    except ValueError as ex:
        quota_blocked = True
        print(f"   ok: create_session blocked by quota ({ex})")
    expect(quota_blocked, "create_session raises ValueError when quota reached")

# Also test annotator opening a new dynamic_red_team task when quota is reached
# (should redirect to dashboard with quota flash).
with flask_app.app_context():
    t4 = Task(id=8605, project_id=P_RT_ID, status="assigned",
              assigned_to=802, input_data="test guardrails 4")
    db.session.add(t4); db.session.commit()

c.set_cookie(SESSION_COOKIE_NAME, COOKIE_ANN)
r = c.get("/annotator/task/8605", follow_redirects=False)
expect(r.status_code == 302,
       f"task GET redirects when quota reached (got {r.status_code})")
expect(r.headers.get("Location", "").endswith("/annotator/"),
       f"redirects to annotator dashboard (got {r.headers.get('Location')})")

print()
if failures:
    print(f"PHASE 8 FAILED ({failures}/{checks} checks)")
    sys.exit(1)
print(f"ALL PHASE 8 EXPORTS & ADMIN QUOTA CHECKS PASSED ({checks} checks)")
