"""Phase 6 (RedLab): Annotator chat UI & task claim hook.

Verifies:
 1. Visiting /annotator/task/<id> for a dynamic_red_team task auto-
    creates a Submission (status='in_progress') and a redlab Session
    linked by redlab_session_id.
 2. Returning GET returns the same session (idempotent) without
    creating duplicate Submission/Session rows.
 3. Standard (classification) tasks are completely unaffected — no
    Submission auto-created, form renders normally, no inline
    redteam <script> in the rendered HTML.
 4. CSP compliance: the task.html template has NO inline event
    handlers (no onload/onclick/onsubmit attributes) and NO inline
    <script> that strings user-controlled data into executable JS.
    The only inline <script> is the JSON bootstrap and a one-line
    assignment that parses the JSON block by id (never with
    document.write/eval/innerHTML).
 5. Concluding the session through the API flips the linked
    submission to 'submitted' with submitted_at set and creates the
    expected QA ConsensusGroup.
"""
from __future__ import annotations

import os
import re
import shutil
import sys

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
os.environ["SECRET_KEY"] = "phase6-test-secret-key-32chars-min-xx"

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


# Bootstrap Flask app with registered integration handlers.
from redlab.auth import _flask_app  # noqa: E402
flask_app = _flask_app()
from redlab.integration import register_integration_handlers  # noqa: E402
register_integration_handlers()

from extensions import db  # noqa: E402
from models import Project, Submission, Task, User  # noqa: E402
from redlab.crypto import encrypt_secret  # noqa: E402
from redlab.models import (  # noqa: E402
    Message, Session, Target, ensure_redlab_schema,
)

import hashlib  # noqa: E402
from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from redlab.auth import SESSION_COOKIE_NAME  # noqa: E402


def _cookie_ser():
    from flask.sessions import session_json_serializer
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


def make_cookie(uid: int, raw_csrf: str):
    payload = {"_user_id": str(uid), "_fresh": True, "csrf_token": raw_csrf}
    return _cookie_ser().dumps(payload), _csrf_signer().dumps(raw_csrf)


with flask_app.app_context():
    ensure_redlab_schema(db.engine)
    db.create_all()

    ann = User(id=1, email="ann6@example.com", role="annotator",
               password_hash="x", is_active=True, name="Ann6")
    db.session.add(ann)
    db.session.flush()

    # Red-team project
    p_rt = Project(id=9101, name="RedTeam P", client_name="C", task_type="dynamic_red_team",
                   created_by=1, status="active", instructions="inst",
                   required_annotations=1, redteam_base_pay=1.00,
                   redteam_breach_bounty=0.50, redteam_session_quota=0)
    # Standard classification project
    p_cls = Project(id=9102, name="Cls P", client_name="C2", task_type="classification",
                    created_by=1, status="active", instructions="inst",
                    required_annotations=1)
    db.session.add_all([p_rt, p_cls])
    db.session.flush()

    t_target = Target(id="target600000000000000000000001", project_ref="9101",
                      name="T", endpoint="https://93.184.216.34/v1/chat",
                      model_name="m",
                      api_key_ciphertext=encrypt_secret("sk-test-" + "c" * 32),
                      key_fingerprint="sk-...cccc", active=True)
    db.session.add(t_target)
    db.session.flush()

    # Pending red-team task + pending classification task
    t_rt = Task(id=9601, project_id=9101, input_data="rt prompt", status="pending")
    t_cls = Task(id=9602, project_id=9102, input_data="cls prompt", status="pending")
    db.session.add_all([t_rt, t_cls])
    db.session.commit()
    ann.record_consent()
    db.session.commit()


CSRF_RAW = "phase6-csrf-token-" + "z" * 32
COOKIE, CSRF = make_cookie(1, CSRF_RAW)


def _client():
    c = flask_app.test_client()
    c.set_cookie(SESSION_COOKIE_NAME, COOKIE)
    return c


