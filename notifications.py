"""In-app + optional email notifications for annotator events.

Tier 1: annotators must not have to poll the dashboard to learn that
work arrived, a submission was rejected, or a payout left the bank.
notify() writes the durable in-app row (the bell feed) and, when SMTP
is configured, fires a short email. Both legs are best-effort and
NEVER raise: a notification failure must not roll back the business
action that caused it (assignment, verdict, payout).
"""

import json
import os
import logging

from flask import current_app

from extensions import db
from mailer import send_notification_email
from models import Notification

logger = logging.getLogger(__name__)


def notify(
    user,
    kind: str,
    *,
    payload: dict | None = None,
    subject: str | None = None,
    body: str | None = None,
    commit: bool = False,
) -> None:
    """Record one in-app notification; optionally email the user.

    Args:
        user: The recipient (User instance; needs .id and .email).
        kind: Dotted event name ("task.assigned", "task.rejected",
            "payout.paid").
        payload: Small JSON-safe dict shown alongside the message.
        subject: Email subject; when None no email is attempted.
        body: Email body (falls back to the subject text).
        commit: Commit the notification row immediately (callers that
            commit later may pass False and let their own commit
            persist it).
    """
    try:
        db.session.add(
            Notification(
                user_id=user.id,
                kind=kind,
                payload=(
                    json.dumps(payload, ensure_ascii=False)
                    if payload
                    else None
                ),
            )
        )
        if commit:
            db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "Notification %s for user %s could not be stored.",
            kind,
            user.id,
        )
    if subject and current_app.config.get("SMTP_HOST"):
        # Phase 10: when the job queue runs, the SMTP leg moves off
        # the request path (the in-app row above is already durable).
        if current_app.config.get("JOBS_ENABLED") and os.environ.get(
                "BRYDL_DISABLE_JOBS", "") != "1":
            try:
                import jobs

                jobs.enqueue(
                    "email.deliver",
                    {
                        "to": user.email,
                        "subject": subject,
                        "body": body or subject,
                    },
                )
                return
            except Exception:
                logger.exception(
                    "Email job enqueue to %s failed; sending inline.",
                    user.email,
                )
        try:
            send_notification_email(user.email, subject, body or subject)
        except Exception:
            logger.exception(
                "Notification email to %s could not be sent.", user.email
            )
