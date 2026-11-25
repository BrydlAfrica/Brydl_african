"""Brydl Contributor Legal & Policy Pack Metadata, Canonical Content & Integrity Engine.

Provides versioning, canonical text hashing, and document metadata for:
 1. Contributor Terms & Conditions (Version 2.1)
 2. Contributor Privacy Notice (Version 1.0)
 3. Contributor Policies (Version 1.0)
 4. Historical Contributor Terms & Conditions (Version 1.0 Archive)

Document integrity is guaranteed by computing SHA-256 digests directly over the
normalized canonical text of each document version. Any substantive text change
alters the cryptographic fingerprint.

Unknown or unregistered versions fail closed by raising ValueError.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

TERMS_VERSION = "2.1"
PRIVACY_NOTICE_VERSION = "1.1"
CONTRIBUTOR_POLICIES_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Canonical Document Content Definitions (Immutable Source of Truth)
# ---------------------------------------------------------------------------

TERMS_V1_0_CANONICAL_TEXT = """BRYDL CONTRIBUTOR TERMS & CONDITIONS
VERSION: 1.0 (Historical Archive)
OPERATING JURISDICTION: Federal Republic of Nigeria
CONTACT: inquiry@brydldata.com.ng

1. CONTRIBUTOR STATUS
Contributors participate on Brydl as independent contractors. No employment relationship is created.

2. WORK SUBMISSIONS AND QUALITY
All task submissions must be original work meeting task guidelines. Work failing quality review is not approved for payment.

3. COMPENSATION
Approved tasks are compensated at advertised project rates and disbursed to registered Nigerian bank accounts.

4. CONFIDENTIALITY
Project materials and instructions are confidential and must not be copied or distributed.

5. ACCEPTANCE
Acceptance of these terms constitutes an agreement to follow platform guidelines and rules."""

TERMS_V2_1_CANONICAL_TEXT = """BRYDL CONTRIBUTOR TERMS & CONDITIONS
VERSION: 2.1
OPERATING JURISDICTION: Federal Republic of Nigeria
CONTACT: inquiry@brydldata.com.ng

1. INTRODUCTION AND CONTRACTING PARTY
These Contributor Terms & Conditions ("Terms") govern participation in projects and activities made available through the Brydl platform ("Brydl", "Platform", "we", "us" or "our"). Brydl is a platform for African-language AI evaluation, safety research, red-teaming, language data work and related human evaluation activities operating in the Federal Republic of Nigeria. Contact: inquiry@brydldata.com.ng.

2. ELIGIBILITY AND ACCOUNT REGISTRATION
To participate as a contributor, you must be at least 18 years of age, legally eligible to contract in Nigeria, and complete required onboarding assessments. You must provide accurate, current registration details, maintain credential confidentiality, and not share accounts.

3. NATURE OF CONTRIBUTOR RELATIONSHIP
You participate as an independent contractor. Nothing in these Terms creates an employment, agency, partnership, or joint venture relationship. You have no authority to bind Brydl. You are responsible for your own taxes and equipment.

4. PROJECTS AND TASK REQUIREMENTS
Tasks are assigned or self-claimed based on qualification and availability. Task instructions, acceptance criteria, and deadlines are specified per project. Work must be submitted within the allotted timeframe.

5. QUALITY ASSURANCE, REVIEW AND REVISIONS
All submissions undergo quality review against stated criteria. Submissions meeting quality thresholds are approved; defective or non-compliant submissions may be rejected or returned for revision. Contributor appeals must follow documented procedures.

6. COMPENSATION AND PAYMENTS
Approved submissions are compensated at the rates specified prior to task acceptance. Payouts are batched and disbursed to the contributor's registered Nigerian bank account (NUBAN). Disputed or fraudulent submissions are ineligible for compensation.

7. CONFIDENTIALITY AND INFORMATION SECURITY
All project data, prompt texts, model outputs, client identities, and platform tools are confidential. You must not disclose, export, copy, or use project materials outside authorized platform workflows.

8. PERSONAL DATA AND PRIVACY
Personal data is processed in accordance with the Nigeria Data Protection Act 2023 (NDPA) and our Contributor Privacy Notice. Bank details are encrypted at rest. Consent records and audit logs are securely retained.