def _claim(task_id: int, csrf: str = CSRF):
    c = _client()
    return c.post("/annotator/claim", data={"csrf_token": csrf},
                  follow_redirects=False)


def _get_task(task_id: int):
    c = _client()
    return c.get(f"/annotator/task/{task_id}")


# Claim the red-team task (conditional UPDATE flips it to 'assigned').
r = _claim(9601)
expect(r.status_code in (302, 303), f"claim red-team task → redirect (got {r.status_code})")

with flask_app.app_context():
    task = db.session.get(Task, 9601)
    expect(task.status == "assigned",
           f"redteam task assigned (got {task.status})")


# === 1. First GET creates Submission + Session =======================
print("\n== 1. First GET creates Submission + Session ==")
r = _get_task(9601)
expect(r.status_code == 200, f"GET task 200 (got {r.status_code})")
body = r.get_data(as_text=True)
expect("id=\"redteam-chat\"" in body, "redteam-chat container present")
expect("static/js/redteam_chat.js" in body, "redteam_chat.js included")

with flask_app.app_context():
    sub = Submission.query.filter_by(task_id=9601, annotator_id=1).first()
    expect(sub is not None, "submission created")
    if sub is not None:
        expect(sub.status == "in_progress",
               f"submission status=in_progress (got {sub.status})")
        expect(sub.redlab_session_id is not None,
               "submission.redlab_session_id set")
        sess = Session.query.get(sub.redlab_session_id)
        expect(sess is not None, "redlab session exists")
        if sess is not None:
            expect(sess.annotator_ref == "1", "session annotator correct")
            expect(sess.project_ref == "9101", "session project correct")
            expect(sess.target_id == "target600000000000000000000001",
                   "session target correct")
            SID = sess.id
        else:
            SID = None


# === 2. Second GET is idempotent ====================================
print("\n== 2. Second GET idempotent ==")
r2 = _get_task(9601)
expect(r2.status_code == 200, f"GET task 2nd 200 (got {r2.status_code})")
with flask_app.app_context():
    n_subs = Submission.query.filter_by(task_id=9601, annotator_id=1).count()
    expect(n_subs == 1, f"exactly one submission after re-GET (got {n_subs})")
    sub = Submission.query.filter_by(task_id=9601, annotator_id=1).first()
    n_sess = Session.query.filter_by(submission_ref=str(sub.id)).count()
    expect(n_sess == 1, f"exactly one session after re-GET (got {n_sess})")


# === 3. Standard classification task is unaffected ===================
print("\n== 3. Classification task unaffected ==")
_claim(9602)
r = _get_task(9602)
expect(r.status_code == 200, f"GET classification 200 (got {r.status_code})")
body_cls = r.get_data(as_text=True)
expect("id=\"redteam-chat\"" not in body_cls,
       "no redteam-chat container on classification page")
expect("static/js/redteam_chat.js" not in body_cls,
       "no redteam_chat.js on classification page")
expect("redteam-csrf-token" not in body_cls,
       "no redteam csrf data-attr on classification page")
with flask_app.app_context():
    # No auto-created submission yet (submission is only created on POST).
    n_inprog = Submission.query.filter_by(
        task_id=9602, annotator_id=1, status="in_progress"
    ).count()
    expect(n_inprog == 0,
           f"no auto-created in_progress submission (got {n_inprog})")
    expect('<form method="post"' in body_cls, "classification form rendered")


# === 4. CSP compliance ==============================================
print("\n== 4. CSP compliance ==")
INLINE_EVENT_RE = re.compile(
    r"\s(on(click|submit|load|error|change|input|keydown|keyup|mouseover"
    r"|focus|blur)\s*=)", re.IGNORECASE,
)
expect(not INLINE_EVENT_RE.search(body),
       "no inline event handlers (onclick/onsubmit/...) in redteam page")
