"""Phase 4 (RedLab): Turn API & session-cookie auth.

Verifies:
 1. ``POST /redlab/v1/sessions/{sid}/turn`` accepts an authenticated
    annotator with a valid CSRF header and writes user + assistant
    messages atomically.
 2. Unauthenticated / bad-cookie / missing-CSRF / wrong-CSRF / wrong
    session-owner requests are rejected with the correct JSON error.
 3. Turn cap: the 41st turn is rejected (after 40 successful turns).
 4. Idempotency: re-POSTing the same ``client_turn_id`` returns the
    cached pair and does not increment turn_count, insert new rows,
    or re-invoke the connector.
 5. Atomicity on connector failure: simulated client failure writes
    ZERO rows, leaves turn_count unchanged, and surfaces a structured
    502/503 error.
 6. 404 for unknown session id; 400 for concluded session.
 7. 400 for oversize content (>4000 chars).

The test uses FastAPI's ``TestClient`` against the root ASGI app (so
the ``/redlab/...`` mount is exercised end-to-end) but injects a
Flask-signed session cookie directly rather than driving a real
login form — that keeps the suite hermetic and fast. CSRF tokens are
signed with the same ``itsdangerous`` primitives flask-wtf uses so
the :func:`verify_csrf_header` dependency sees a value it recognises.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["BRYDL_DISABLE_JOBS"] = "1"
os.environ["REDLAB_ENABLED"] = "true"
os.environ["BRYDL_AUTO_SCHEMA"] = "0"
os.environ["REDLAB_FERNET_KEY"] = (
    "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM="
)
os.environ["SECRET_KEY"] = "phase4-test-secret-key-32chars-min-xx"
os.environ["REDLAB_ALLOW_HTTP_LOCAL"] = "1"

shutil.rmtree(os.path.join(ROOT, "instance"), ignore_errors=True)
os.makedirs(os.path.join(ROOT, "instance"), exist_ok=True)
shutil.rmtree(os.path.join(ROOT, "backups"), ignore_errors=True)

FAIL: list[str] = []


def expect(cond: bool, label: str) -> None:
    if cond:
        print(f"   ok: {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL: {label}")


# --- App / DB bootstrap -------------------------------------------------
# Use the same cached Flask app that redlab.auth / redlab.app use, so
# the SQLAlchemy session registry and db metadata line up across the
# FastAPI worker thread (which reads via redlab.auth._flask_app) and
# the test's setup/assertions (which also need to see the same rows).
from redlab.auth import _flask_app  # noqa: E402
flask_app = _flask_app()

from extensions import db  # noqa: E402
from models import User  # noqa: E402
from redlab.crypto import encrypt_secret  # noqa: E402
from redlab.models import Message, Session, Target  # noqa: E402

# Build the root ASGI app (mirrors asgi.py composition).
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from redlab.app import app as redlab_app  # noqa: E402
from redlab.flags import is_redlab_enabled  # noqa: E402

root = FastAPI()
root.mount("/redlab", redlab_app)


@root.get("/redlab", include_in_schema=False)
async def _redlab_root():
    return JSONResponse(
        status_code=200,
        content={
            "service": "redlab",
            "version": "14.0.0",
            "redlab_enabled": is_redlab_enabled(),
            "endpoints": ["/redlab/healthz"]
            + (["/redlab/v1/..."] if is_redlab_enabled() else []),
        },
    )


from a2wsgi import WSGIMiddleware  # noqa: E402
root.mount("/", WSGIMiddleware(flask_app))


# --- Helpers: signed cookies & CSRF -------------------------------------
from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from redlab.auth import SESSION_COOKIE_NAME  # noqa: E402


def _serializer():
    """Cookie serializer matching Flask's SecureCookieSessionInterface."""
    from flask.sessions import session_json_serializer
    import hashlib
    sk = flask_app.secret_key
    if isinstance(sk, (list, tuple)):
        sk = sk[0]
    return URLSafeTimedSerializer(
        secret_key=sk, salt="cookie-session",
        serializer=session_json_serializer,
        signer_kwargs={"key_derivation": "hmac",
                       "digest_method": hashlib.sha1},
    )


# flask-wtf CSRF tokens use default URLSafeTimedSerializer settings
# (plain JSON, itsdangerous defaults — no signer_kwargs override).
def _sign_csrf(token: str) -> str:
    sk = flask_app.secret_key
    if isinstance(sk, (list, tuple)):
        sk = sk[0]
    s = URLSafeTimedSerializer(secret_key=sk, salt="wtf-csrf-token")
    return s.dumps(token)