9. INTELLECTUAL PROPERTY AND DELIVERABLES
Upon submission, all intellectual property, data rights, and deliverables created in the course of project work are irrevocably assigned to the Platform and its designated clients, free from encumbrances.

10. SENSITIVE CONTENT AND CONTRIBUTOR WELLBEING
Certain evaluation projects may involve sensitive, controversial, or adversarial content. Contributors may opt out of sensitive projects prior to task commencement without penalty.

11. PROHIBITED CONDUCT
Contributors must not use automated scraping, bots, collusion, multiple accounts, unauthorized generative AI tools, or submit fraudulent, plagiarized, or malicious content.

12. ACCOUNT RESTRICTIONS, SUSPENSION AND TERMINATION
Brydl reserves the right to suspend or terminate accounts for material breaches of these Terms, fraud, quality violations, or policy infractions. Accrued legitimate earnings will be paid subject to offset for fraud losses.

13. PLATFORM AVAILABILITY AND OPPORTUNITIES
The Platform does not guarantee continuous task availability, minimum volume, or guaranteed income. Task distribution depends on client demand and contributor qualifications.

14. LIABILITY
To the maximum extent permitted by Nigerian law, the Platform is provided on an "as is" and "as available" basis without warranties of any kind.

15. CHANGES TO THESE TERMS
Material modifications will be notified to contributors. Continued platform access requires re-affirming consent to updated versions.

16. COMPLAINTS AND DISPUTE RESOLUTION
Complaints and disputes should be submitted to inquiry@brydldata.com.ng. Disputes will be resolved through good-faith negotiation, followed by mediation under Nigerian arbitration rules.

17. GENERAL PROVISIONS
If any provision is found invalid, remaining terms remain in effect. Failure to enforce a provision is not a waiver.

18. CONTACT
For inquiries regarding these Terms: inquiry@brydldata.com.ng.

19. CONTRIBUTOR ACCEPTANCE PROTOCOL
Selecting "Accept Terms & Conditions" and submitting the verified form creates a durable, timestamped legal consent record linking your account ID, UTC timestamp, IP address, and document SHA-256 hash."""

PRIVACY_V1_0_CANONICAL_TEXT = """BRYDL CONTRIBUTOR PRIVACY NOTICE
VERSION: 1.0
OPERATING JURISDICTION: Federal Republic of Nigeria
GOVERNING STATUTE: Nigeria Data Protection Act 2023 (NDPA 2023)
CONTACT: inquiry@brydldata.com.ng

1. ABOUT THIS NOTICE & DATA CONTROLLER DETAILS (NDPA SECTION 27)
This Privacy Notice is issued pursuant to Section 27 of the Nigeria Data Protection Act 2023 (NDPA). It explains how personal data relating to applicants and registered contributors is collected, used, stored, shared, and protected.
Platform: Brydl Platform (operating under Gretek Technologies).
Data Controllers: The leadership and operational team of Gretek Technologies / Brydl (acting as joint data controllers under Section 65 of the NDPA).
Operating locations: Kano (evaluator and linguist operations) and Abuja (administrative coordination), Nigeria. Contact: inquiry@brydldata.com.ng.

2. PRINCIPLES OF PERSONAL DATA PROCESSING (NDPA SECTION 24)
Personal data is processed in accordance with the principles set out in Section 24 of the NDPA: lawfully, fairly, and transparently; collected for specified, explicit, and legitimate purposes; adequate, relevant, and limited to what is necessary; accurate and kept up to date; retained no longer than necessary; and secured with appropriate technical and organizational measures.

3. PERSONAL DATA WE MAY COLLECT
We collect: (a) Account and identity information (name, email, phone); (b) Eligibility and qualification data (languages, skills, assessment scores); (c) Work product and submissions (annotations, evaluations, ratings); (d) Payment details (bank name, 10-digit NUBAN encrypted at rest); (e) Technical metadata (IP address, user agent, session timestamps); (f) Support and dispute records.

4. WHY WE PROCESS PERSONAL DATA
Data is processed to administer contributor accounts, verify language qualifications, assign and review tasks, calculate and disburse approved compensation, prevent multi-account fraud, resolve disputes, and maintain statutory compliance.