# No eval()/document.write/innerHTML usage in static/js/redteam_chat.js.
with open(os.path.join(ROOT, "static/js/redteam_chat.js"), "r",
          encoding="utf-8") as fh:
    js = fh.read()
expect("eval(" not in js, "redteam_chat.js: no eval()")
expect("document.write" not in js, "redteam_chat.js: no document.write")
# innerHTML is used only for the flag-icon insertAdjacentHTML with a
# static string constant (no user data); check no user-controlled
# content flows through it.
expect(".innerHTML" not in js.replace("insertAdjacentHTML", ""),
       "redteam_chat.js: no .innerHTML assignment (insertAdjacentHTML ok)")
expect("window.__REDLAB_TRANSCRIPT__" in js,
       "chat bootstraps from window.__REDLAB_TRANSCRIPT__")
# The JSON bootstrap <script> type is application/json (not executable).
expect('type="application/json"' in body
       or "type='application/json'" in body,
       "transcript <script> has type=application/json (not executable)")


# === 5. Conclude flips submission to 'submitted' ====================
print("\n== 5. Conclude flips submission ==")
with flask_app.app_context():
    sub = Submission.query.filter_by(task_id=9601, annotator_id=1).first()
    SID = sub.redlab_session_id
    # Seed 3 turns so MIN_CONCLUDE_TURNS is satisfied.
    from redlab.models import Message as M
    for i in range(3):
        db.session.add(M(
            session_id=SID, turn_index=i * 2, role="user",
            content=f"u{i}", client_turn_id=f"s6-u{i}"))
        db.session.add(M(
            session_id=SID, turn_index=i * 2 + 1, role="assistant",
            content=f"a{i}"))
    s = db.session.get(Session, SID)
    s.turn_count = 3
    db.session.commit()

# Reach /redlab through the composed root ASGI app (same shape as
# asgi.py); Flask test_client alone can't see the FastAPI mount.
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient as ATestClient  # noqa: E402
from redlab.app import app as redlab_app  # noqa: E402
from redlab.flags import is_redlab_enabled  # noqa: E402
from a2wsgi import WSGIMiddleware  # noqa: E402

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


root.mount("/", WSGIMiddleware(flask_app))


def _asgi_client():
    c = ATestClient(root)
    c.cookies.set(SESSION_COOKIE_NAME, COOKIE)
    return c


c = _asgi_client()
r = c.post(f"/redlab/v1/sessions/{SID}/conclude",
           json={"note": "all done"},
           headers={"Content-Type": "application/json",
                    "X-CSRFToken": CSRF})
expect(r.status_code == 200, f"conclude 200 (got {r.status_code})")
expect(r.json()["session"]["status"] == "concluded", "session concluded")

with flask_app.app_context():
    sub = Submission.query.filter_by(task_id=9601, annotator_id=1).first()
    expect(sub.status == "submitted",
           f"submission flipped to submitted (got {sub.status})")
    expect(sub.submitted_at is not None, "submitted_at is set")
    expect(sub.redteam_state == "qa_pending",
           f"redteam_state=qa_pending (got {sub.redteam_state})")
    # Exactly one QA consensus group + 2 QA tasks created.
    from models import ConsensusGroup, Task as T
    cgs = ConsensusGroup.query.filter_by(project_id=9101).all()
    expect(len(cgs) >= 1, f"ConsensusGroup created (got {len(cgs)})")
    if cgs:
        qa_tasks = T.query.filter_by(consensus_group_id=cgs[-1].id).all()
        expect(len(qa_tasks) == 2,
               f"2 QA tasks under new group (got {len(qa_tasks)})")
        expect(all(t.status == "pending" for t in qa_tasks),
               "QA tasks pending")


print()
if FAIL:
    print(f"FAILED: {len(FAIL)} check(s) failed:")
    for f in FAIL:
        print(" -", f)
    sys.exit(1)
print("ALL PHASE 6 ANNOTATOR-UI CHECKS PASSED")