def make_cookie(user_id: int, raw_csrf_token: str):
    """Return (cookie_value, signed_csrf_header_value)."""
    # session["csrf_token"] holds the RAW random token; the client
    # sends the signed form in X-CSRFToken (matching flask-wtf's
    # generate_csrf() flow).
    payload = {
        "_user_id": str(user_id),
        "_fresh": True,
        "csrf_token": raw_csrf_token,
    }
    cookie = _serializer().dumps(payload)
    signed = _sign_csrf(raw_csrf_token)
    return cookie, signed


# --- DB fixtures --------------------------------------------------------
with flask_app.app_context():
    from redlab.models import ensure_redlab_schema
    ensure_redlab_schema(db.engine)
    db.create_all()
    # Create an annotator user (id=1) and an unrelated user (id=2).
    u1 = User(id=1, email="ann@example.com", role="annotator",
              password_hash="x", is_active=True, name="Ann")
    u2 = User(id=2, email="other@example.com", role="annotator",
              password_hash="x", is_active=True, name="Other")
    u3 = User(id=3, email="inactive@example.com", role="annotator",
              password_hash="x", is_active=False, name="Gone")
    db.session.add_all([u1, u2, u3])
    db.session.commit()

    # Create a Target with an SSRF-valid endpoint (https to public IP
    # literal — SSRF allows it; the tests inject a MockTransport).
    t = Target(
        id="target000000000000000000000001",
        project_ref="proj001",
        name="MockTarget",
        endpoint="https://93.184.216.34/v1/chat",
        model_name="mock-model",
        api_key_ciphertext=encrypt_secret("sk-test-" + "a" * 32),
        key_fingerprint="sk-...aaaa",
        ssrf_validated_at=None,
        active=True,
    )
    db.session.add(t)
    db.session.commit()

    # An open session owned by u1.
    s1 = Session(
        id="session000000000000000000000001",
        annotator_ref="1", project_ref="proj001",
        target_id=t.id, status="open", turn_count=0,
    )
    # A concluded session.
    s2 = Session(
        id="session000000000000000000000002",
        annotator_ref="1", project_ref="proj001",
        target_id=t.id, status="concluded", turn_count=0,
    )
    # A session owned by u2.
    s3 = Session(
        id="session000000000000000000000003",
        annotator_ref="2", project_ref="proj001",
        target_id=t.id, status="open", turn_count=0,
    )
    db.session.add_all([s1, s2, s3])
    db.session.commit()

CSRF_RAW = "csrf-phase4-token-abcdef0123456789" * 2  # long enough; hashed
GOOD_COOKIE, CSRF_TOKEN = make_cookie(1, CSRF_RAW)
OTHER_COOKIE, _ = make_cookie(2, CSRF_RAW)
INACTIVE_COOKIE, _ = make_cookie(3, CSRF_RAW)
BAD_COOKIE = GOOD_COOKIE + "tamper"

TURN_URL_SID1 = "/redlab/v1/sessions/session000000000000000000000001/turn"
TURN_URL_SID2 = "/redlab/v1/sessions/session000000000000000000000002/turn"
TURN_URL_SID3 = "/redlab/v1/sessions/session000000000000000000000003/turn"
TURN_URL_MISSING = "/redlab/v1/sessions/session00000000000000000000ffff/turn"


# --- Monkey-patch call_client_model to inject mocked responses -------
# We override redlab.connector.call_client_model so we can (a) return a
# canned assistant response for successful calls, (b) count how many
# times it was invoked (idempotency tests), (c) simulate failures for
# the atomicity test.
import redlab.app as redlab_app_module  # noqa: E402
import redlab.connector as connector_mod  # noqa: E402
import asyncio  # noqa: E402

_real_call = connector_mod.call_client_model
_call_count = {"n": 0}
_failure_mode = {"code": None}  # None means success; else error code
_assistant_reply = {"text": "hello from mock"}


async def _fake_call_client_model(target, history, **kwargs):
    _call_count["n"] += 1
    if _failure_mode["code"] is not None:
        return False, f"mock failure ({_failure_mode['code']})", _failure_mode["code"]
    return True, _assistant_reply["text"], None


# Override at the point of use (redlab.app already imported it under
# its own name, so patch both).
redlab_app_module.call_client_model = _fake_call_client_model


def reset_call_state(failure_code=None, reply_text="hello from mock"):
    _call_count["n"] = 0
    _failure_mode["code"] = failure_code
    _assistant_reply["text"] = reply_text


# --- Convenience: reset session between tests --------------------------
def fresh_session_s1():
    """Reset s1 to zero turns / delete messages — so tests start clean."""
    with flask_app.app_context():
        Message.query.filter(
            Message.session_id == "session000000000000000000000001"
        ).delete(synchronize_session=False)
        s = db.session.get(Session, "session000000000000000000000001")
        s.turn_count = 0
        s.status = "open"
        db.session.commit()


