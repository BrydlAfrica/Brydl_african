"""Phase 10 suite: the machine API tier (Bearer keys, v1).

Covers: unauthenticated ping, the 401 matrix (missing / unknown /
revoked keys, no session fallback), project scoping (foreign projects
are uniform 404s), the progress + client-safe results contracts
(annotator identity, gold markers and reviewer notes never appear),
task submission (validation, batch caps, paused refusal, audit), the
per-key rate limit, and the admin issue/revoke flow with the
show-once raw-key contract.
"""
import json
import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
import re
import shutil
import sys
from datetime import timedelta

import api_keys
from app import create_app
from extensions import db
from models import (
    ApiToken,
    AuditLog,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)

app = create_app()

FAILURES = []


def expect(condition, label):
    """Record one granular pass/fail check."""
    if condition:
        print(f"   ok: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}")


def get_csrf(html):
    """Pull the CSRF token out of a rendered form page."""
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    """POST the sign-in form."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


shutil.rmtree("instance", ignore_errors=True)

with app.app_context():
    admin = User(name="Ada Admin", email="api-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    ann = User(name="Ann Work", email="api-ann@brydl.ng",
               role="annotator")
    ann.set_password("ann-pass-1")
    db.session.add_all([admin, ann])
    db.session.flush()
    admin_id, ann_id = admin.id, ann.id
    mine = Project(name="Client X delivery", client_name="Lab X",
                   task_type="translation", pay_per_task=0.20,
                   instructions="i", created_by=admin_id)
    other = Project(name="Client Y delivery", client_name="Lab Y",
                    task_type="translation", pay_per_task=0.20,
                    instructions="i", created_by=admin_id)
    paused = Project(name="Paused delivery", client_name="Lab X",
                     task_type="translation", pay_per_task=0.20,
                     instructions="i", status="paused",
                     created_by=admin_id)
    db.session.add_all([mine, other, paused])
    db.session.flush()
    mine_id, other_id, paused_id = mine.id, other.id, paused.id
    for i in range(3):
        t = Task(input_data=f"source text {i}",
                 project_id=mine_id, status="approved",
                 assigned_to=ann_id)
        db.session.add(t)
        db.session.flush()
        db.session.add(Submission(
            task_id=t.id, annotator_id=ann_id,
            output_data=json.dumps(
                {"kind": "translation", "text": f"target {i}"}),
            status="approved", qa_score=4.0,
            submitted_at=utc_now() - timedelta(days=1),
            reviewed_at=utc_now()))
    db.session.add(Task(input_data="pending work",
                        project_id=mine_id, status="pending"))
    raw_key = api_keys.generate_key()
    revoked_raw = api_keys.generate_key()
    paused_raw = api_keys.generate_key()
    probe_raw = api_keys.generate_key()
    db.session.add(ApiToken(
        project_id=mine_id, label="CI pipeline",
        key_hash=api_keys.hash_key(raw_key),
        key_prefix=api_keys.display_prefix(raw_key),
        created_by=admin_id))
    db.session.add(ApiToken(
        project_id=paused_id, label="paused project key",
        key_hash=api_keys.hash_key(paused_raw),
        key_prefix=api_keys.display_prefix(paused_raw),
        created_by=admin_id))
    db.session.add(ApiToken(
        project_id=mine_id, label="rate probe",
        key_hash=api_keys.hash_key(probe_raw),
        key_prefix=api_keys.display_prefix(probe_raw),
        created_by=admin_id))
    db.session.add(ApiToken(
        project_id=mine_id, label="old key",
        key_hash=api_keys.hash_key(revoked_raw),
        key_prefix=api_keys.display_prefix(revoked_raw),
        created_by=admin_id,
        revoked_at=utc_now()))
    db.session.commit()

H = {"Authorization": f"Bearer {raw_key}"}
H_REVOKED = {"Authorization": f"Bearer {revoked_raw}"}
H_PAUSED = {"Authorization": f"Bearer {paused_raw}"}
H_PROBE = {"Authorization": f"Bearer {probe_raw}"}
c = app.test_client()

# ---- Section 1: auth ----
print("== Section 1: Bearer auth ==")
expect(c.get("/api/v1/ping").status_code == 200,
       "ping is open for uptime checks")
r = c.get("/api/v1/me")
expect(r.status_code == 401
       and r.headers.get("WWW-Authenticate") == "Bearer",
       "missing key: 401 with WWW-Authenticate")
expect(c.get("/api/v1/me", headers={
    "Authorization": "Bearer brydl_not_a_key"}).status_code == 401,
    "unknown key: 401")
expect(c.get("/api/v1/me", headers=H_REVOKED).status_code == 401,
       "revoked key: 401")
# A signed-in browser session must NEVER satisfy the API.
login(c, "api-admin@brydl.ng", "admin-pass-1")
expect(c.get("/api/v1/me").status_code == 401,
       "session cookie without Bearer: still 401")

# ---- Section 2: identity + scoping ----
print("== Section 2: identity + scoping ==")
r = c.get("/api/v1/me", headers=H)
body = r.get_json()
expect(r.status_code == 200
       and body["project"]["name"] == "Client X delivery"
       and body["key"].startswith("brydl_")
       and "…" in body["key"],
       "/me identifies key + project")
expect(c.get(f"/api/v1/projects/{other_id}", headers=H
             ).status_code == 404
       and c.get("/api/v1/projects/999999", headers=H
                 ).status_code == 404,
       "foreign/unknown projects are uniform 404s")
expect(c.post(f"/api/v1/projects/{other_id}/tasks", headers=H,
              json={"tasks": [{"input_data": "x"}]}
              ).status_code == 404,
       "foreign-project submission 404s")

# ---- Section 3: status + results ----
print("== Section 3: status + results ==")
r = c.get(f"/api/v1/projects/{mine_id}", headers=H)
body = r.get_json()
expect(r.status_code == 200 and body["status_counts"]["pending"] == 1
       and body["progress"]["approved"] == 3,
       "progress summary matches the portal math")
r = c.get(f"/api/v1/projects/{mine_id}/results?limit=2&offset=1",
          headers=H)
rows = r.get_json()["rows"]
serialized = json.dumps(rows)
expect(r.status_code == 200 and len(rows) == 2
       and rows[0]["task_id"] > 1,
       "results pagination honours limit/offset")
expect(all(row["qa_score"] == 4.0 for row in rows),
       "rows carry the QA score")
expect("annotator" not in serialized
       and "expected_output" not in serialized
       and "qa_notes" not in serialized
       and "reviewer" not in serialized,
       "no identity, gold or reviewer notes in the payload")
expect(rows[0]["output"]["text"].startswith("target"),
       "output documents ride through")
expect(c.get(f"/api/v1/projects/{mine_id}/results?limit=zebra",
             headers=H).status_code == 400,
       "non-integer pagination is 400")
expect(c.get(f"/api/v1/projects/{mine_id}/results?limit=9999",
             headers=H).status_code == 200,
       "over-large limit is capped, not an error")

# ---- Section 4: task submission ----
print("== Section 4: task submission ==")
r = c.post(f"/api/v1/projects/{mine_id}/tasks", headers=H, json={
    "tasks": [{"input_data": "  fresh source  ",
               "expected_output": "gold"}]})
expect(r.status_code == 201 and r.get_json()["created"] == 1,
       "batch accepted (201)")
with app.app_context():
    fresh = db.session.scalar(
        db.select(Task).where(
            Task.project_id == mine_id,
            Task.status == "pending",
            Task.input_data == "fresh source",
        )
    )
    expect(fresh is not None
           and fresh.expected_output == "gold",
           "row stored trimmed with its gold marker")
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "api.tasks.submit")) is not None,
        "api.tasks.submit audited (counts only)")
expect(c.post(f"/api/v1/projects/{mine_id}/tasks", headers=H,
              json={"tasks": []}).status_code == 400,
       "empty batch 400")
expect(c.post(f"/api/v1/projects/{mine_id}/tasks", headers=H,
              json={"tasks": [{"input_data": " "}]}
              ).status_code == 400,
       "blank input_data 400")
expect(c.post(f"/api/v1/projects/{mine_id}/tasks", headers=H,
              json={"tasks": [{"input_data": "x" * 10_501}]}
              ).status_code == 400,
       "overlong input_data 400")
expect(c.post(f"/api/v1/projects/{mine_id}/tasks", headers=H,
              json={"tasks": [{"input_data": "x"}]
                    * (501)}).status_code == 413,
       "batch over UPLOAD_BATCH_LIMIT 413")
expect(c.post(f"/api/v1/projects/{paused_id}/tasks", headers=H_PAUSED,
              json={"tasks": [{"input_data": "x"}]}
              ).status_code == 403,
       "paused project refuses work (403)")

# ---- Section 5: rate limit ----
print("== Section 5: rate limit ==")
app.config["API_RATE_LIMIT_PER_MINUTE"] = 2
codes = [
    c.get(f"/api/v1/projects/{mine_id}", headers=H_PROBE).status_code
    for _ in range(3)
]
app.config["API_RATE_LIMIT_PER_MINUTE"] = 60
expect(codes == [200, 200, 429],
       f"third call inside the window is 429 ({codes})")

# ---- Section 6: admin key lifecycle ----
print("== Section 6: admin key lifecycle ==")
r = c.post(f"/admin/projects/{mine_id}/api-keys", data={})
expect(r.status_code == 302,
       "admin key issue without CSRF redirects")
page = c.get(f"/admin/projects/{mine_id}/export").get_data(
    as_text=True)
r = c.post(f"/admin/projects/{mine_id}/api-keys", data={
    "label": "Nightly pull", "csrf_token": get_csrf(page)},
    follow_redirects=True)
new_html = r.get_data(as_text=True)
m = re.search(r"(brydl_[A-Za-z0-9_\-]{40,})", new_html)
expect(m is not None, "raw key flashed exactly once at issue")
raw_new = m.group(1) if m else ""
page = c.get(f"/admin/projects/{mine_id}/export").get_data(
    as_text=True)
expect(raw_new and raw_new not in page
       and api_keys.display_prefix(raw_new) in page,
       "raw key never rendered again; prefix only")
with app.app_context():
    issued = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "apikey.issue"))
    expect(issued is not None
           and raw_new not in json.dumps(issued.detail or {}),
           "audit records the prefix, never the raw key")
r = c.get("/api/v1/me", headers={
    "Authorization": f"Bearer {raw_new}"})
expect(r.status_code == 200, "issued key authenticates immediately")
page = c.get(f"/admin/projects/{mine_id}/export").get_data(
    as_text=True)
with app.app_context():
    new_token = db.session.scalar(db.select(ApiToken).where(
        ApiToken.label == "Nightly pull"))
r = c.post(f"/admin/api-keys/{new_token.id}/revoke", data={
    "csrf_token": get_csrf(page)}, follow_redirects=True)
expect("revoked" in r.get_data(as_text=True),
       "revoke flashes confirmation")
expect(c.get("/api/v1/me", headers={
    "Authorization": f"Bearer {raw_new}"}).status_code == 401,
    "revoked key dies instantly")
with app.app_context():
    expect(db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "apikey.revoke")) is not None,
        "apikey.revoke audited")

# ---- Section 7: annotator wall ----
print("== Section 7: role wall ==")
ann_c = app.test_client()
login(ann_c, "api-ann@brydl.ng", "ann-pass-1")
r = ann_c.post(f"/admin/projects/{mine_id}/api-keys", data={
    "label": "sneaky", "csrf_token": get_csrf(
        ann_c.get("/login").get_data(as_text=True))})
expect(r.status_code == 403, "annotator cannot issue API keys")
expect(ann_c.get("/api/v1/projects/999999").status_code == 401,
       "annotator session still 401s the API (no session path)")

print()
if FAILURES:
    print(f"API TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL API TESTS PASSED")