5. LAWFUL BASES FOR PROCESSING (NDPA SECTION 25)
- Section 25(1)(b) Contractual Necessity: Administering contributor accounts, task workflow, and payout execution.
- Section 25(1)(c) Legal Obligation: Statutory tax, financial audit, and regulatory reporting.
- Section 25(1)(f) Legitimate Interests: Fraud prevention, platform integrity, audit logging, and security monitoring.
- Section 25(1)(a) & Section 26 Consent: Voluntary consent for optional communication preferences and specialized profile disclosures.

6. HOW WE SHARE PERSONAL DATA
Deliverable datasets are shared with authorized project clients with contributor PII severed. Payment instructions are transmitted securely to licensed Nigerian banking partners and Paystack. Technical hosting providers process data under strict data processing agreements.

7. CROSS-BORDER DATA TRANSFERS (NDPA SECTIONS 41-43)
Cross-border data transfers to cloud service providers utilize standard contractual clauses, technical safeguards, and transfer mechanisms in compliance with NDPA Sections 41–43.

8. DATA RETENTION PERIODS AND JUSTIFICATIONS
- Account Profile: Retained for account duration + 5 years for contractual limitation compliance.
- Payment & Financial Records: Retained for 7 years under Nigerian financial and tax audit statutes (CITA / CAMA).
- Consent Records & Audit Logs: Retained permanently or account duration + 6 years as legal proof of consent and contract acceptance.
- IP Addresses & Security Logs: Retained for 3 years for threat intelligence and fraud auditing.
- Task Submissions: Retained for project lifecycle + 3 years as client benchmark assets.

9. SECURITY, INTEGRITY AND CONFIDENTIALITY (NDPA SECTION 39)
Safeguards implemented under Section 39 include AES-128-CBC + HMAC-SHA256 authenticated encryption for NUBANs, PBKDF2/Argon2 password hashing, HTTPS transport encryption, role-based access control, and append-only audit logging.

10. DATA SUBJECT RIGHTS AND PROTECTIONS (NDPA SECTIONS 34-38)
Under Sections 34–38 of the NDPA, contributors have: Rights of a data subject to confirmation, access, rectification, and erasure (Section 34); Right to withdrawal of consent at any time without affecting prior lawful processing (Section 35); Right to object to processing based on legitimate interests or direct marketing (Section 36); Safeguards regarding automated decision-making and profiling (Section 37); and Right to data portability in structured, commonly used formats (Section 38). Contributors may also lodge complaints with the Nigeria Data Protection Commission (NDPC). Submit requests to inquiry@brydldata.com.ng.

11. COOKIES AND TECHNICAL STORAGE
Essential first-party session cookies are used for authentication and CSRF security. No third-party tracking or advertising cookies are deployed.

12. MINORS AND CHILDREN'S DATA
Platform access is strictly restricted to individuals aged 18 and above. Minors are not permitted to register.

13. CHANGES TO THIS NOTICE
Updates will be published with a revised version number and effective date.

14. CONTACT
Data Protection Team: inquiry@brydldata.com.ng."""

PRIVACY_V1_1_CANONICAL_TEXT = """BRYDL CONTRIBUTOR PRIVACY NOTICE
VERSION: 1.1
OPERATING JURISDICTION: Federal Republic of Nigeria
GOVERNING STATUTE: Nigeria Data Protection Act 2023 (NDPA 2023)
CONTACT: inquiry@brydldata.com.ng / privacy@brydldata.com.ng

1. ABOUT THIS NOTICE & DATA CONTROLLER DETAILS (NDPA SECTION 27)
This Privacy Notice is issued pursuant to Section 27 of the Nigeria Data Protection Act 2023 (NDPA). It explains how personal data relating to applicants and registered contributors is collected, used, stored, shared, and protected.
Platform: Brydl Platform (operating under Gretek Technologies, an unincorporated technology initiative).
Data Controllers: The founding and operational leadership team of Gretek Technologies / Brydl (acting as joint data controllers under Section 65 of the NDPA).
Operating locations: Kano (evaluator and linguist operations) and Abuja (administrative and regulatory coordination), Nigeria. Contact: inquiry@brydldata.com.ng / privacy@brydldata.com.ng.

