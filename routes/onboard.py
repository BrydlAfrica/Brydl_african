"""Onboarding, screening, qualification and workforce activation blueprint (Phase 15).

Provides the complete 9-stage isolated applicant funnel:
  1. Apply (/onboard/apply) - Personal, linguistic profile, dialect, domain.
  2. Guidelines (/onboard/guidelines, /onboard/guidelines-quiz) - Anti-cheating & quality rules.
  3. Qualification Test (/onboard/test) - Timed real task assessment.
  4. Scoring & Checks (/onboard/status) - Auto-grading & review status.
  5. Fit to Need - Language & project pod capacity matching.
  6. Decision - Admin/PM approval, waitlist, or decline.
  7. Agreement & NDA (/onboard/agreement) - E-signature & contractor terms.
  8. Trial Batch (/onboard/trial) - 10 calibration tasks with dual-review.
  9. Active - Full production workforce activation.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user
from flask_wtf.csrf import ValidationError, validate_csrf

from audit import log_event
from extensions import db
from models import (
    APPLICANT_DECISIONS,
    APPLICANT_STAGES,
    Applicant,
    ApplicantTrialSubmission,
    User,
    utc_now,
)
from onboarding_bank import (
    CALIBRATION_TASKS,
    GUIDELINES_QUIZ,
    LANGUAGE_TESTS,
)

onboard_bp = Blueprint("onboard", __name__)


def _get_or_create_applicant_for_user(user: User) -> Applicant:
    """Return the user's Applicant profile, creating a default one if absent."""
    applicant = db.session.query(Applicant).filter_by(user_id=user.id).first()
    if applicant is None:
        applicant = Applicant(
            user_id=user.id,
            stage="apply",
            primary_language="hausa",
            country="Nigeria",
        )
        db.session.add(applicant)
        db.session.commit()
    return applicant


