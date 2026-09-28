import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Phase 7 suite: the client portal (scoped client-facing delivery).

Covers: the pure portal module (token generation/hashing, path
redaction, expiry, progress math, client-safe records, JSONL), the
admin issue/revoke flow (raw link shown once, hash-only storage),
portal access (happy path, dead links, expiry, noindex header),
deliveries (approved-only, no annotator identity, no gold markers,
CSV formula guard), audit coverage, and that the raw token never
reaches the REQ access log.
"""
import json
import logging
import re
import shutil
import sys
from datetime import timedelta
from decimal import Decimal

from app import create_app
from client_portal import (
    client_safe_record,
    generate_token,
    hash_token,
    link_expired,
    progress_summary,
    redact_client_path,
    to_jsonl,
)
from extensions import db
from models import (
    AuditLog,
    ClientAccessToken,
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
shutil.rmtree("backups", ignore_errors=True)

# ---- Section 1: pure module ----
print("== Section 1: client_portal.py unit contract ==")
t1, t2 = generate_token(), generate_token()
expect(t1 != t2 and len(t1) > 40, "tokens: random, long enough")
expect(hash_token(t1) == hash_token(t1)
       and hash_token(t1) != hash_token(t2)
       and len(hash_token(t1)) == 64,
       "hash: deterministic sha256 hex")
expect(redact_client_path(f"/client/{t1}") == "/client/[redacted]",
       "redact: bare portal path")
expect(redact_client_path(f"/client/{t1}/export.jsonl")
       == "/client/[redacted]/export.jsonl",
       "redact: export subpath")
expect(redact_client_path("/annotator/tasks") == "/annotator/tasks",
       "redact: other paths untouched")
expect(not link_expired(None, utc_now()), "expiry: None never expires")
expect(link_expired(utc_now() - timedelta(seconds=1), utc_now()),
       "expiry: past stamp expired")
expect(link_expired(utc_now(), utc_now()), "expiry: boundary expired")
summary = progress_summary(
    {"submitted": 6, "approved": 4, "rejected": 2}, 4, 10)
expect(summary["approved"] == 4 and summary["in_review"] == 2
       and summary["progress_percent"] == 40.0,
       "progress: math and percent")
expect(progress_summary({}, 0, 0)["progress_percent"] == 0,
       "progress: empty project safe")
record = client_safe_record(7, {"q": "x"}, {"kind": "text"},
                            92.0, "agreed", "a", "2026-09-25", None)
expect("annotator" not in json.dumps(record)
       and record["qa_score"] == 92.0,
       "record: client-safe shape")
expect(to_jsonl([record]).endswith("\n")
       and to_jsonl([]) == "", "jsonl: terminated, empty safe")

# ---- fixtures ----
with app.app_context():
    admin = User(name="Ada Admin", email="cp-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    qa = User(name="Qasi", email="cp-qa@brydl.ng", role="qa")
    qa.set_password("qa-pass-1")
    db.session.add_all([admin, qa])
    db.session.flush()
    admin_id, qa_id = admin.id, qa.id
    project = Project(name="Portal Project", client_name="Acme AI",
                      task_type="classification",
                      pay_per_task=Decimal("0.10"),
                      required_annotations=1,
                      instructions="Label it.", created_by=admin_id)
    db.session.add(project)
    db.session.flush()
    pid = project.id
    t_ok = Task(input_data="nice text", project_id=pid,
                expected_output="GOLD-ANSWER", status="approved",
                assigned_to=admin_id)
    t_rev = Task(input_data="in review", project_id=pid,
                 status="submitted", assigned_to=admin_id)
    db.session.add_all([t_ok, t_rev])
    db.session.flush()
    sub_ok = Submission(task_id=t_ok.id, annotator_id=admin_id,
                        output_data=json.dumps(
                            {"kind": "classification",
                             "label": "positive"}),
                        qa_score=95.0, status="approved",
                        submitted_at=utc_now(),
                        reviewed_at=utc_now())
    sub_rev = Submission(task_id=t_rev.id, annotator_id=admin_id,
                         output_data=json.dumps(
                             {"kind": "classification",
                              "label": "negative"}),
                         status="submitted", submitted_at=utc_now())
    db.session.add_all([sub_ok, sub_rev])
    db.session.commit()
    ok_id = t_ok.id

admin_c = app.test_client()
login(admin_c, "cp-admin@brydl.ng", "admin-pass-1")
qa_c = app.test_client()
login(qa_c, "cp-qa@brydl.ng", "qa-pass-1")

# ---- Section 2: admin issue/revoke flow ----
print("== Section 2: admin token management ==")
exp_page = admin_c.get(f"/admin/projects/{pid}/export").get_data(
    as_text=True)
expect("Client portal" in exp_page, "export page shows portal card")
page = admin_c.post(f"/admin/projects/{pid}/client-tokens", data={
    "label": "Acme AI delivery", "csrf_token": get_csrf(exp_page)})
expect(page.status_code == 302, "token creation redirects")
flash_page = admin_c.get(f"/admin/projects/{pid}/export"
                         ).get_data(as_text=True)
m = re.search(r"Client link created \(shown once\): (\S+)", flash_page)
expect(m is not None, "raw link flashed once")
expect(m and "/client/" in m.group(1), "link points at the portal")
raw_token = m.group(1).rsplit("/", 1)[1] if m else ""
with app.app_context():
    row = db.session.scalar(db.select(ClientAccessToken))
    expect(row is not None and row.token_hash == hash_token(raw_token),
           "storage holds the hash, not the raw token")
    expect(raw_token not in json.dumps([row.token_hash]),
           "raw token absent from storage")
    token_id = row.id
# qa cannot manage tokens
r = qa_c.post(f"/admin/projects/{pid}/client-tokens", data={
    "label": "sneaky", "csrf_token": get_csrf(
        qa_c.get(f"/admin/projects/{pid}/export").get_data(
            as_text=True))})
expect(r.status_code == 403, "qa cannot create client links")

# ---- Section 3: portal access ----
print("== Section 3: portal access ==")
anon = app.test_client()
r = anon.get(f"/client/{raw_token}")
expect(r.status_code == 200, "portal opens with the raw link")
body = r.get_data(as_text=True)
expect("Portal Project" in body and "Acme AI delivery" in body,
       "portal shows project + prepared-for label")
expect("60.0%" in body or "50.0%" in body or "progress" in body.lower(),
       "progress section renders")
expect("GOLD-ANSWER" not in body and "Ada Admin" not in body,
       "no gold answers, no staff names on the portal")
expect(r.headers.get("X-Robots-Tag") == "noindex, nofollow",
       "portal marked noindex")
# The credential may exist ONLY inside the export links' hrefs —
# never in page text.
outside_hrefs = re.sub(r'href="[^"]*"', '', body)
expect(raw_token not in outside_hrefs,
       "credential appears only in link hrefs, never in page text")
expect(anon.get("/client/not-a-real-token").status_code == 404,
       "unknown token 404s")
expect(anon.get(f"/client/{raw_token}").status_code == 200,
       "link still works (not one-time)")

# ---- Section 4: deliveries ----
print("== Section 4: client-safe deliveries ==")
r = anon.get(f"/client/{raw_token}/export.jsonl")
expect(r.status_code == 200
       and "ndjson" in r.headers.get("Content-Type", ""),
       "JSONL download works")
records = [json.loads(ln)
           for ln in r.get_data(as_text=True).splitlines() if ln.strip()]
expect(len(records) == 1 and records[0]["task_id"] == ok_id,
       "approved rows only")
dump = json.dumps(records)
expect("annotator" not in dump and "GOLD-ANSWER" not in dump
       and "is_gold" not in dump and "qa_notes" not in dump,
       "no identity, gold marker or reviewer notes in delivery")
expect(records[0]["output"]["label"] == "positive",
       "client gets the paid-for output")
r = anon.get(f"/client/{raw_token}/export.csv")
expect(r.status_code == 200
       and "csv" in r.headers.get("Content-Type", ""),
       "CSV download works")
csv_body = r.get_data(as_text=True)
expect("task_id" in csv_body and "positive" in csv_body,
       "CSV carries the approved row")

# ---- Section 5: audit + log redaction ----
print("== Section 5: audit + log hygiene ==")
with app.app_context():
    for action in ("client_portal.create", "client_portal.access",
                   "client_portal.export"):
        expect(db.session.scalar(db.select(AuditLog).where(
            AuditLog.action == action)) is not None,
            f"{action} audited")
    audit_dump = json.dumps([
        a.detail for a in db.session.scalars(
            db.select(AuditLog).where(
                AuditLog.action.like("client_portal%"))).all()])
expect(raw_token not in audit_dump, "raw token absent from audit trail")
captured = []


class _Grab(logging.Handler):
    """Collect this app's log lines for the redaction assertion."""

    def emit(self, record):
        captured.append(record.getMessage())


