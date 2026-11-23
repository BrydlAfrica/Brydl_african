"""Test Enterprise Scoping & Project Inquiry Pathway."""
import os
import shutil
import sys

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
os.chdir(ROOT)
shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import AuditLog  # noqa: E402
from sqlalchemy import select  # noqa: E402

app = create_app()
client = app.test_client()

print("== 1. Enterprise Inquiry Validation & Error Handling ==")
# Empty payload
r = client.post("/inquiry", json={})
assert r.status_code == 400
data = r.get_json()
assert data["status"] == "error"
print("   ok: empty payload rejected with 400")

# Missing email
r = client.post("/inquiry", json={
    "full_name": "Dr. Adaeze Okafor",
    "company_name": "African AI Labs",
    "project_scope": "Evaluation of 70B parameter Hausa LLM model."
})
assert r.status_code == 400
print("   ok: missing email rejected")

print("== 2. Valid Enterprise Inquiry AJAX Submission ==")
r = client.post("/inquiry", json={
    "full_name": "Dr. Babatunde Adeleke",
    "work_email": "babatunde@pan-african-ai.com",
    "company_name": "Pan-African AI Labs",
    "project_type": "Adversarial Red-Teaming (RedLab)",
    "target_languages": ["Hausa", "Yoruba", "Igbo"],
    "timeline": "Immediate (< 1 month)",
    "project_scope": "Need adversarial stress-testing against financial jailbreaks and USSD dialect exploits."
})
assert r.status_code == 200
data = r.get_json()
assert data["status"] == "success"
assert "Pan-African AI Labs" in data["message"] or "received" in data["message"]
print("   ok: valid enterprise inquiry submitted via AJAX returns 200 success")

print("== 3. Audit Log Verification ==")
with app.app_context():
    log = db.session.scalar(
        select(AuditLog).where(AuditLog.action == "inquiry.submitted").order_by(AuditLog.id.desc())
    )
    assert log is not None
    assert log.actor_name == "Dr. Babatunde Adeleke"
    assert "Pan-African AI Labs" in log.detail
    print("   ok: audit log record verified in database")

print("== 4. GET /inquiry & GET /contact redirects ==")
r_get = client.get("/inquiry")
assert r_get.status_code == 302
assert "#inquiry" in r_get.headers["Location"]
print("   ok: GET /inquiry redirects to #inquiry anchor")

r_contact = client.get("/contact")
assert r_contact.status_code == 302
assert "#inquiry" in r_contact.headers["Location"]
print("   ok: GET /contact redirects to #inquiry anchor")

print("ALL ENTERPRISE INQUIRY TESTS PASSED")
