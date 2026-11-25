"""Annotator Workforce Cutover & Verification Migration Engine.

Handles the atomic transition of provisional annotators who have earned official
workforce recognition into verified production annotator accounts:
1. Provisions the new verified User account (role='annotator', is_verified=True).
2. Atomically migrates tasks, submissions, payment ledger, and agreements.
3. Archives the legacy provisional account (is_active=False, is_archived=True).
4. Issues a cryptographically signed, single-use, 48-hour activation token.
5. Emits auditable event logs and dispatches the official onboarding activation email.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from flask import current_app, url_for
from sqlalchemy import func, select, update
from werkzeug.security import generate_password_hash

from audit import log_event
from extensions import db
from mailer import send_verification_activation_email
from models import (
    Applicant,
    ContributorConsent,
    Payment,
    Project,
    Submission,
    Task,
    User,
    money,
    utc_now,
)

logger = logging.getLogger(__name__)


def compute_provisional_performance(applicant_id: int) -> dict[str, Any]:
    """Compute real-time performance and verification readiness metrics for an applicant.

    Args:
        applicant_id: The Applicant primary key.

    Returns:
        Dictionary of performance metrics, QA averages, project counts, and eligibility.
    """
    applicant = db.session.get(Applicant, applicant_id)
    if not applicant or not applicant.user:
        return {
            "total_tasks_assigned": 0,
            "total_submissions": 0,
            "approved_submissions": 0,
            "rejected_submissions": 0,
            "pending_submissions": 0,
            "distinct_projects_count": 0,
            "qa_average_percent": 0.0,
            "total_approved_earnings": Decimal("0.00"),
            "pending_earnings": Decimal("0.00"),
            "active_days": 0,
            "is_eligible": False,
            "verification_status": "provisional",
        }

    user = applicant.user
    user_id = user.id

    # Submission counts and stats
    submissions = db.session.scalars(
        select(Submission).where(Submission.annotator_id == user_id)
    ).all()

    total_submissions = len(submissions)
    approved_submissions = [s for s in submissions if s.status == "approved"]
    rejected_submissions = [s for s in submissions if s.status == "rejected"]
    pending_submissions = [s for s in submissions if s.status not in ("approved", "rejected")]

    # Calculate average QA score on reviewed submissions
    scores = [s.qa_score for s in submissions if s.qa_score is not None]
    if scores:
        qa_avg = round(sum(scores) / len(scores), 1)
    else:
        # Fall back to calibration trial average or 0
        qa_avg = float(applicant.trial_score_average or 0.0)

    # Distinct projects worked on
    task_ids = [s.task_id for s in submissions]
    if task_ids:
        project_ids = db.session.scalars(
            select(Task.project_id).where(Task.id.in_(task_ids)).distinct()
        ).all()
        distinct_projects = len(set(project_ids))
    else:
        distinct_projects = 0

    # Assigned tasks count
    tasks_assigned = db.session.scalar(
        select(func.count(Task.id)).where(Task.assigned_to == user_id)
    ) or 0

    # Approved & Pending earnings
    approved_earnings = user.get_total_earnings()

    # Active days
    created_dt = applicant.created_at or utc_now()
    active_days = max(1, (utc_now() - created_dt).days)

    # Eligibility rules:
    # (At least 2 distinct completed projects OR at least 30 active days)
    # AND QA Average >= 80% AND at least 5 completed submissions
    is_eligible = (
        (distinct_projects >= 2 or active_days >= 30)
        and qa_avg >= 80.0
        and len(approved_submissions) >= 5
    )

    return {
        "total_tasks_assigned": tasks_assigned,
        "total_submissions": total_submissions,
        "approved_submissions": len(approved_submissions),
        "rejected_submissions": len(rejected_submissions),
        "pending_submissions": len(pending_submissions),
        "distinct_projects_count": distinct_projects,
        "qa_average_percent": qa_avg,
        "total_approved_earnings": approved_earnings,
        "active_days": active_days,
        "is_eligible": is_eligible,
        "verification_status": applicant.verification_status or "provisional",
    }


def execute_annotator_cutover(
    applicant_id: int, admin_user: User
) -> tuple[User, str]:
    """Execute an atomic cutover from provisional applicant to verified workforce annotator.

    Transaction boundary guarantees:
    1. Creates official User record (role='annotator', is_verified=True).
    2. Re-keys provisional account email and marks is_archived=True, is_active=False.
    3. Reassigns all historical Tasks, Submissions, and Payments to the new User ID.
    4. Updates Applicant record to link to the new User ID and verification_status='verified'.
    5. Issues a 48h single-use activation token hash.

    Args:
        applicant_id: Primary key of the Applicant to verify.
        admin_user: The authenticated Admin executing the verification.

    Returns:
        Tuple of (new_verified_user, raw_activation_token).

    Raises:
        ValueError: If applicant is invalid or already verified.
    """
    applicant = db.session.get(Applicant, applicant_id)
    if not applicant:
        raise ValueError(f"Applicant #{applicant_id} does not exist.")
    if applicant.verification_status == "verified" and applicant.user and applicant.user.is_verified:
        raise ValueError(f"Applicant #{applicant_id} is already officially verified.")

    old_user = applicant.user
    if not old_user:
        raise ValueError(f"Applicant #{applicant_id} has no linked user account.")

    # 1. Generate 48-hour secure activation token
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_expires = utc_now() + timedelta(hours=48)

    clean_email = old_user.email
    # Temporarily re-key old user email to avoid unique index violation
    archived_email_alias = f"archived_{old_user.id}_{int(datetime.now(timezone.utc).timestamp())}_{old_user.email}"
    old_user.email = archived_email_alias
    db.session.flush()

    # 2. Provision new official User account
    placeholder_pass = generate_password_hash(secrets.token_urlsafe(24))
    new_user = User(
        name=old_user.name,
        email=clean_email,
        password_hash=placeholder_pass,
        role="annotator",
        is_verified=True,
        is_archived=False,
        is_active=True,
        verified_at=utc_now(),
        verified_by_id=admin_user.id,
        migrated_from_user_id=old_user.id,
        bank_name=old_user.bank_name,
        bank_account_number=old_user.bank_account_number,
        bank_account_last4=old_user.bank_account_last4,
        bank_account_name=old_user.bank_account_name,
        bank_account_type=old_user.bank_account_type,
        paystack_recipient_code=old_user.paystack_recipient_code,
        phone=old_user.phone or applicant.phone_whatsapp,
        terms_version=old_user.terms_version,
        terms_accepted_at=old_user.terms_accepted_at or utc_now(),
        activation_token_hash=token_hash,
        activation_token_expires=token_expires,
    )
    db.session.add(new_user)
    db.session.flush()

    # 3. Archive old provisional user
    old_user.is_active = False
    old_user.is_archived = True
    old_user.migrated_to_user_id = new_user.id
    db.session.flush()

    # 4. Migrate historical records
    # Submissions
    db.session.execute(
        update(Submission)
        .where(Submission.annotator_id == old_user.id)
        .values(annotator_id=new_user.id)
    )
    # Tasks
    db.session.execute(
        update(Task)
        .where(Task.assigned_to == old_user.id)
        .values(assigned_to=new_user.id)
    )
    # Payments
    db.session.execute(
        update(Payment)
        .where(Payment.annotator_id == old_user.id)
        .values(annotator_id=new_user.id)
    )
    # Migrate any existing legitimate consent records from provisional user to new user
    db.session.execute(
        update(ContributorConsent)
        .where(ContributorConsent.user_id == old_user.id)
        .values(user_id=new_user.id)
    )

    # 5. Link Applicant record to new official user
    applicant.user_id = new_user.id
    applicant.verification_status = "verified"
    applicant.stage = "active"
    applicant.admin_decision = "approved"
    applicant.admin_decision_by = admin_user.id
    applicant.admin_decision_at = utc_now()
    db.session.flush()

    # 6. Audit logging
    log_event(
        "annotator.verified_and_cutover",
        actor=admin_user,
        entity_type="applicant",
        entity_id=applicant.id,
        detail={
            "old_user_id": old_user.id,
            "new_user_id": new_user.id,
            "email": clean_email,
            "specialization_track": applicant.specialization_track,
            "primary_language": applicant.primary_language,
        },
        commit=False,
    )

    db.session.commit()
    logger.info(
        "Cutover completed: applicant #%s -> new verified user #%s (%s)",
        applicant_id,
        new_user.id,
        clean_email,
    )
    return new_user, token


def dispatch_verification_email(
    user: User, token: str, applicant: Applicant
) -> bool:
    """Send branded official verification & account activation email to the annotator.

    Args:
        user: The newly provisioned official User.
        token: The raw single-use activation token.
        applicant: The associated Applicant model.

    Returns:
        True if mail was dispatched successfully or queued.
    """
    activation_url = url_for("auth.activate_account", token=token, _external=True)
    track_label = applicant.specialization_track.replace("_", " ").title()

    try:
        return send_verification_activation_email(
            recipient=user.email,
            name=user.name,
            activation_url=activation_url,
            track_label=track_label,
        )
    except Exception:
        logger.exception("Failed to send verification activation email to %s", user.email)
        return False