2. PRINCIPLES OF PERSONAL DATA PROCESSING (NDPA SECTION 24)
Personal data is processed in accordance with the principles set out in Section 24 of the NDPA: lawfully, fairly, and transparently; collected for specified, explicit, and legitimate purposes; adequate, relevant, and limited to what is necessary; accurate and kept up to date; retained no longer than necessary; and secured with appropriate technical and organizational measures.

3. PERSONAL DATA WE MAY COLLECT
We collect: (a) Account and identity information (name, email, phone); (b) Eligibility and qualification data (languages, skills, assessment scores); (c) Work product and submissions (annotations, evaluations, ratings); (d) Payment details (bank name, 10-digit NUBAN encrypted at rest); (e) Technical metadata (IP address, user agent, session timestamps); (f) Support and dispute records.

4. WHY WE PROCESS PERSONAL DATA
Data is processed to administer contributor accounts, verify language qualifications, assign and review tasks, calculate and disburse approved compensation, prevent multi-account fraud, resolve disputes, and maintain statutory compliance.

5. LAWFUL BASES FOR PROCESSING (NDPA SECTION 25)
- Section 25(1)(b)(i) Contractual Necessity: Administering contributor accounts, task workflow, quality assurance, and payout execution under the Contributor Terms & Conditions.
- Section 25(1)(b)(ii) Legal Obligation: Compliance with applicable Personal Income Tax Act (PITA) / State Internal Revenue Service withholding rules, AML/CBN financial transaction record-keeping for NUBAN transfers, and NDPA compliance audit trails.
- Section 25(1)(b)(v) Legitimate Interests: Platform integrity, fraud prevention, audit logging, security monitoring, and dispute resolution.
- Section 25(1)(a) & Section 26 Consent: Voluntary, unbundled consent for optional email notifications, product newsletters, and project announcements. Section 26 governs conditions, burden of proof on the controller, clear withdrawal mechanisms, and affirmative opt-in requirements.

6. HOW WE SHARE PERSONAL DATA
Deliverable datasets are shared with authorized project clients with contributor PII severed. Payment instructions are transmitted securely to licensed Nigerian banking partners and Paystack. Technical hosting providers process data under strict data processing agreements.

7. CROSS-BORDER DATA TRANSFERS (NDPA SECTIONS 41-43)
Platform infrastructure is hosted on cloud servers located in Frankfurt, Germany (EU) and the United States (Render Services, Inc. and Supabase / AWS). Cross-border transfers to these cloud infrastructure providers are governed by Data Processing Addenda (DPAs) and Standard Contractual Clauses under NDPA Sections 41–42, and are necessary for the performance of the contributor contract under Section 43(1)(b).

8. DATA RETENTION PERIODS AND JUSTIFICATIONS
- Account Profile: Retained for account duration + 12 months post-closure for contractual dispute limitation compliance.
- Payment & Financial Records: Retained for 7 years under Nigerian financial and tax audit standards (PITA / SIRS rules).
- Consent Records & Audit Logs: Retained for 7 years as statutory proof of consent under NDPA Section 26(1).
- IP Addresses & Security Logs: Retained for 24 months for threat intelligence and cybersecurity investigation under NDPA Section 39.
- Task Submissions: Retained for project lifecycle + 3 years as client benchmark and quality training assets.

9. SECURITY, INTEGRITY AND CONFIDENTIALITY (NDPA SECTION 39)
Safeguards implemented under Section 39 include AES authenticated encryption for NUBANs, scrypt password hashing, HTTPS / TLS 1.3 transport encryption, role-based access control, single-hop proxy filtering, and append-only audit logging.

10. DATA SUBJECT RIGHTS AND PROTECTIONS (NDPA SECTIONS 34-38)
Under Sections 34–38 of the NDPA, contributors have statutory rights:
- Self-Service Workflows: Direct profile inspection (/annotator/profile), consent history log (/annotator/terms), task/earnings history (/annotator/history), CSV data portability export, and email consent withdrawal (/annotator/profile/notifications).
- Manual Request Workflows: Comprehensive Subject Access Requests (SARs), identity rectification, account erasure, restriction of processing, and objections to legitimate-interest processing via privacy@brydldata.com.ng.
- Regulatory Complaint: Right to lodge complaints with the Nigeria Data Protection Commission (NDPC).

