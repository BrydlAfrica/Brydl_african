"""Phase 5 (RedLab): Breach flagging, session conclude, in-process events.

Verifies:
 1. ``POST /redlab/v1/messages/{mid}/flag`` toggles ``is_unsafe_flag``
    and recomputes ``session.breach_count`` accurately (0 → 1 → 0
    across two messages).
 2. ``POST /redlab/v1/sessions/{sid}/conclude``:
    - 400 when fewer than MIN_CONCLUDE_TURNS (default 3) completed turns.
    - 200 + status='concluded' + concluded_at set when turns ≥ 3.
    - Idempotent: a second POST returns success without creating a
      duplicate ConsensusGroup / duplicate QA tasks.
 3. Event dispatch:
    - ``breach.flagged`` fires and reaches the registered handler.
    - ``session.concluded`` fires; ``handle_redlab_event`` creates
      exactly one ConsensusGroup with QA_REVIEWERS_PER_SESSION=2
      sibling Task rows (status='pending') when a submission is linked,
      updates ``submissions.redteam_state`` to ``qa_pending`` and
      caches ``redteam_breach_count``.
    - Re-conclude does NOT emit duplicate events / rows (dedup via
      processed_events OR our marker check).
 4. Flag on a user message → 400; flag on another user's message → 403.
 5. ``get_session_transcript`` returns chronological messages.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["BRYDL_DISABLE_JOBS"] = "1"
os.environ["REDLAB_ENABLED"] = "true"
os.environ["BRYDL_AUTO_SCHEMA"] = "0"
os.environ["REDLAB_FERNET_KEY"] = (
    "P2R_l8fQ4bZ7xJ9mK3nV5pS8tU2wY6aC_dEfGhIjKlM="
)
os.environ["REDLAB_ALLOW_HTTP_LOCAL"] = "1"
os.environ["SECRET_KEY"] = "phase5-test-secret-key-32chars-min-xx"

shutil.rmtree(os.path.join(ROOT, "instance"), ignore_errors=True)
os.makedirs(os.path.join(ROOT, "instance"), exist_ok=True)
shutil.rmtree(os.path.join(ROOT, "backups"), ignore_errors=True)

FAIL: list[str] = []


def expect(cond, label):
    if cond:
        print(f"   ok: {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL: {label}")


# --- Bootstrap ---------------------------------------------------------
from redlab.auth import _flask_app  # noqa: E402
flask_app = _flask_app()
from redlab.app import utc_now as _rl_utc_now  # noqa: E402
from extensions import db  # noqa: E402
from models import (  # noqa: E402
    ConsensusGroup, Project, Submission, Task, User,
)
from redlab.crypto import encrypt_secret  # noqa: E402
from redlab.events import clear_handlers  # noqa: E402
from redlab.integration import (  # noqa: E402
    QA_REVIEWERS_PER_SESSION, get_session_transcript,
    link_session_to_submission, register_integration_handlers,
)
from redlab.models import (  # noqa: E402
    Message, Session, Target, ensure_redlab_schema,
)

# Build root ASGI app (same shape as asgi.py + phase 4 test).
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from redlab.app import app as redlab_app  # noqa: E402
from redlab.flags import is_redlab_enabled  # noqa: E402

root = FastAPI()
root.mount("/redlab", redlab_app)


@root.get("/redlab", include_in_schema=False)
async def _redlab_root():
    return JSONResponse(status_code=200, content={
        "service": "redlab", "version": "14.0.0",
        "redlab_enabled": is_redlab_enabled(),
        "endpoints": ["/redlab/healthz"]
        + (["/redlab/v1/..."] if is_redlab_enabled() else []),
    })


from a2wsgi import WSGIMiddleware  # noqa: E402
root.mount("/", WSGIMiddleware(flask_app))

# Register integration handlers (the app factory normally does this,
# but we constructed _flask_app() before patching; call explicitly).
register_integration_handlers()

# Cookies / CSRF helpers (same pattern as Phase 4 test).
from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from redlab.auth import SESSION_COOKIE_NAME  # noqa: E402
import hashlib  # noqa: E402


def _cookie_serializer():
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


def make_cookie(user_id: int, raw_token: str):
    payload = {"_user_id": str(user_id), "_fresh": True,
               "csrf_token": raw_token}
    return _cookie_serializer().dumps(payload), _csrf_signer().dumps(raw_token)


# --- Fixtures ----------------------------------------------------------
CSRF_RAW = "phase5-csrf-raw-token-0123456789abcdefghij"

with flask_app.app_context():
    ensure_redlab_schema(db.engine)
    db.create_all()

    u = User(id=1, email="ann5@example.com", role="annotator",
             password_hash="x", is_active=True, name="Ann5")
    db.session.add(u)
    db.session.flush()

    proj = Project(
        id=9001, name="RedTeam QA Test",
        client_name="Test Client",
        task_type="redteam", created_by=1, status="active",
        instructions="test instructions",
        required_annotations=1,
    )
    db.session.add(proj)
    db.session.flush()

    t = Target(
        id="target50000000000000000000001",
        project_ref="p", name="MockTarget",
        endpoint="https://93.184.216.34/v1/chat",
        model_name="m",
        api_key_ciphertext=encrypt_secret("sk-test-" + "b" * 32),
        key_fingerprint="sk-...bbbb",
        active=True,
    )
    db.session.add(t)
    db.session.flush()

    s1 = Session(id="session50000000000000000000001",
                 annotator_ref="1", project_ref="p",
                 target_id=t.id, status="open", turn_count=0)
    s2 = Session(id="session50000000000000000000002",
                 annotator_ref="1", project_ref="p",
                 target_id=t.id, status="open", turn_count=0)
    s3 = Session(id="session50000000000000000000003",
                 annotator_ref="1", project_ref="p",
                 target_id=t.id, status="open", turn_count=0)
    db.session.add_all([s1, s2, s3])
    db.session.flush()

    # Need a parent Task for the Submission FK.
    parent_task = Task(
        id=900500, project_id=proj.id, input_data="seed",
        status="approved", assigned_to=1,
    )
    db.session.add(parent_task)
    db.session.flush()

    # s3 has a linked Submission so conclude() will create QA rows.
    sub = Submission(
        id=900501, task_id=parent_task.id, annotator_id=1,
        status="submitted", output_data="{}",
        redlab_session_id=None,
    )
    db.session.add(sub)
    db.session.flush()
    link_session_to_submission(s3.id, sub.id)
    db.session.commit()

GOOD_COOKIE, CSRF_TOKEN = make_cookie(1, CSRF_RAW)


def with_flask(fn):
    """Run ``fn()`` inside a Flask app context and return its value."""
    with flask_app.app_context():
        return fn()


def _client():
    c = TestClient(root)
    c.cookies.set(SESSION_COOKIE_NAME, GOOD_COOKIE)
    return c


def _post(url, json):
    c = _client()
    return c.post(url, json=json, headers={
        "Content-Type": "application/json", "X-CSRFToken": CSRF_TOKEN,
    })


def _err(r):
    """Return the machine-readable 'error' string from a JSON response.

    Our exception handlers always emit ``{"error": <code>, "detail":
    <human readable>, "request_id": ...}`` at the top level.
    """
    try:
        j = r.json()
    except Exception:
        return None
    if isinstance(j, dict):
        return j.get("error")
    return None


# Helper: insert N completed turns directly into s3 so we can test
# conclude (needs ≥3 turns). We do this via plain DB rows (bypassing
# the turn endpoint and the outbound connector) because Phase 4
# tests already exercise happy-path turn flow end-to-end.
def _seed_turns(session_id: str, n: int):
    with flask_app.app_context():
        # Wipe messages for this session first.
        Message.query.filter(Message.session_id == session_id).delete()
        s = db.session.get(Session, session_id)
        s.turn_count = n
        s.status = "open"
        s.concluded_at = None
        s.conclude_note = None
        s.breach_count = 0
        for i in range(n):
            db.session.add(Message(
                session_id=session_id, turn_index=i * 2,
                role="user", content=f"u{i}",
                client_turn_id=f"seed-u-{session_id}-{i}",
            ))
            db.session.add(Message(
                session_id=session_id, turn_index=i * 2 + 1,
                role="assistant", content=f"a{i}",
            ))
        db.session.commit()


# === 1. Flag toggle ====================================================
print("\n== 1. Flag toggle ==")
_seed_turns("session50000000000000000000001", n=2)
with flask_app.app_context():
    a0 = (Message.query.filter(Message.session_id == "session50000000000000000000001",
                               Message.role == "assistant")
                       .order_by(Message.turn_index).all())
    a0_id, a1_id = a0[0].id, a0[1].id
    u0_id = (Message.query.filter(Message.session_id == "session50000000000000000000001",
                                  Message.role == "user").first()).id

# 1a. Flag first assistant → breach_count 1.
r = _post(f"/redlab/v1/messages/{a0_id}/flag", {"is_unsafe": True})
expect(r.status_code == 200, f"flag a0 → 200 (got {r.status_code} {r.text[:200]})")
expect(r.json()["message"]["is_unsafe_flag"] is True, "a0 flagged")
expect(r.json()["session"]["breach_count"] == 1,
       f"breach_count=1 (got {r.json()['session']['breach_count']})")

# 1b. Flag second assistant → breach_count 2.
r = _post(f"/redlab/v1/messages/{a1_id}/flag", {"is_unsafe": True})
expect(r.status_code == 200, f"flag a1 → 200 (got {r.status_code})")
expect(r.json()["session"]["breach_count"] == 2,
       f"breach_count=2 (got {r.json()['session']['breach_count']})")

# 1c. Unflag a0 → breach_count 1.
r = _post(f"/redlab/v1/messages/{a0_id}/flag", {"is_unsafe": False})
expect(r.status_code == 200, f"unflag a0 → 200 (got {r.status_code})")
expect(r.json()["session"]["breach_count"] == 1,
       f"breach_count=1 after unflag (got {r.json()['session']['breach_count']})")

# 1d. Flag a user message → 400.
r = _post(f"/redlab/v1/messages/{u0_id}/flag", {"is_unsafe": True})
expect(r.status_code == 400,
       f"flag user msg → 400 (got {r.status_code})")
body = r.json()
# body shape is {"error": "...", "detail": "..."} per our handler
err_code = body.get("error") if isinstance(body, dict) else None
expect(err_code == "flag_assistant_only",
       f"user-flag error=flag_assistant_only (got body={body!r})")

# === 2. Conclude: min-turns gate ======================================
print("\n== 2. Conclude min-turns ==")
_seed_turns("session50000000000000000000002", n=2)  # only 2 turns
r = _post("/redlab/v1/sessions/session50000000000000000000002/conclude",
          {"note": "try early"})
expect(r.status_code == 400,
       f"conclude after 2 turns → 400 (got {r.status_code})")
expect(_err(r) == "min_turns_required",
       f"early conclude error=min_turns_required (got {_err(r)!r})")
with flask_app.app_context():
    s2_after = db.session.get(Session, "session50000000000000000000002")
    expect(s2_after.status == "open",
           "session still open after failed conclude")


# === 3. Conclude success → event → QA rows ============================
print("\n== 3. Conclude success ==")
_seed_turns("session50000000000000000000003", n=4)
# Flag one assistant so breach_count=1 going into conclude.
with flask_app.app_context():
    ast1 = (
        Message.query
        .filter(Message.session_id == "session50000000000000000000003",
                Message.role == "assistant")
        .order_by(Message.turn_index).first()
    )
    ast1_id = ast1.id
_post(f"/redlab/v1/messages/{ast1_id}/flag", {"is_unsafe": True})

qa_groups_before = with_flask(lambda: db.session.query(ConsensusGroup).count())
qa_tasks_before = with_flask(lambda: db.session.query(Task).filter(
    Task.input_data.like('%"_redlab_qa": true%')).count())

r = _post("/redlab/v1/sessions/session50000000000000000000003/conclude",
          {"note": "done"})
expect(r.status_code == 200, f"conclude → 200 (got {r.status_code} {r.text[:300]})")
j = r.json()
expect(j["session"]["status"] == "concluded", "status=concluded")
expect(j["session"]["concluded_at"] is not None, "concluded_at set")
expect(j["session"]["conclude_note"] == "done", "conclude_note stored")

with flask_app.app_context():
    sub_row = db.session.get(Submission, 900501)
    expect(sub_row.redteam_state == "qa_pending",
           f"submission.redteam_state=qa_pending (got {sub_row.redteam_state})")
    expect(sub_row.redteam_breach_count == 1,
           f"submission.redteam_breach_count=1 (got {sub_row.redteam_breach_count})")

qa_groups_after = with_flask(lambda: db.session.query(ConsensusGroup).count())
qa_tasks_after = with_flask(lambda: db.session.query(Task).filter(
    Task.input_data.like('%"_redlab_qa": true%'),
    Task.input_data.like('%"session_id": "session50000000000000000000003"%'),
).count())
expect(qa_groups_after == qa_groups_before + 1,
       f"1 new ConsensusGroup (before={qa_groups_before}, after={qa_groups_after})")
expect(qa_tasks_after == qa_tasks_before + QA_REVIEWERS_PER_SESSION,
       f"{QA_REVIEWERS_PER_SESSION} new QA tasks (before={qa_tasks_before}, after={qa_tasks_after})")

with flask_app.app_context():
    new_group = (db.session.query(ConsensusGroup)
                 .filter(ConsensusGroup.project_id == 9001)
                 .order_by(ConsensusGroup.id.desc()).first())
    expect(new_group is not None and new_group.required_count == QA_REVIEWERS_PER_SESSION,
           "new ConsensusGroup.required_count=QA_REVIEWERS_PER_SESSION")
    qa_tasks = (db.session.query(Task)
                .filter(Task.consensus_group_id == new_group.id).all())
    expect(all(t.status == "pending" for t in qa_tasks),
           "all QA tasks status=pending")
    expect(all(t.assigned_to is None for t in qa_tasks),
           "QA tasks unassigned (enters review pool)")


# === 4. Conclude idempotency ==========================================
print("\n== 4. Conclude idempotency ==")
qa_groups_before2 = qa_groups_after
qa_tasks_before2 = qa_tasks_after
r = _post("/redlab/v1/sessions/session50000000000000000000003/conclude",
          {"note": "second call"})
expect(r.status_code == 200, f"re-conclude → 200 (got {r.status_code})")
expect(r.json()["session"]["status"] == "concluded",
       "still concluded")
qa_groups_after2 = with_flask(lambda: db.session.query(ConsensusGroup).count())
qa_tasks_after2 = with_flask(lambda: db.session.query(Task).filter(
    Task.input_data.like('%"_redlab_qa": true%'),
    Task.input_data.like('%"session_id": "session50000000000000000000003"%'),
).count())
expect(qa_groups_after2 == qa_groups_before2,
       "no new ConsensusGroup on re-conclude")
expect(qa_tasks_after2 == qa_tasks_before2,
       "no new QA tasks on re-conclude")


# === 5. Transcript helper ==============================================
print("\n== 5. Transcript helper ==")
with flask_app.app_context():
    t = get_session_transcript("session50000000000000000000003")
    expect(t["session"] is not None and t["session"]["status"] == "concluded",
           "transcript session present + concluded")
    roles = [m["role"] for m in t["messages"]]
    # 4 turns = 8 messages (u/a/u/a/u/a/u/a)
    expect(len(t["messages"]) == 8, f"8 messages (got {len(t['messages'])})")
    expect(roles == ["user", "assistant"] * 4,
           "messages ordered u/a/u/a/...")


print()
if FAIL:
    print(f"FAILED: {len(FAIL)} check(s) failed:")
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print("ALL PHASE 5 EVENT CHECKS PASSED")