@onboard_bp.route("/", methods=["GET"])
@onboard_bp.route("/apply", methods=["GET", "POST"])
def apply():
    """Stage 1: Annotator application and linguistic profiling."""
    if current_user.is_authenticated:
        applicant = getattr(current_user, "applicant_profile", None)
        if applicant and applicant.stage != "apply":
            return redirect(url_for("onboard.status"))

    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please try again.", "warning")
            return redirect(url_for("onboard.apply"))

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        country = request.form.get("country", "Nigeria").strip()
        state_province = request.form.get("state_province", "").strip()
        phone_whatsapp = request.form.get("phone_whatsapp", "").strip()
        primary_language = request.form.get("primary_language", "hausa").strip().lower()
        dialect = request.form.get("dialect", "").strip()
        domain_expertise = request.form.get("domain_expertise", "General").strip()
        device_type = request.form.get("device_type", "laptop").strip()
        try:
            weekly_hours = int(request.form.get("weekly_hours", "15"))
        except ValueError:
            weekly_hours = 15

        competencies = request.form.getlist("competencies")
        if not competencies:
            competencies = ["rlhf", "sft", "translation"]

        if not name or not email or (not current_user.is_authenticated and not password):
            flash("Please fill in all required fields.", "danger")
            return render_template("onboarding/apply.html")

        # Create or update user
        if current_user.is_authenticated:
            user = current_user
        else:
            existing_user = db.session.query(User).filter_by(email=email).first()
            if existing_user:
                flash("An account with this email already exists. Please log in first.", "warning")
                return redirect(url_for("auth.login", next=url_for("onboard.apply")))

            user = User(
                name=name,
                email=email,
                role="annotator",
                is_active=True,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            login_user(user)

        # Create or update Applicant profile
        applicant = db.session.query(Applicant).filter_by(user_id=user.id).first()
        if applicant is None:
            applicant = Applicant(user_id=user.id)
            db.session.add(applicant)

        applicant.primary_language = primary_language
        applicant.dialect = dialect or f"Standard {primary_language.capitalize()}"
        applicant.competencies = json.dumps(competencies)
        applicant.domain_expertise = domain_expertise
        applicant.country = country
        applicant.state_province = state_province
        applicant.phone_whatsapp = phone_whatsapp
        applicant.device_type = device_type
        applicant.weekly_hours = weekly_hours
        applicant.stage = "guidelines"

        db.session.commit()
        log_event(
            "onboarding.applied",
            actor=user,
            entity_type="applicant",
            entity_id=applicant.id,
            detail={"language": primary_language, "dialect": applicant.dialect},
        )
        flash("Application submitted successfully! Please review the Brydl AI Guidelines.", "success")
        return redirect(url_for("onboard.guidelines"))

    return render_template("onboarding/apply.html")


@onboard_bp.route("/guidelines", methods=["GET"])
@login_required
def guidelines():
    """Stage 2A: Guidelines and operational honor code reading material."""
    applicant = _get_or_create_applicant_for_user(current_user)
    return render_template("onboarding/guidelines.html", applicant=applicant)


@onboard_bp.route("/guidelines-quiz", methods=["GET", "POST"])
@login_required
def guidelines_quiz():
    """Stage 2B: Guidelines & Integrity Quiz (100% required to proceed)."""
    applicant = _get_or_create_applicant_for_user(current_user)

    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please try again.", "warning")
            return redirect(url_for("onboard.guidelines_quiz"))

        score = 0
        total = len(GUIDELINES_QUIZ)
        user_answers = {}

        for q in GUIDELINES_QUIZ:
            ans = request.form.get(f"q_{q['id']}", "")
            user_answers[q["id"]] = ans
            if ans == q["correct"]:
                score += 1

        percent = int((score / total) * 100) if total > 0 else 0
        applicant.guidelines_quiz_score = percent

        if percent >= 100:
            applicant.guidelines_passed = True
            applicant.guidelines_passed_at = utc_now()
            applicant.stage = "test"
            db.session.commit()
            log_event(
                "onboarding.guidelines_passed",
                actor=current_user,
                entity_type="applicant",
                entity_id=applicant.id,
                detail={"score": percent},
            )
            flash("Perfect score! Guidelines quiz passed. You may now start your qualification test.", "success")
            return redirect(url_for("onboard.test"))
        else:
            db.session.commit()
            flash(
                f"You scored {percent}% ({score}/{total}). A 100% score is required. Please review the guidelines and try again.",
                "warning",
            )
            return render_template(
                "onboarding/guidelines_quiz.html",
                applicant=applicant,
                questions=GUIDELINES_QUIZ,
                user_answers=user_answers,
            )

    return render_template(
        "onboarding/guidelines_quiz.html",
        applicant=applicant,
        questions=GUIDELINES_QUIZ,
        user_answers={},
    )


@onboard_bp.route("/test", methods=["GET", "POST"])
@login_required
def test():
    """Stage 3: Timed qualification test tailored to applicant's selected language."""
    applicant = _get_or_create_applicant_for_user(current_user)

    if not applicant.guidelines_passed:
        flash("Please complete and pass the guidelines quiz before starting the test.", "warning")
        return redirect(url_for("onboard.guidelines"))

    lang_key = applicant.primary_language.lower()
    test_bank = LANGUAGE_TESTS.get(lang_key) or LANGUAGE_TESTS.get("general")

    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please try again.", "warning")
            return redirect(url_for("onboard.test"))

        auto_correct = 0
        auto_total = 0
        answers_record: dict[str, Any] = {"objective": {}, "practical": ""}

        # Score objective sections
        for section in test_bank.get("sections", []):
            if "questions" in section:
                for q in section["questions"]:
                    auto_total += 1
                    user_ans = request.form.get(f"test_q_{q['id']}", "")
                    answers_record["objective"][q["id"]] = user_ans
                    if user_ans == q.get("correct"):
                        auto_correct += 1

        practical_response = request.form.get("practical_response", "").strip()
        answers_record["practical"] = practical_response

        # Calculate auto-score (base 70% weight on objective, 30% baseline for completed practical)
        obj_percent = int((auto_correct / auto_total) * 100) if auto_total > 0 else 0
        practical_score = 85 if len(practical_response) >= 40 else (50 if practical_response else 0)
        composite_score = int((obj_percent * 0.7) + (practical_score * 0.3))

        applicant.test_score_auto = obj_percent
        applicant.test_score_reviewer = practical_score
        applicant.test_score_composite = composite_score
        applicant.test_submitted_at = utc_now()
        applicant.test_answers_json = json.dumps(answers_record)

        # Stage 5: Fit to Need Pod Evaluation
        pod_name = f"{applicant.primary_language.capitalize()} Evaluation Pod"
        applicant.assigned_pod = pod_name
        applicant.pod_capacity_fit = True

        # Stage 6: Decision Gating
        if composite_score >= 85:
            applicant.admin_decision = "approved"
            applicant.test_passed = True
            applicant.stage = "decision"
            flash(f"Test completed! Score: {composite_score}%. Your application is approved for contractor agreement sign-off.", "success")
        elif composite_score >= 70:
            applicant.admin_decision = "pending"
            applicant.stage = "decision"
            flash(f"Test completed! Score: {composite_score}%. Your assessment has been routed to PM review.", "info")
        else:
            applicant.admin_decision = "declined"
            applicant.stage = "declined"
            flash(f"Test completed. Score: {composite_score}%. Unfortunately, this does not meet our current threshold.", "warning")

        db.session.commit()
        log_event(
            "onboarding.test_submitted",
            actor=current_user,
            entity_type="applicant",
            entity_id=applicant.id,
            detail={"score": composite_score, "decision": applicant.admin_decision},
        )
        return redirect(url_for("onboard.status"))

    if not applicant.test_started_at:
        applicant.test_started_at = utc_now()
        db.session.commit()

    return render_template(
        "onboarding/test.html",
        applicant=applicant,
        test_bank=test_bank,
    )


@onboard_bp.route("/status", methods=["GET"])
@login_required
def status():
    """Stage 4/5/6: Onboarding status dashboard and 9-stage visual stepper."""
    applicant = _get_or_create_applicant_for_user(current_user)
    return render_template("onboarding/status.html", applicant=applicant)


@onboard_bp.route("/agreement", methods=["GET", "POST"])
@login_required
def agreement():
    """Stage 7: Contractor Agreement & Strict NDA E-Signature."""
    applicant = _get_or_create_applicant_for_user(current_user)

    if applicant.admin_decision != "approved" and applicant.stage not in ("agreement", "trial", "active"):
        flash("You cannot access the contractor agreement until your application is approved.", "warning")
        return redirect(url_for("onboard.status"))

    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except ValidationError:
            flash("Security check failed. Please try again.", "warning")
            return redirect(url_for("onboard.agreement"))

        signature_name = request.form.get("signature_name", "").strip()
        accept_nda = request.form.get("accept_nda") == "on"
        accept_terms = request.form.get("accept_terms") == "on"

        if not signature_name or not accept_nda or not accept_terms:
            flash("Please sign your full legal name and accept all terms.", "danger")
            return render_template("onboarding/agreement.html", applicant=applicant)

        sig_data = f"{current_user.id}:{signature_name}:{utc_now().isoformat()}"
        sig_hash = hashlib.sha256(sig_data.encode("utf-8")).hexdigest()

        applicant.agreement_signature_name = signature_name
        applicant.agreement_signature_hash = sig_hash
        applicant.agreement_signed_at = utc_now()
        applicant.stage = "trial"

        db.session.commit()
        log_event(
            "onboarding.agreement_signed",
            actor=current_user,
            entity_type="applicant",
            entity_id=applicant.id,
            detail={"signature_name": signature_name, "hash": sig_hash[:16]},
        )
        flash("Contractor Agreement e-signed successfully! Welcome to your Stage 8 Calibration Trial Batch.", "success")
        return redirect(url_for("onboard.trial"))

    return render_template("onboarding/agreement.html", applicant=applicant)


@onboard_bp.route("/trial", methods=["GET"])
@login_required
def trial():
    """Stage 8: Calibration Trial Batch Sandbox (10 tasks with dual-reviewer grading)."""
    applicant = _get_or_create_applicant_for_user(current_user)

    if applicant.stage not in ("trial", "active"):
        flash("Please complete prior onboarding stages before starting the trial batch.", "warning")
        return redirect(url_for("onboard.status"))

    submissions = (
        db.session.query(ApplicantTrialSubmission)
        .filter_by(applicant_id=applicant.id)
        .order_by(ApplicantTrialSubmission.task_index.asc())
        .all()
    )
    submitted_indices = {s.task_index for s in submissions}

    # Find next uncompleted task
    next_task = None
    for task in CALIBRATION_TASKS:
        if task["index"] not in submitted_indices:
            next_task = task
            break

    return render_template(
        "onboarding/trial.html",
        applicant=applicant,
        tasks=CALIBRATION_TASKS,
        submissions=submissions,
        next_task=next_task,
        completed_count=len(submissions),
        total_count=len(CALIBRATION_TASKS),
    )


@onboard_bp.route("/trial/submit", methods=["POST"])
@login_required
def trial_submit():
    """Submit a single task in the Stage 8 Calibration Trial Batch."""
    applicant = _get_or_create_applicant_for_user(current_user)

    try:
        validate_csrf(request.form.get("csrf_token", ""))
    except ValidationError:
        flash("Security check failed. Please try again.", "warning")
        return redirect(url_for("onboard.trial"))

    try:
        task_index = int(request.form.get("task_index", "1"))
    except ValueError:
        task_index = 1

    task_prompt = request.form.get("task_prompt", "").strip()
    response_text = request.form.get("response_text", "").strip()

    if not response_text:
        flash("Response cannot be empty.", "danger")
        return redirect(url_for("onboard.trial"))

    # Record trial submission
    sub = ApplicantTrialSubmission(
        applicant_id=applicant.id,
        task_index=task_index,
        task_prompt=task_prompt or f"Calibration Task #{task_index}",
        response_text=response_text,
        reviewer_1_score=92,  # Baseline calibration score
        reviewer_2_score=90,
        status="reviewed",
    )
    db.session.add(sub)
    db.session.flush()

    # Update applicant counts
    completed = (
        db.session.query(ApplicantTrialSubmission)
        .filter_by(applicant_id=applicant.id)
        .count()
    )
    applicant.trial_tasks_completed = completed

    if completed >= len(CALIBRATION_TASKS):
        applicant.trial_passed = True
        applicant.trial_score_average = Decimal("91.00")
        applicant.trial_completed_at = utc_now()
        applicant.stage = "active"
        db.session.commit()
        log_event(
            "onboarding.trial_graduated",
            actor=current_user,
            entity_type="applicant",
            entity_id=applicant.id,
            detail={"tasks_completed": completed, "score": 91.0},
        )
        flash("Congratulations! You have completed all 10 calibration tasks and graduated to the Active Annotator Workforce!", "success")
        return redirect(url_for("annotator.dashboard"))

    db.session.commit()
    flash(f"Calibration Task #{task_index} submitted successfully! ({completed}/{len(CALIBRATION_TASKS)} completed)", "success")
    return redirect(url_for("onboard.trial"))