def post_turn(url, cookie=GOOD_COOKIE, csrf=CSRF_TOKEN,
              content="hi", client_turn_id=None):
    if client_turn_id is None:
        client_turn_id = uuid.uuid4().hex
    c = TestClient(root)
    headers = {"Content-Type": "application/json"}
    if csrf is not None:
        headers["X-CSRFToken"] = csrf
    if cookie is not None:
        c.cookies.set(SESSION_COOKIE_NAME, cookie)
    return c.post(url, json={"content": content,
                             "client_turn_id": client_turn_id},
                  headers=headers)


# === 1. Auth tests ======================================================
print("\n== 1. Auth ==")

# 1a. No cookie → 401 not_authenticated.
reset_call_state()
r = TestClient(root).post(TURN_URL_SID1,
                          json={"content": "hi",
                                "client_turn_id": "cid01"},
                          headers={"Content-Type": "application/json",
                                   "X-CSRFToken": CSRF_TOKEN})
expect(r.status_code == 401, f"no cookie → 401 (got {r.status_code})")
expect(r.json().get("error") == "not_authenticated",
       "no cookie error=not_authenticated")

# 1b. Tampered cookie → 401 bad_session.
r = post_turn(TURN_URL_SID1, cookie=BAD_COOKIE)
expect(r.status_code == 401, f"bad cookie → 401 (got {r.status_code})")
expect(r.json().get("error") == "bad_session",
       "bad cookie error=bad_session")

# 1c. Missing CSRF header → 403 csrf_missing.
r = post_turn(TURN_URL_SID1, csrf=None)
expect(r.status_code == 403, f"no CSRF → 403 (got {r.status_code})")
expect(r.json().get("error") == "csrf_missing",
       "no CSRF error=csrf_missing")

# 1d. Wrong CSRF token → 403 csrf_invalid.
r = post_turn(TURN_URL_SID1, csrf="wrong-token-value")
expect(r.status_code == 403, f"bad CSRF → 403 (got {r.status_code})")
expect(r.json().get("error") == "csrf_invalid",
       "bad CSRF error=csrf_invalid")

# 1e. Inactive user → 403 account_disabled.
r = post_turn(TURN_URL_SID1, cookie=INACTIVE_COOKIE)
expect(r.status_code == 403, f"inactive user → 403 (got {r.status_code})")
expect(r.json().get("error") == "account_disabled",
       "inactive user error=account_disabled")

# 1f. Session owned by another annotator → 403 session_not_yours.
r = post_turn(TURN_URL_SID3, cookie=GOOD_COOKIE)
expect(r.status_code == 403, f"other's session → 403 (got {r.status_code})")
expect(r.json().get("error") == "session_not_yours",
       "other's session error=session_not_yours")

# 1g. Missing session id → 404.
r = post_turn(TURN_URL_MISSING)
expect(r.status_code == 404, f"unknown session → 404 (got {r.status_code})")
expect(r.json().get("error") == "session_not_found",
       "unknown session error=session_not_found")

# 1h. Concluded session → 400 session_concluded.
r = post_turn(TURN_URL_SID2)
expect(r.status_code == 400,
       f"concluded session → 400 (got {r.status_code})")
expect(r.json().get("error") == "session_concluded",
       "concluded session error=session_concluded")


# === 2. Happy path ======================================================
print("\n== 2. Happy path ==")
fresh_session_s1()
reset_call_state()

r = post_turn(TURN_URL_SID1, content="hello annotator",
              client_turn_id="happy01")
expect(r.status_code == 200,
       f"happy 200 (got {r.status_code} body={r.text[:200]})")
data = r.json()
expect(data["user_message"]["role"] == "user",
       "user message role=user")
expect(data["user_message"]["content"] == "hello annotator",
       "user content echoed")
expect(data["user_message"]["client_turn_id"] == "happy01",
       "user message carries client_turn_id")
expect(data["assistant_message"]["role"] == "assistant",
       "assistant message role=assistant")
expect(data["assistant_message"]["content"] == "hello from mock",
       "assistant content from (mocked) connector")
expect(data["session"]["turn_count"] == 1,
       "turn_count bumped to 1")
expect(data["session"]["id"] == "session000000000000000000000001",
       "session id correct")
expect(_call_count["n"] == 1,
       f"connector called exactly once (got {_call_count['n']})")

# DB rows check: exactly 2 messages (1 user, 1 assistant) with
# matching turn indices (user at 0, assistant at 1).
with flask_app.app_context():
    msgs = (Message.query
            .filter(Message.session_id == "session000000000000000000000001")
            .order_by(Message.turn_index).all())
    expect(len(msgs) == 2,
           f"2 messages written after 1 turn (got {len(msgs)})")
    user_m = next((m for m in msgs if m.role == "user"), None)
    ast_m = next((m for m in msgs if m.role == "assistant"), None)
    expect(user_m is not None and ast_m is not None,
           "both user + assistant rows present")
    expect(user_m.turn_index == 0 and ast_m.turn_index == 1,
           f"user turn_index=0, assistant turn_index=1 (got u={user_m.turn_index if user_m else None}, a={ast_m.turn_index if ast_m else None})")
    expect(ast_m.client_turn_id is None,
           "assistant row has NULL client_turn_id")


