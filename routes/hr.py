"""HR Screening & Workforce Verification Desk blueprint.

Provides the human resources interview, professional evaluation, and clearance portal
for candidates qualifying for official workforce verification.

Invariants enforced here:
- Strict RBAC: only role IN ('admin', 'hr') via @hr_required.
- Records structured evaluation: communication, professionalism, availability, notes.
- Submits departmental clearance recommendation before final Admin cutover.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload
from wtforms import (
    IntegerField,
    SelectField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, NumberRange

from audit import log_event
from cutover import compute_provisional_performance
from extensions import db
from models import (
    APPLICANT_TRACKS,
    APPLICANT_VERIFICATION_STATUSES,
    Applicant,
    HRScreening,
    User,
    utc_now,
)
from onboarding_bank import SPECIALIZATION_TRACKS_META
from routes import hr_required

hr_bp = Blueprint("hr", __name__, url_prefix="/hr")


class HRScreeningForm(FlaskForm):
    """Evaluation form for conducting an annotator verification interview."""

    communication_score = SelectField(
        "Communication & Responsiveness (1-5)",
        choices=[
            ("5", "5 - Excellent (Fast, articulate, professional)"),
            ("4", "4 - Good (Clear, responsive)"),
            ("3", "3 - Adequate (Acceptable baseline)"),
            ("2", "2 - Weak (Delayed responses, unclear)"),
            ("1", "1 - Poor (Unresponsive, unresponsive to feedback)"),
        ],
        default="4",
        validators=[DataRequired()],
    )
    professionalism_score = SelectField(
        "Professionalism & Integrity (1-5)",
        choices=[
            ("5", "5 - High (Strict confidentiality, zero AI cheating)"),
            ("4", "4 - Good (Dependable, adheres to standards)"),
            ("3", "3 - Adequate (Complies when reminded)"),
            ("2", "2 - Concerning (Questionable conduct)"),
            ("1", "1 - Unacceptable (Integrity violation risk)"),
        ],
        default="4",
        validators=[DataRequired()],
    )
    availability_confirmed_hours = IntegerField(
        "Confirmed Weekly Availability (Hours)",
        default=15,
        validators=[
            DataRequired(),
            NumberRange(min=1, max=80, message="Weekly hours must be between 1 and 80."),
        ],
    )
    notes = TextAreaField(
        "HR Interview Notes & Observations",
        validators=[
            DataRequired(message="Please provide detailed interview notes and assessment."),
        ],
    )
    recommendation = SelectField(
        "HR Recommendation",
        choices=[
            ("recommended", "Recommended — Cleared for Official Verification"),
            ("needs_practice", "Needs More Practice — Defer Verification"),
            ("disqualified", "Disqualified — Reject Candidate"),
        ],
        default="recommended",
        validators=[DataRequired()],
    )
    submit = SubmitField("Save HR Evaluation & Submit Clearance")


@hr_bp.route("/")
@hr_bp.route("/candidates")
@hr_required
def candidates():
    """List provisional workforce candidates with performance dossiers and verification status."""
    track_filter = request.args.get("track", "").strip()
    status_filter = request.args.get("status", "").strip()
    lang_filter = request.args.get("lang", "").strip()

    stmt = (
        select(Applicant)
        .options(joinedload(Applicant.user), joinedload(Applicant.hr_screening))
        .order_by(Applicant.created_at.desc())
    )

    if track_filter and track_filter in APPLICANT_TRACKS:
        stmt = stmt.where(Applicant.specialization_track == track_filter)
    if status_filter and status_filter in APPLICANT_VERIFICATION_STATUSES:
        stmt = stmt.where(Applicant.verification_status == status_filter)
    if lang_filter:
        stmt = stmt.where(Applicant.primary_language == lang_filter.lower())

    applicants = db.session.scalars(stmt).all()

    # Pre-calculate performance metrics for all candidate cards
    candidate_records = []
    for app in applicants:
        perf = compute_provisional_performance(app.id)
        candidate_records.append({
            "applicant": app,
            "perf": perf,
            "screening": app.hr_screening,
        })

    # Summary metrics for HR dashboard header
    total_provisional = db.session.scalar(
        select(func.count(Applicant.id)).where(Applicant.verification_status == "provisional")
    ) or 0
    total_pending_hr = db.session.scalar(
        select(func.count(Applicant.id)).where(Applicant.verification_status == "pending_hr")
    ) or 0
    total_hr_cleared = db.session.scalar(
        select(func.count(Applicant.id)).where(Applicant.verification_status == "hr_cleared")
    ) or 0
    total_verified = db.session.scalar(
        select(func.count(Applicant.id)).where(Applicant.verification_status == "verified")
    ) or 0

    return render_template(
        "hr/candidates.html",
        candidates=candidate_records,
        track_filter=track_filter,
        status_filter=status_filter,
        lang_filter=lang_filter,
        tracks_meta=SPECIALIZATION_TRACKS_META,
        total_provisional=total_provisional,
        total_pending_hr=total_pending_hr,
        total_hr_cleared=total_hr_cleared,
        total_verified=total_verified,
    )


@hr_bp.route("/candidates/<int:applicant_id>", methods=["GET", "POST"])
@hr_required
def candidate_detail(applicant_id: int):
    """HR screening interview and evaluation detail view."""
    applicant = db.session.get(Applicant, applicant_id)
    if not applicant:
        abort(404)

    perf = compute_provisional_performance(applicant.id)
    screening = applicant.hr_screening

    form = HRScreeningForm()

    if request.method == "GET" and screening:
        form.communication_score.data = str(screening.communication_score)
        form.professionalism_score.data = str(screening.professionalism_score)
        form.availability_confirmed_hours.data = screening.availability_confirmed_hours
        form.notes.data = screening.notes
        form.recommendation.data = screening.recommendation

    if form.validate_on_submit():
        if screening is None:
            screening = HRScreening(
                applicant_id=applicant.id,
                hr_user_id=current_user.id,
                interview_date=utc_now(),
                communication_score=int(form.communication_score.data),
                professionalism_score=int(form.professionalism_score.data),
                availability_confirmed_hours=form.availability_confirmed_hours.data,
                notes=form.notes.data.strip(),
                recommendation=form.recommendation.data,
            )
            db.session.add(screening)
        else:
            screening.hr_user_id = current_user.id
            screening.communication_score = int(form.communication_score.data)
            screening.professionalism_score = int(form.professionalism_score.data)
            screening.availability_confirmed_hours = form.availability_confirmed_hours.data
            screening.notes = form.notes.data.strip()
            screening.recommendation = form.recommendation.data
            screening.updated_at = utc_now()

        # Update applicant verification status based on HR recommendation
        if form.recommendation.data == "recommended":
            applicant.verification_status = "hr_cleared"
        elif form.recommendation.data == "needs_practice":
            applicant.verification_status = "pending_hr"
        elif form.recommendation.data == "disqualified":
            applicant.verification_status = "rejected"

        db.session.commit()

        log_event(
            "hr.screening_recorded",
            actor=current_user,
            entity_type="applicant",
            entity_id=applicant.id,
            detail={
                "recommendation": form.recommendation.data,
                "comm_score": int(form.communication_score.data),
                "prof_score": int(form.professionalism_score.data),
            },
            commit=True,
        )

        flash(
            f"HR Evaluation recorded for {applicant.user.name}. Status updated to {applicant.verification_status.title()}.",
            "success",
        )
        return redirect(url_for("hr.candidates"))

    return render_template(
        "hr/candidate_detail.html",
        applicant=applicant,
        perf=perf,
        screening=screening,
        form=form,
        track_meta=SPECIALIZATION_TRACKS_META.get(applicant.specialization_track, {}),
    )