11. COOKIES AND TECHNICAL STORAGE
Essential first-party session cookies are used for authentication and CSRF security. No third-party tracking or advertising cookies are deployed.

12. MINORS AND CHILDREN'S DATA
Platform access is strictly restricted to individuals aged 18 and above. Minors are not permitted to register.

13. CHANGES TO THIS NOTICE
Updates will be published with a revised version number and effective date.

14. CONTACT
Data Protection Team: privacy@brydldata.com.ng / inquiry@brydldata.com.ng."""

POLICIES_V1_0_CANONICAL_TEXT = """BRYDL CONTRIBUTOR POLICIES HUB
VERSION: 1.0 (Operational Contributor Guidance)
OPERATING JURISDICTION: Federal Republic of Nigeria
CONTACT: inquiry@brydldata.com.ng

POLICY 1: CONTRIBUTOR CODE OF CONDUCT
Contributors must treat peers, reviewers, and staff respectfully; submit original, honest work; avoid harassment, discrimination, or intimidation; protect confidential information; and report misconduct or security issues promptly.

POLICY 2: WORK QUALITY AND REVIEW POLICY
Projects provide clear instructions and objective acceptance criteria. Submissions are reviewed against rubrics. Rejections include documented feedback where practicable. Contributors have access to appeal procedures. Reviewers must not falsify ratings or self-review submissions.

POLICY 3: COMPENSATION AND PAYMENT POLICY
Compensation rates and conditions are disclosed prior to task acceptance. Approved work is recorded in the earnings ledger. Payouts are batched and disbursed via Nigerian NUBAN accounts. Banking PINs, passwords, or OTPs are never requested. No minimum income is guaranteed.

POLICY 4: CONFIDENTIALITY AND DATA HANDLING POLICY
Project materials, model responses, prompts, and datasets must not be copied, published, or used outside platform workflows. Personal data must not be extracted or re-identified. Unauthorised data disclosures must be reported immediately.

POLICY 5: SENSITIVE CONTENT AND WELLBEING POLICY
Projects with sensitive or disturbing material provide advance disclosure where practicable. Contributors may decline sensitive tasks without penalty. Contributors should observe reasonable rest limits and report out-of-scope content.

POLICY 6: COMPLAINTS AND APPEALS POLICY
Contributors may submit complaints regarding quality reviews, payments, harassment, or platform access to inquiry@brydldata.com.ng. The appeals process ensures timely acknowledgment, independent review by non-conflicted staff, reasoned outcomes, and non-retaliation protection.

POLICY 7: ACCOUNT REVIEW, SUSPENSION AND REINSTATEMENT POLICY
Account restrictions apply to serious breaches (fraud, bot use, plagiarized data, NDA violation). Suspended contributors receive written notices with grounds and appeal mechanisms. Accrued legitimate earnings are preserved subject to fraud offsets.

