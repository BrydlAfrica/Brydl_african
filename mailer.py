"""Outbound email for Brydl Dashboard.

One job: deliver transactional mail (currently just password-reset
links) through customer SMTP credentials from the environment. There
is deliberately no mail dependency (no Flask-Mail): smtplib is stdlib,
and the free-tier deploy keeps its dependency list short.

When SMTP_HOST is unset (local development) the mail body is written
to the application log instead of being sent — the whole flow stays
testable offline and nothing is emailed by surprise.

All configuration comes from config.py environment knobs:
SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD / MAIL_FROM /
SMTP_STARTTLS.
"""

import logging
import smtplib
from email.message import EmailMessage

from config import (
    MAIL_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_STARTTLS,
    SMTP_USERNAME,
)

logger = logging.getLogger(__name__)


def mail_enabled() -> bool:
    """Return True when SMTP is configured.

    Returns:
        True if SMTP_HOST is set — mail will actually be sent.
    """
    return bool(SMTP_HOST)


def send_password_reset_email(recipient: str, reset_url: str) -> bool:
    """Send (or, without SMTP configured, log) a password-reset link.

    Args:
        recipient: The annotator's/admin's email address.
        reset_url: The absolute reset link containing the one-off
            token. Never logged at info level WITH the token when SMTP
            is enabled; in dev-fallback the link IS the delivery, so
            it goes to the log by design.

    Returns:
        True when delivered (sent, or dev-logged), False on failure.
    """
    subject = "Reset your Brydl password"
    body = (
        "Hello,\n\n"
        "Someone (hopefully you) asked to reset your Brydl password.\n"
        "Open this link within the hour to choose a new one:\n\n"
        f"{reset_url}\n\n"
        "The link works once. If you didn't request this, ignore this"
        " email — your password stays as it is.\n\n"
        "— Brydl"
    )
    if not mail_enabled():
        logger.warning(
            "SMTP not configured; DEV-FALLBACK password reset link for"
            " %s: %s",
            recipient,
            reset_url,
        )
        return True

    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(
                SMTP_HOST, SMTP_PORT, timeout=10
            ) as server:
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                if SMTP_STARTTLS:
                    server.starttls()
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        return True
    except OSError:
        logger.exception(
            "Password-reset email to %s could not be sent.", recipient
        )
        return False


def send_notification_email(recipient: str, subject: str, body: str) -> bool:
    """Send a short informational email (task assigned, payout sent...).

    Mirrors send_password_reset_email's transport logic. Without SMTP
    configured this is a no-op (the in-app notification is the durable
    copy — offline-first).

    Args:
        recipient: The annotator's email address.
        subject: The email subject line.
        body: Plain-text body.

    Returns:
        True when sent, False on failure or when SMTP is not set.
    """
    if not mail_enabled():
        return False

    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(
                SMTP_HOST, SMTP_PORT, timeout=10
            ) as server:
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                if SMTP_STARTTLS:
                    server.starttls()
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        return True
    except OSError:
        logger.exception(
            "Notification email to %s could not be sent.", recipient
        )
        return False