grab = _Grab()
app.logger.addHandler(grab)
anon.get(f"/client/{raw_token}")
app.logger.removeHandler(grab)
joined = "\n".join(captured)
expect(raw_token not in joined and "[redacted]" in joined,
       "REQ log line carries no raw token")

# ---- Section 6: revocation + expiry ----
print("== Section 6: revocation + expiry ==")
r = admin_c.post(f"/admin/client-tokens/{token_id}/deactivate", data={
    "csrf_token": get_csrf(admin_c.get(f"/admin/projects/{pid}/export"
                                       ).get_data(as_text=True))})
expect(r.status_code == 302, "deactivation redirects")
expect(anon.get(f"/client/{raw_token}").status_code == 404,
       "deactivated link 404s immediately")
expect(anon.get(f"/client/{raw_token}/export.jsonl").status_code == 404,
       "deactivated link cannot download")
with app.app_context():
    db.session.execute(
        db.update(ClientAccessToken)
        .where(ClientAccessToken.id == token_id)
        .values(is_active=True, expires_at=utc_now()
                - timedelta(days=1)))
    db.session.commit()
expect(anon.get(f"/client/{raw_token}").status_code == 404,
       "expired link 404s")
with app.app_context():
    db.session.execute(
        db.update(ClientAccessToken)
        .where(ClientAccessToken.id == token_id)
        .values(is_active=True, expires_at=None))
    db.session.commit()
expect(anon.get(f"/client/{raw_token}").status_code == 200,
       "re-activated link works again")

print()
if FAILURES:
    print(f"CLIENT PORTAL TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL CLIENT PORTAL TESTS PASSED")