POLICY 8: POLICY ADMINISTRATION AND GOVERNANCE
Policies are reviewed periodically. Material updates are communicated through the platform. Document versions and revisions are tracked immutably."""

# ---------------------------------------------------------------------------
# Canonical Registry
# ---------------------------------------------------------------------------

CANONICAL_DOCUMENTS: Dict[tuple[str, str], Dict[str, Any]] = {
    ("terms_and_conditions", "2.1"): {
        "document_type": "terms_and_conditions",
        "version": "2.1",
        "title": "Brydl Contributor Terms & Conditions",
        "short_title": "Terms & Conditions",
        "status": "Published (Version 2.1)",
        "operating_country": "Nigeria",
        "contact_email": "inquiry@brydldata.com.ng",
        "effective_date": "November 2026",
        "is_draft": False,
        "canonical_text": TERMS_V2_1_CANONICAL_TEXT,
        "is_active": True,
    },
    ("terms_and_conditions", "1.0"): {
        "document_type": "terms_and_conditions",
        "version": "1.0",
        "title": "Brydl Contributor Terms & Conditions (Version 1.0 Archive)",
        "short_title": "Terms & Conditions v1.0",
        "status": "Archived / Superseded",
        "operating_country": "Nigeria",
        "contact_email": "inquiry@brydldata.com.ng",
        "effective_date": "October 2025",
        "is_draft": False,
        "canonical_text": TERMS_V1_0_CANONICAL_TEXT,
        "is_active": False,
    },
    ("privacy_notice", "1.1"): {
        "document_type": "privacy_notice",
        "version": "1.1",
        "title": "Brydl Contributor Privacy Notice",
        "short_title": "Privacy Notice",
        "status": "Published (Version 1.1)",
        "operating_country": "Nigeria",
        "contact_email": "inquiry@brydldata.com.ng",
        "effective_date": "November 2026",
        "is_draft": False,
        "canonical_text": PRIVACY_V1_1_CANONICAL_TEXT,
        "is_active": True,
    },
    ("privacy_notice", "1.0"): {
        "document_type": "privacy_notice",
        "version": "1.0",
        "title": "Brydl Contributor Privacy Notice (Version 1.0 Archive)",
        "short_title": "Privacy Notice v1.0",
        "status": "Archived / Superseded",
        "operating_country": "Nigeria",
        "contact_email": "inquiry@brydldata.com.ng",
        "effective_date": "October 2025",
        "is_draft": False,
        "canonical_text": PRIVACY_V1_0_CANONICAL_TEXT,
        "is_active": False,
    },
    ("contributor_policies", "1.0"): {
        "document_type": "contributor_policies",
        "version": "1.0",
        "title": "Brydl Contributor Policies",
        "short_title": "Contributor Policies",
        "status": "Published (Version 1.0)",
        "operating_country": "Nigeria",
        "contact_email": "inquiry@brydldata.com.ng",
        "effective_date": "November 2026",
        "is_draft": False,
        "canonical_text": POLICIES_V1_0_CANONICAL_TEXT,
        "is_active": True,
        "policies_count": 8,
    },
}


def compute_content_hash(text: str) -> str:
    """Compute a deterministic SHA-256 fingerprint over normalized text content."""
    normalized = "\n".join(
        line.strip() for line in text.strip().splitlines() if line.strip()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_document_hash(document_type: str, version: str) -> str:
    """Compute a deterministic SHA-256 fingerprint from the canonical document text.

    Fails closed by raising ValueError if the document type or version is unregistered.
    """
    doc = CANONICAL_DOCUMENTS.get((document_type, version))
    if not doc or "canonical_text" not in doc:
        raise ValueError(
            f"Cannot compute hash for unregistered canonical document: {document_type} v{version}"
        )
    return compute_content_hash(doc["canonical_text"])


def get_document_content(document_type: str, version: str) -> str:
    """Retrieve the exact canonical plain-text content for a registered document version.

    Fails closed by raising ValueError if the version is unregistered.
    """
    doc = CANONICAL_DOCUMENTS.get((document_type, version))
    if not doc or "canonical_text" not in doc:
        raise ValueError(
            f"No canonical content registered for {document_type} v{version}"
        )
    return doc["canonical_text"]


def get_canonical_document(
    document_type: str, version: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Retrieve canonical document structure and metadata for a given type and version."""
    if version is None:
        if document_type == "terms_and_conditions":
            version = TERMS_VERSION
        elif document_type == "privacy_notice":
            version = PRIVACY_NOTICE_VERSION
        elif document_type == "contributor_policies":
            version = CONTRIBUTOR_POLICIES_VERSION
        else:
            version = "1.0"

    doc = CANONICAL_DOCUMENTS.get((document_type, version))
    if doc:
        result = dict(doc)
        result["document_hash"] = compute_document_hash(document_type, version)
        return result
    return None


def get_document_metadata(document_type: str) -> Dict[str, Any]:
    """Retrieve document metadata with computed content hash for public/authenticated views."""
    doc = get_canonical_document(document_type)
    if doc:
        return doc
    raise ValueError(f"Unknown document type for metadata retrieval: {document_type}")
