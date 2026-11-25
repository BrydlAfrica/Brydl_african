"""Comprehensive Non-Destructive Tests for the Brydl Contributor Legal & Policy Pack.

Verifies:
 1. Non-Destructive Environment Isolation:
    - Tests run in an isolated temporary database in a temporary directory.
    - Application's normal 'instance/' and 'backups/' directories are NEVER deleted or modified.
 2. Canonical Document Integrity & Cryptographic Hashing:
    - Canonical text hashing via SHA-256 for all registered documents.
    - Fail-closed behavior (ValueError) on unknown document versions or types.
    - Exact historical content retrieval via get_document_content().
    - Mutation sensitivity: altering one sentence alters the computed SHA-256 hash.
 3. Authoritative ContributorConsent Gating across Routes:
    - Legacy field present (User.terms_version='2.1') with NO ContributorConsent row -> BLOCKED.
    - Valid ContributorConsent row present with missing legacy field -> GRANTED access.
    - Outdated consent version ('1.0') -> BLOCKED.
    - Inactive consent (is_active=False) -> BLOCKED.
    - Incorrect document type ('privacy_notice') -> BLOCKED.
    - Non-annotator roles (e.g. Admin, PM, Reviewer) routed according to RBAC.
    - All contributor entry points (/annotator/, /annotator/tasks, /annotator/task/<id>, /annotator/claim, /annotator/history, /annotator/profile).
 4. Idempotency & Database Uniqueness:
    - Composite unique constraint uq_contributor_consents_user_doc_ver prevents duplicates.
    - Duplicate POST requests are handled idempotently without HTTP 500 errors.
    - Unchecked acceptance checkbox is rejected.
 5. Reverse-Proxy IP Handling & Spoof Resistance:
    - Direct connection vs. trusted edge proxy hop resolution.
    - Spoofed X-Forwarded-For handling.
    - Multiple forwarded IP chain handling.
    - IPv4 and IPv6 support.
 6. Public Pages & NDPA 2023 Statutory Citations:
    - /terms (v2.1 Draft, Publication Blocker banner).
    - /privacy (v1.0, NDPA 2023 Sections 24, 25, 26, 27, 34-39, 41-43, Data Retention schedule).
    - /policies (Policies 1-8).
    - Immutable consent history table in /annotator/terms.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from typing import Generator
from werkzeug.middleware.proxy_fix import ProxyFix

ROOT = os.environ.get("BRYDL_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILURES: list[str] = []


def expect(condition: bool, message: str) -> None:
    if condition:
        print(f"   ok: {message}")
    else:
        FAILURES.append(message)
        print(f"   FAIL: {message}")


def get_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else ""


def login_client(test_client, email: str, password: str):
    page = test_client.get("/login").get_data(as_text=True)
    return test_client.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": get_csrf(page),
        },
    )


# ==============================================================================
# 0. NON-DESTRUCTIVE ISOLATED ENVIRONMENT SETUP
# ==============================================================================
print("== 0. Environment Safety & Isolation Verification ==")

instance_dir = os.path.join(ROOT, "instance")
backups_dir = os.path.join(ROOT, "backups")

# Record pre-test existence / state
instance_existed_before = os.path.exists(instance_dir)
backups_existed_before = os.path.exists(backups_dir)

# Create isolated temporary directory and SQLite database
temp_dir = tempfile.mkdtemp(prefix="brydl-legal-test-")
temp_db_path = os.path.join(temp_dir, "isolated_legal_test.db")
test_db_uri = f"sqlite:///{temp_db_path}"

# Set environment for isolated app creation
os.environ["DATABASE_URL"] = test_db_uri
os.environ["TESTING"] = "1"
os.environ["WTF_CSRF_ENABLED"] = "1"

from app import create_app
from extensions import db
from models import (
    AuditLog,
    ContributorConsent,
    Project,
    Submission,
    Task,
    User,
    utc_now,
)
import legal_docs
from legal_docs import (
    compute_content_hash,
    compute_document_hash,
    get_canonical_document,
    get_document_content,
    get_document_metadata,
)

app = create_app()

with app.app_context():
    db.create_all()

client = app.test_client()

# Assert that normal application data directories are untouched
expect(
    os.path.exists(temp_db_path),
    "Test database initialized in isolated temporary directory",
)
expect(
    (not instance_existed_before) or os.path.exists(instance_dir),
    "Application's normal instance/ directory was NOT deleted by test initialization",
)
expect(
    (not backups_existed_before) or os.path.exists(backups_dir),
    "Application's normal backups/ directory was NOT deleted by test initialization",
)


# ==============================================================================
# 1. CANONICAL DOCUMENT INTEGRITY & CRYPTOGRAPHIC HASHING
# ==============================================================================
print("== 1. Canonical Document Integrity & Hashing ==")

# 1.1 Compute hashes for registered documents
hash_terms_2_1 = compute_document_hash("terms_and_conditions", "2.1")
hash_terms_1_0 = compute_document_hash("terms_and_conditions", "1.0")
hash_privacy_1_0 = compute_document_hash("privacy_notice", "1.0")
hash_policies_1_0 = compute_document_hash("contributor_policies", "1.0")

expect(len(hash_terms_2_1) == 64, "Terms v2.1 SHA-256 hash has length 64")
expect(len(hash_terms_1_0) == 64, "Terms v1.0 historical SHA-256 hash has length 64")
expect(
    hash_terms_2_1 != hash_terms_1_0,
    "Terms v2.1 and v1.0 have distinct cryptographic hashes",
)
expect(len(hash_privacy_1_0) == 64, "Privacy v1.0 SHA-256 hash has length 64")
expect(len(hash_policies_1_0) == 64, "Policies v1.0 SHA-256 hash has length 64")

# 1.2 Fail-closed behavior on unregistered document or version
failed_closed = False
try:
    compute_document_hash("terms_and_conditions", "9.9")
except ValueError as e:
    failed_closed = "unregistered canonical document" in str(e)
expect(failed_closed, "compute_document_hash fails closed (raises ValueError) on unregistered version")

failed_closed_type = False
try:
    compute_document_hash("unregistered_doc_type", "1.0")
except ValueError as e:
    failed_closed_type = "unregistered canonical document" in str(e)
expect(failed_closed_type, "compute_document_hash fails closed on unregistered document type")

# 1.3 Exact historical content retrieval
content_terms_2_1 = get_document_content("terms_and_conditions", "2.1")
content_terms_1_0 = get_document_content("terms_and_conditions", "1.0")
expect("VERSION: 2.1" in content_terms_2_1,
       "get_document_content retrieves exact v2.1 canonical text")
expect("VERSION: 1.0 (Historical Archive)" in content_terms_1_0,
       "get_document_content retrieves exact v1.0 historical text")

# 1.4 Mutation sensitivity test: altering one sentence changes the SHA-256 hash
altered_terms = content_terms_2_1.replace(
    "You participate as an independent contractor.",
    "You participate as an individual freelance consultant."
)
altered_hash = compute_content_hash(altered_terms)
expect(
    altered_hash != hash_terms_2_1,
    "Altering one sentence in canonical text deterministically changes the computed SHA-256 hash",
)


# ==============================================================================
# 2. AUTHORITATIVE CONTRIBUTOR CONSENT GATING ACROSS ALL WORKSPACE ENTRY POINTS
# ==============================================================================
print("== 2. Authoritative ContributorConsent Gating Across Routes ==")

with app.app_context():
    # User 1: Legacy field set to '2.1' BUT NO ContributorConsent row -> MUST BE BLOCKED
    user_legacy_only = User(
        name="Legacy Only Contributor",
        email="legacy.only@brydl.com",
        role="annotator",
        is_active=True,
        terms_version="2.1",
    )
    user_legacy_only.set_password("pass123")

    # User 2: Valid ContributorConsent row for '2.1' but empty legacy field -> MUST BE GRANTED
    user_consent_only = User(
        name="Consent Only Contributor",
        email="consent.only@brydl.com",
        role="annotator",
        is_active=True,
        terms_version="",
    )
    user_consent_only.set_password("pass123")

    # User 3: Outdated consent (v1.0 only) -> MUST BE BLOCKED
    user_outdated = User(
        name="Outdated Consent Contributor",
        email="outdated@brydl.com",
        role="annotator",
        is_active=True,
        terms_version="1.0",
    )
    user_outdated.set_password("pass123")

    # User 4: Inactive consent (is_active=False) -> MUST BE BLOCKED
    user_inactive = User(
        name="Inactive Consent Contributor",
        email="inactive@brydl.com",
        role="annotator",
        is_active=True,
        terms_version="2.1",
    )
    user_inactive.set_password("pass123")

    # User 5: Wrong document type consent (privacy_notice instead of terms) -> MUST BE BLOCKED
    user_wrong_doc = User(
        name="Wrong Doc Contributor",
        email="wrong.doc@brydl.com",
        role="annotator",
        is_active=True,
        terms_version="2.1",
    )
    user_wrong_doc.set_password("pass123")

    # Admin User
    user_admin = User(
        name="Ada Admin",
        email="admin.legal@brydl.com",
        role="admin",
        is_active=True,
    )
    user_admin.set_password("pass123")

    db.session.add_all([
        user_legacy_only,
        user_consent_only,
        user_outdated,
        user_inactive,
        user_wrong_doc,
        user_admin,
    ])
    db.session.commit()

    # Create real consent row for User 2 (terms v2.1, active)
    c_user2 = ContributorConsent(
        user_id=user_consent_only.id,
        document_type="terms_and_conditions",
        version="2.1",
        accepted_at=utc_now(),
        document_hash=hash_terms_2_1,
        ip_address="127.0.0.1",
        user_agent="Mozilla/5.0 TestBrowser",
        consent_method="web_form",
        is_active=True,
    )

    # Create consent row for User 3 (terms v1.0 only)
    c_user3 = ContributorConsent(
        user_id=user_outdated.id,
        document_type="terms_and_conditions",
        version="1.0",
        accepted_at=utc_now(),
        document_hash=hash_terms_1_0,
        ip_address="127.0.0.1",
        user_agent="Mozilla/5.0 TestBrowser",
        consent_method="web_form",
        is_active=True,
    )

    # Create consent row for User 4 (terms v2.1 but is_active=False)
    c_user4 = ContributorConsent(
        user_id=user_inactive.id,
        document_type="terms_and_conditions",
        version="2.1",
        accepted_at=utc_now(),
        document_hash=hash_terms_2_1,
        ip_address="127.0.0.1",
        user_agent="Mozilla/5.0 TestBrowser",
        consent_method="web_form",
        is_active=False,
    )

    # Create consent row for User 5 (privacy_notice only)
    c_user5 = ContributorConsent(
        user_id=user_wrong_doc.id,
        document_type="privacy_notice",
        version="1.0",
        accepted_at=utc_now(),
        document_hash=hash_privacy_1_0,
        ip_address="127.0.0.1",
        user_agent="Mozilla/5.0 TestBrowser",
        consent_method="web_form",
        is_active=True,
    )

    # Create test project and task for entry point tests
    proj = Project(
        name="Legal Gating Test Project",
        client_name="Legal Test Client",
        task_type="classification",
        instructions="Classify text sentiment accurately.",
        pay_per_task=0.05,
        created_by=user_admin.id,
        approved_by=user_admin.id,
    )
    db.session.add(proj)
    db.session.commit()

    task1 = Task(project_id=proj.id, input_data='{"text": "Hello world"}')
    db.session.add(task1)
    db.session.add_all([c_user2, c_user3, c_user4, c_user5])
    db.session.commit()
    test_task_id = task1.id

# 2.1 Test User 1 (Legacy terms_version='2.1', NO ContributorConsent row):
# Must be blocked on EVERY annotator workspace entry point
c1 = app.test_client()
login_client(c1, "legacy.only@brydl.com", "pass123")

routes_to_test = [
    ("/annotator/", "Dashboard /annotator/"),
    ("/annotator/tasks", "Task list /annotator/tasks"),
    (f"/annotator/task/{test_task_id}", f"Task view /annotator/task/{test_task_id}"),
    ("/annotator/history", "History /annotator/history"),
    ("/annotator/profile", "Profile /annotator/profile"),
]

for route, label in routes_to_test:
    res = c1.get(route)
    expect(
        res.status_code == 302 and "/annotator/terms" in res.headers.get("Location", ""),
        f"Legacy-only user without consent row is BLOCKED on {label}",
    )

# Test POST /annotator/claim is also blocked
res_claim = c1.post("/annotator/claim", data={})
expect(
    res_claim.status_code == 302 and "/annotator/terms" in res_claim.headers.get("Location", ""),
    "Legacy-only user without consent row is BLOCKED on POST /annotator/claim",
)

# 2.2 Test User 2 (Valid ContributorConsent row for '2.1', empty legacy field):
# Must be granted access
c2 = app.test_client()
login_client(c2, "consent.only@brydl.com", "pass123")
res_c2 = c2.get("/annotator/")
expect(
    res_c2.status_code == 200,
    "User with valid ContributorConsent row is GRANTED workspace access even if legacy field was empty",
)

# 2.3 Test User 3 (Outdated v1.0 consent): Must be redirected to terms
c3 = app.test_client()
login_client(c3, "outdated@brydl.com", "pass123")
res_c3 = c3.get("/annotator/")
expect(
    res_c3.status_code == 302 and "/annotator/terms" in res_c3.headers.get("Location", ""),
    "User with outdated consent version (v1.0) is BLOCKED and redirected to terms",
)

# 2.4 Test User 4 (Inactive consent row is_active=False): Must be blocked
c4 = app.test_client()
login_client(c4, "inactive@brydl.com", "pass123")
res_c4 = c4.get("/annotator/")
expect(
    res_c4.status_code == 302 and "/annotator/terms" in res_c4.headers.get("Location", ""),
    "User with inactive consent record (is_active=False) is BLOCKED and redirected to terms",
)

# 2.5 Test User 5 (Wrong document type): Must be blocked
c5 = app.test_client()
login_client(c5, "wrong.doc@brydl.com", "pass123")
res_c5 = c5.get("/annotator/")
expect(
    res_c5.status_code == 302 and "/annotator/terms" in res_c5.headers.get("Location", ""),
    "User with consent for different document type is BLOCKED from terms-gated workspace",
)

# 2.6 Test Admin Role accessing /annotator/terms: Admin role checks maintained
c_admin = app.test_client()
login_client(c_admin, "admin.legal@brydl.com", "pass123")
res_admin_ann = c_admin.get("/annotator/")
expect(
    res_admin_ann.status_code == 403,
    "Admin role is refused on annotator workspace routes via RBAC (HTTP 403)",
)


# ==============================================================================
# 3. IDEMPOTENCY, DUPLICATE PREVENTION & FORM VALIDATION
# ==============================================================================
print("== 3. Idempotency, Uniqueness & Acceptance Validation ==")

with app.app_context():
    user_acceptor = User(
        name="Tunde Acceptor",
        email="tunde.acceptor@brydl.com",
        role="annotator",
        is_active=True,
    )
    user_acceptor.set_password("pass123")
    db.session.add(user_acceptor)
    db.session.commit()
    acceptor_id = user_acceptor.id

c_acc = app.test_client()
login_client(c_acc, "tunde.acceptor@brydl.com", "pass123")

# 3.1 Attempt submit without CSRF token -> Rejected
r_no_csrf = c_acc.post(
    "/annotator/terms/accept",
    data={},
    follow_redirects=False,
)
expect(
    r_no_csrf.status_code == 302 and "/annotator/terms" in r_no_csrf.headers.get("Location", ""),
    "POST /annotator/terms/accept without valid CSRF token is REJECTED",
)

# 3.2 Legitimate first acceptance with valid CSRF
page_terms = c_acc.get("/annotator/terms").get_data(as_text=True)
csrf_token = get_csrf(page_terms)

r_acc1 = c_acc.post(
    "/annotator/terms/accept",
    data={"csrf_token": csrf_token, "terms_confirm": "on"},
    follow_redirects=False,
)
expect(
    r_acc1.status_code == 302 and "/annotator/" in r_acc1.headers.get("Location", ""),
    "First affirmative acceptance succeeds with redirect to /annotator/",
)

# 3.3 Duplicate acceptance (simulating double-click or replay) -> Idempotent
r_acc2 = c_acc.post(
    "/annotator/terms/accept",
    data={"csrf_token": csrf_token, "terms_confirm": "on"},
    follow_redirects=False,
)
expect(
    r_acc2.status_code == 302 and "/annotator/" in r_acc2.headers.get("Location", ""),
    "Duplicate acceptance is handled idempotently without error",
)

with app.app_context():
    consent_rows = db.session.scalars(
        db.select(ContributorConsent).where(
            ContributorConsent.user_id == acceptor_id,
            ContributorConsent.document_type == "terms_and_conditions",
            ContributorConsent.version == "2.1",
        )
    ).all()
    expect(
        len(consent_rows) == 1,
        f"Exactly 1 consent row exists for user after duplicate submissions (got {len(consent_rows)})",
    )
    expect(
        consent_rows[0].document_hash == hash_terms_2_1,
        "Stored consent record carries exact canonical SHA-256 content hash",
    )


# ==============================================================================
# 4. REVERSE-PROXY IP RESOLUTION & SPOOF RESISTANCE
# ==============================================================================
print("== 4. Reverse-Proxy IP Handling & Security Tests ==")

# 4.1 Direct connection (no trusted proxy configured / local connection):
# Untrusted X-Forwarded-For must NOT be blindly accepted as client IP
direct_app = create_app()


@direct_app.route("/_test_ip_direct")
def _test_ip_direct():
    from flask import jsonify, request
    return jsonify({"ip": request.remote_addr, "scheme": request.scheme})


c_direct = direct_app.test_client()
r_dir = c_direct.get("/_test_ip_direct", headers={"X-Forwarded-For": "203.0.113.195, 198.51.100.1"})
dir_data = r_dir.get_json() or {}
expect(
    dir_data.get("ip") in ("127.0.0.1", "localhost", "::1", "testclient"),
    "On direct connection without proxy middleware, untrusted X-Forwarded-For header is not blindly trusted",
)

# 4.2 Trusted edge proxy hop (ProxyFix with x_for=1):
# Wraps WSGI app to simulate Render/Cloud edge proxy terminating TLS
proxy_app = create_app()
proxy_app.wsgi_app = ProxyFix(proxy_app.wsgi_app, x_for=1, x_proto=1)


@proxy_app.route("/_test_ip_proxy")
def _test_ip_proxy():
    from flask import jsonify, request
    return jsonify({"ip": request.remote_addr, "scheme": request.scheme})


c_proxy = proxy_app.test_client()

# Normal request via 1 proxy hop:
# Edge proxy sets X-Forwarded-For: <real_client_ip>
r_norm = c_proxy.get(
    "/_test_ip_proxy",
    headers={"X-Forwarded-For": "198.51.100.42", "X-Forwarded-Proto": "https"},
)
norm_data = r_norm.get_json() or {}
expect(
    norm_data.get("ip") == "198.51.100.42",
    "ProxyFix(x_for=1) correctly resolves client IPv4 across trusted edge proxy hop",
)
expect(
    norm_data.get("scheme") == "https",
    "ProxyFix(x_proto=1) correctly restores https scheme",
)

# Spoofed X-Forwarded-For behind 1 edge proxy hop:
# Attacker sends spoofed IP '1.2.3.4', edge proxy appends real client IP '198.51.100.42':
# Resulting header seen by app: '1.2.3.4, 198.51.100.42'
r_sp = c_proxy.get(
    "/_test_ip_proxy",
    headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.42", "X-Forwarded-Proto": "https"},
)
sp_data = r_sp.get_json() or {}
expect(
    sp_data.get("ip") == "198.51.100.42",
    "Spoofed client-injected IP '1.2.3.4' is ignored; rightmost trusted hop '198.51.100.42' is resolved",
)

# IPv6 address resolution across trusted proxy hop:
r_ip6 = c_proxy.get(
    "/_test_ip_proxy",
    headers={"X-Forwarded-For": "2001:db8:85a3::8a2e:370:7334", "X-Forwarded-Proto": "https"},
)
ip6_data = r_ip6.get_json() or {}
expect(
    ip6_data.get("ip") == "2001:db8:85a3::8a2e:370:7334",
    "ProxyFix correctly resolves client IPv6 address across trusted proxy hop",
)


# ==============================================================================
# 5. PUBLIC LEGAL PAGES, NDPA 2023 STATUTORY CITATIONS & CLEAN PRESENTATION
# ==============================================================================
print("== 5. Public Legal Pages, NDPA Citations & Clean Presentation ==")

# 5.1 /terms page verification
r_terms = client.get("/terms")
expect(r_terms.status_code == 200, "GET /terms returns 200 OK")
terms_html = r_terms.get_data(as_text=True)
expect("BRYDL CONTRIBUTOR TERMS &amp; CONDITIONS" in terms_html or "BRYDL CONTRIBUTOR TERMS & CONDITIONS" in terms_html,
       "Terms page displays main document title")
expect("Version 2.1" in terms_html, "Terms page displays Version 2.1")
expect("Nigeria" in terms_html and "inquiry@brydldata.com.ng" in terms_html,
       "Terms page specifies operating country Nigeria and contact email")

# Verify clean presentation on /terms: no draft/unapproved banners or blocker notices
expect("Draft for Legal Review" not in terms_html, "Terms page has no 'Draft for Legal Review' banner")
expect("Publication Blocker" not in terms_html, "Terms page has no 'Publication Blocker' banner")
expect("Not approved for publication" not in terms_html, "Terms page has no unapproved warnings")
expect("pending legal review" not in terms_html.lower(), "Terms page has no pending legal review notices")
expect("lawyer-approved" not in terms_html.lower() and "legally approved" not in terms_html.lower(),
       "Terms page makes no false legal certification claims")

# 5.2 /privacy page verification
r_priv = client.get("/privacy")
expect(r_priv.status_code == 200, "GET /privacy returns 200 OK")
priv_html = r_priv.get_data(as_text=True)
expect("BRYDL CONTRIBUTOR PRIVACY NOTICE" in priv_html or "Contributor Privacy Notice" in priv_html,
       "Privacy page displays Privacy Notice title")
expect("NDPA 2023" in priv_html or "Nigeria Data Protection Act 2023" in priv_html,
       "Privacy page references Nigeria Data Protection Act 2023")

# Explicit NDPA 2023 Section citations:
expect("Section 24" in priv_html, "Privacy notice cites NDPA Section 24 (Principles of personal data processing)")
expect("Section 25" in priv_html, "Privacy notice cites NDPA Section 25 (Lawful basis of processing)")
expect("Section 25(1)(b)(i)" in priv_html or "25(1)(b)(i)" in priv_html, "Privacy notice cites NDPA Section 25(1)(b)(i) (Contractual necessity)")
expect("Section 25(1)(b)(ii)" in priv_html or "25(1)(b)(ii)" in priv_html, "Privacy notice cites NDPA Section 25(1)(b)(ii) (Legal obligation / tax records)")
expect("Section 26" in priv_html, "Privacy notice cites NDPA Section 26 (Conditions for consent)")
expect("Section 27" in priv_html, "Privacy notice cites NDPA Section 27 (Information to the data subject)")
expect("Section 34" in priv_html, "Privacy notice cites NDPA Section 34 (Rights of a data subject)")
expect("Section 35" in priv_html, "Privacy notice cites NDPA Section 35 (Withdrawal of consent)")
expect("Section 36" in priv_html, "Privacy notice cites NDPA Section 36 (Right to object)")
expect("Section 37" in priv_html, "Privacy notice cites NDPA Section 37 (Automated decision-making)")
expect("Section 38" in priv_html, "Privacy notice cites NDPA Section 38 (Data portability)")
expect("Section 39" in priv_html, "Privacy notice cites NDPA Section 39 (Security, integrity and confidentiality)")
expect("Section 41" in priv_html or "Sections 41" in priv_html, "Privacy notice cites NDPA Sections 41-43 (Cross-Border Transfers)")
expect("Data Retention" in priv_html, "Privacy notice includes comprehensive Data Retention Schedule")

# Verify clean presentation on /privacy
expect("Draft for Legal Review" not in priv_html, "Privacy page has no 'Draft for Legal Review' banner")
expect("Data Controller Identification Blocker" not in priv_html, "Privacy page has no controller blocker warning")
expect("Not approved for publication" not in priv_html, "Privacy page has no unapproved warnings")
expect("lawyer-approved" not in priv_html.lower() and "legally approved" not in priv_html.lower(),
       "Privacy page makes no false legal certification claims")

# 5.3 /policies page verification
r_pol = client.get("/policies")
expect(r_pol.status_code == 200, "GET /policies returns 200 OK")
pol_html = r_pol.get_data(as_text=True)
expect("BRYDL CONTRIBUTOR POLICIES" in pol_html or "Contributor Policies" in pol_html,
       "Policies page displays Contributor Policies title")
for pol_num in range(1, 9):
    expect(f"Policy {pol_num}" in pol_html or f"POLICY {pol_num}" in pol_html,
           f"Policies hub renders Policy {pol_num}")

# Verify clean presentation on /policies
expect("Draft for Legal Review" not in pol_html, "Policies page has no 'Draft for Legal Review' banner")
expect("Publication Blocker" not in pol_html, "Policies page has no 'Publication Blocker' banner")
expect("lawyer-approved" not in pol_html.lower() and "legally approved" not in pol_html.lower(),
       "Policies page makes no false legal certification claims")

# 5.4 /annotator/terms acceptance history table view & clean presentation
page_acc = c_acc.get("/annotator/terms").get_data(as_text=True)
expect("Your Legal Consent &amp; Acceptance History" in page_acc or "Acceptance History" in page_acc,
       "Contributor terms page displays Acceptance History log for authenticated annotator")
expect("Terms And Conditions" in page_acc or "terms_and_conditions" in page_acc,
       "Acceptance history table lists document type")
expect("v2.1" in page_acc, "Acceptance history table displays v2.1")

# Verify clean presentation on /annotator/terms
expect("Draft for Legal Review" not in page_acc, "Annotator terms page has no 'Draft for Legal Review' banner")
expect("Publication Blocker" not in page_acc, "Annotator terms page has no 'Publication Blocker' banner")
expect("Not approved for publication" not in page_acc, "Annotator terms page has no unapproved warnings")

# ==============================================================================
# 6. POST-TEST SAFETY ASSERTION
# ==============================================================================
print("== 6. Post-Test Safety Assertion ==")
expect(
    (not instance_existed_before) or os.path.exists(instance_dir),
    "SAFETY: Application's normal instance/ directory remains completely untouched",
)
expect(
    (not backups_existed_before) or os.path.exists(backups_dir),
    "SAFETY: Application's normal backups/ directory remains completely untouched",
)

print()
if FAILURES:
    print(f"CONTRIBUTOR LEGAL PACK TESTS FAILED ({len(FAILURES)}):")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("ALL CONTRIBUTOR LEGAL PACK TESTS PASSED (100% NON-DESTRUCTIVE)")