# === 3. Turn cap (40) ===================================================
print("\n== 3. Turn cap ==")
fresh_session_s1()
reset_call_state()

# Push 40 successful turns.
with flask_app.app_context():
    s = db.session.get(Session, "session000000000000000000000001")
    s.turn_count = 40
    db.session.commit()

r = post_turn(TURN_URL_SID1, content="should be rejected",
              client_turn_id="cap41")
expect(r.status_code == 400,
       f"41st turn → 400 (got {r.status_code})")
expect(r.json().get("error") == "turn_cap_reached",
       "41st turn error=turn_cap_reached")
# Verify connector was NOT called and no new rows written.
expect(_call_count["n"] == 0,
       "connector NOT called on turn-cap rejection")
with flask_app.app_context():
    n_msgs = (Message.query
              .filter(Message.session_id == "session000000000000000000000001")
              .count())
    expect(n_msgs == 0,
           f"zero messages added by rejected turn (got {n_msgs})")


# === 4. Idempotency =====================================================
print("\n== 4. Idempotency ==")
fresh_session_s1()
reset_call_state(reply_text="first reply")

r1 = post_turn(TURN_URL_SID1, content="idem-content",
               client_turn_id="idem-001")
expect(r1.status_code == 200,
       f"first POST 200 (got {r1.status_code})")
first_assistant_id = r1.json()["assistant_message"]["id"]
expect(_call_count["n"] == 1,
       f"connector called on first POST (n={_call_count['n']})")

# Second POST with the same client_turn_id — connector must NOT be
# called, turn_count must NOT increase, same assistant id returned.
reset_call_state(reply_text="SECOND REPLY SHOULD NOT APPEAR")
r2 = post_turn(TURN_URL_SID1, content="idem-content",
               client_turn_id="idem-001")
expect(r2.status_code == 200,
       f"retry POST 200 (got {r2.status_code})")
expect(r2.json()["assistant_message"]["id"] == first_assistant_id,
       "retry returns cached assistant id (no new row)")
expect(r2.json()["assistant_message"]["content"] == "first reply",
       "retry returns original assistant content (not re-called)")
expect(_call_count["n"] == 0,
       f"connector NOT called on idempotent retry (n={_call_count['n']})")

with flask_app.app_context():
    s = db.session.get(Session, "session000000000000000000000001")
    expect(s.turn_count == 1,
           f"turn_count still 1 after retry (got {s.turn_count})")
    n_msgs = (Message.query
              .filter(Message.session_id == "session000000000000000000000001")
              .count())
    expect(n_msgs == 2,
           f"still exactly 2 messages (got {n_msgs})")


# === 5. Atomicity on connector failure =================================
print("\n== 5. Atomicity on connector failure ==")
fresh_session_s1()
reset_call_state(failure_code="client_unreachable")

r = post_turn(TURN_URL_SID1, content="will-fail",
              client_turn_id="fail-001")
expect(r.status_code == 502,
       f"connector failure → 502 (got {r.status_code})")
expect(r.json().get("error") == "client_unreachable",
       "connector failure error=client_unreachable")

# No messages written, turn_count unchanged.
with flask_app.app_context():
    s = db.session.get(Session, "session000000000000000000000001")
    expect(s.turn_count == 0,
           f"turn_count still 0 after failure (got {s.turn_count})")
    n_msgs = (Message.query
              .filter(Message.session_id == "session000000000000000000000001")
              .count())
    expect(n_msgs == 0,
           f"zero messages written after failure (got {n_msgs})")

# Also verify rate-limited → 503 mapping.
reset_call_state(failure_code="client_rate_limited")
r = post_turn(TURN_URL_SID1, content="rate-limited",
              client_turn_id="rate-001")
expect(r.status_code == 503,
       f"rate_limited → 503 (got {r.status_code})")
expect(r.json().get("error") == "client_rate_limited",
       "rate_limited error=client_rate_limited")


# === 6. Oversize content ===============================================
print("\n== 6. Oversize content ==")
fresh_session_s1()
reset_call_state()
big = "x" * 4001
r = post_turn(TURN_URL_SID1, content=big, client_turn_id="big01")
expect(r.status_code == 422 or r.status_code == 400,
       f"oversize content → 4xx (got {r.status_code})")
expect(_call_count["n"] == 0,
       "connector NOT called for oversize content")


# === Final verdict ======================================================
print()
if FAIL:
    print(f"FAILED: {len(FAIL)} check(s) failed:")
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print("ALL PHASE 4 TURN-API CHECKS PASSED")
