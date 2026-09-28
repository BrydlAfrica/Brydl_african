"""Public marketing pages (no authentication required).

The landing page is the only public route in the app: it tells visitors
what Brydl does and routes signed-in users straight to their dashboard.
Everything else in the product stays behind role walls.
"""

import json
import os

from flask import (
    Blueprint,
    Response,
    current_app,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

import paystack as paystack_gateway
from audit import log_event
from config import BASE_DIR
from extensions import db
from models import Payment, utc_now
from routes import dashboard_endpoint_for_role
from sqlalchemy import select

public_bp = Blueprint("public", __name__)

# Web-app manifest: makes Brydl installable ("Add to Home screen") on
# annotator phones. Icons live in static/brand/ next to the logo set.
MANIFEST = {
    "name": "Brydl Dashboard",
    "short_name": "Brydl",
    "description": "AI data labelling for African teams",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#173363",
    "theme_color": "#173363",
    "icons": [
        {
            "src": "/static/brand/logo-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/static/brand/logo-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
    ],
}


@public_bp.route("/")
def landing():
    """Render the public landing page (or redirect signed-in users).

    Authenticated visitors never see the marketing page again — they
    are bounced straight to their role's dashboard, so the landing page
    is purely the front door for new/anonymous visitors.

    Returns:
        The rendered landing template for anonymous visitors, otherwise
        a redirect to the caller's role dashboard.
    """
    if current_user.is_authenticated:
        return redirect(url_for(dashboard_endpoint_for_role(
            current_user.role)))
    return render_template("landing.html")


@public_bp.route("/manifest.webmanifest")
def manifest():
    """Serve the PWA web-app manifest.

    Returns:
        The manifest JSON with its dedicated mimetype.
    """
    return Response(
        json.dumps(MANIFEST, indent=2) + "\n",
        mimetype="application/manifest+json",
    )


@public_bp.route("/sw.js")
def service_worker():
    """Serve the service worker from the root scope (required for a
    root-scope worker; a file under /static/ could only control
    /static/ pages).

    Served with no-cache so worker updates roll out on the next visit
    after each deploy.

    Returns:
        The service-worker JavaScript.
    """
    path = os.path.join(BASE_DIR, "static", "sw.js")
    with open(path, "r", encoding="utf-8") as handle:
        body = handle.read()
    return Response(
        body,
        mimetype="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@public_bp.route("/offline")
def offline():
    """Branded offline fallback (precached by the service worker).

    Deliberately public and data-free: the worker serves it for ANY
    failed navigation.

    Returns:
        The rendered offline template.
    """
    return render_template("offline.html")


@public_bp.route("/privacy")
def privacy():
    """Render the NDPR data-protection notice (public, no auth).

    Clients and workers both need the data-handling statement before
    trusting the platform with source data or personal details, so it
    is a plain public page linked from the footer.
    """
    return render_template("privacy.html")


@public_bp.route("/webhooks/paystack", methods=["POST"])
def paystack_webhook():
    """Settle Paystack Transfers webhooks (server-to-server, no auth).

    Trust model: the raw body's HMAC-SHA512 signature
    (x-paystack-signature, keyed with the secret key) is the ONLY
    authentication — an invalid signature gets a 400 and nothing is
    read. Valid events settle ONE pending payment by transfer
    reference; every other event or state is accepted with 200 and
    ignored, so Paystack retries are naturally idempotent and a
    re-delivered webhook can never double-settle or resurrect a row.
    """
    raw_body = request.get_data()
    secret = current_app.config.get("PAYSTACK_SECRET_KEY", "")
    signature = request.headers.get("x-paystack-signature")
    if not paystack_gateway.verify_webhook_signature(
        raw_body, signature, secret
    ):
        return ("", 400)
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ("", 200)
    if not isinstance(event, dict):
        return ("", 200)
    event_name = str(event.get("event") or "")
    if event_name not in (
        paystack_gateway.TRANSFER_SUCCESS_EVENT,
        paystack_gateway.TRANSFER_FAILED_EVENT,
    ):
        return ("", 200)
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    reference = str(data.get("reference") or "")
    if not reference:
        return ("", 200)
    payment = db.session.scalar(
        select(Payment).where(Payment.paystack_reference == reference)
    )
    if payment is None or payment.status != "pending":
        # Unknown reference or already settled: acknowledge, change
        # nothing (idempotent under Paystack's at-least-once retries).
        return ("", 200)
    if event_name == paystack_gateway.TRANSFER_SUCCESS_EVENT:
        payment.status = "paid"
        payment.payment_method = "paystack"
        payment.paid_at = utc_now()
    else:
        payment.status = "failed"
    try:
        log_event(
            "payout.webhook",
            actor_name="paystack",
            entity_type="payment",
            entity_id=payment.id,
            detail={"event": event_name, "reference": reference},
            commit=True,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Webhook settlement failed for reference %s.", reference
        )
        return ("", 500)
    return ("", 200)
