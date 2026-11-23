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
from flask_wtf.csrf import ValidationError, validate_csrf
from mailer import send_project_inquiry_notification
from models import Payment, utc_now
from routes import dashboard_endpoint_for_role
from sqlalchemy import select
from flask import flash, jsonify

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


@public_bp.route("/contact", methods=["GET", "POST"])
@public_bp.route("/inquiry", methods=["GET", "POST"])
def enterprise_inquiry():
    """Capture enterprise project scoping inquiries and partner outreach.

    Accepts inquiries from AI research teams, foundation labs, and enterprise
    companies requesting model evaluation, adversarial red-teaming, or African
    multilingual datasets.

    Supports both standard form submission and AJAX JSON requests.
    """
    if request.method == "GET":
        return redirect(url_for("public.landing") + "#inquiry")

    # Parse request data (JSON or Form)
    data = {}
    if request.is_json:
        data = request.get_json() or {}
    else:
        csrf_val = request.form.get("csrf_token") or request.headers.get("X-CSRFToken", "")
        try:
            if csrf_val:
                validate_csrf(csrf_val)
        except ValidationError:
            pass
        data = request.form.to_dict()

    full_name = str(data.get("full_name") or data.get("name") or "").strip()
    work_email = str(data.get("work_email") or data.get("email") or "").strip()
    company_name = str(data.get("company_name") or data.get("organization") or data.get("org") or "").strip()
    project_type = str(data.get("project_type") or data.get("service") or "General Project Scoping").strip()
    timeline = str(data.get("timeline") or "Immediate (< 1 month)").strip()
    project_scope = str(data.get("project_scope") or data.get("message") or "").strip()

    # Handle target_languages which may come as a list or comma-separated string
    target_langs_raw = data.get("target_languages") or data.get("languages") or []
    if isinstance(target_langs_raw, list):
        target_languages = ", ".join(str(l).strip() for l in target_langs_raw if str(l).strip())
    else:
        target_languages = str(target_langs_raw).strip()
    if not target_languages:
        target_languages = "Hausa (active), Yoruba, Igbo, Swahili, Amharic"

    # Validation
    errors = []
    if not full_name or len(full_name) < 2:
        errors.append("Please provide your full name.")
    if not work_email or "@" not in work_email or "." not in work_email:
        errors.append("Please provide a valid work email address.")
    if not company_name or len(company_name) < 2:
        errors.append("Please provide your company or organization name.")
    if not project_scope or len(project_scope) < 10:
        errors.append("Please describe your project requirements in at least 10 characters.")

    is_ajax = request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")

    if errors:
        error_msg = " ".join(errors)
        if is_ajax:
            return jsonify({"status": "error", "message": error_msg}), 400
        flash(error_msg, "danger")
        return redirect(url_for("public.landing") + "#inquiry")

    inquiry_record = {
        "full_name": full_name[:120],
        "work_email": work_email[:160],
        "company_name": company_name[:160],
        "project_type": project_type[:120],
        "target_languages": target_languages[:255],
        "timeline": timeline[:60],
        "project_scope": project_scope[:2000],
    }

    # Audit log entry
    log_event(
        "inquiry.submitted",
        actor_name=full_name,
        entity_type="inquiry",
        detail={
            "company": company_name,
            "email": work_email,
            "project_type": project_type,
            "languages": target_languages,
            "timeline": timeline,
        },
        commit=True,
    )

    # Deliver notification email (or log via fallback)
    send_project_inquiry_notification(inquiry_record)

    success_msg = (
        f"Thank you, {full_name}! Your project brief for {company_name} has been received. "
        "Our technical solutions team will review your specifications and contact you within 24 business hours."
    )

    if is_ajax:
        return jsonify({
            "status": "success",
            "message": success_msg,
            "data": {
                "company": company_name,
                "project_type": project_type,
            }
        }), 200

    flash(success_msg, "success")
    return redirect(url_for("public.landing") + "#inquiry")



@public_bp.route("/robots.txt")
def robots_txt():
    """Serve dynamically configured robots.txt file for search & AI crawlers.

    Allows indexing of public landing, privacy, and sitemap resources
    while explicitly disallowing internal admin, annotator, PM, client,
    and API endpoints. AI search and retrieval bots (GPTBot, PerplexityBot,
    ClaudeBot, OAI-SearchBot) are explicitly accommodated.
    """
    base_url = current_app.config.get("SITE_URL") or request.url_root.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://"):]

    body = f"""# ==============================================================================
# Robots.txt for Brydl AI (Gretek Technologies)
# https://brydl.com
# ==============================================================================

User-agent: *
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/
Disallow: /auth/
Disallow: /login
Disallow: /logout
Disallow: /forgot-password
Disallow: /reset-password
Disallow: /backups/
Disallow: /export/
Disallow: /webhooks/

# Generative & Answer Engine Crawlers (AEO & GEO Discoverability)
User-agent: GPTBot
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/

User-agent: OAI-SearchBot
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/

User-agent: PerplexityBot
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/

User-agent: ClaudeBot
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/

User-agent: Google-Extended
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/

User-agent: Applebot
Allow: /
Allow: /privacy
Allow: /sitemap.xml
Allow: /static/
Disallow: /admin/
Disallow: /annotator/
Disallow: /pm/
Disallow: /staff/
Disallow: /client/
Disallow: /api/
Disallow: /redlab/

# Sitemap location
Sitemap: {base_url}/sitemap.xml
"""
    return Response(body, mimetype="text/plain")


@public_bp.route("/sitemap.xml")
def sitemap_xml():
    """Serve standard XML sitemap listing canonical public URLs."""
    base_url = current_app.config.get("SITE_URL") or request.url_root.rstrip("/")
    if request.headers.get("X-Forwarded-Proto") == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[len("http://"):]

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base_url}/</loc>
    <lastmod>2026-10-01</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{base_url}/privacy</loc>
    <lastmod>2026-09-15</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>"""
    return Response(xml, mimetype="application/xml")


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
