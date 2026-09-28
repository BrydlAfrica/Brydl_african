import os as _os
ROOT = _os.environ.get("BRYDL_ROOT") or _os.path.abspath(
    _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
"""Tier 2 suite: Paystack Transfers payouts + the NDPR privacy page.

Covers: the paystack module (signature verification, bank matching,
enable flag), the disabled-feature fallback, the admin payout route
(recipient creation + caching, kobo money boundary, success and
processing outcomes, bank-name mismatch, audit without account
numbers, CSRF), the webhook (signature gate, idempotent settlement,
failed transfers, unknown references) and the public NDPR page.
"""
import hashlib
import hmac
import json
import re
import shutil
import sys
from decimal import Decimal, ROUND_HALF_UP

from app import create_app
from extensions import db
from models import AuditLog, Payment, User, utc_now
import paystack as paystack_mod

app = create_app()

FAILURES = []


def expect(condition, label):
    """Record one granular pass/fail check."""
    if condition:
        print(f"   ok: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}")


def get_csrf(html):
    """Pull the CSRF token out of a rendered form page."""
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
    return m.group(1) if m else None


def login(client, email, password):
    """POST the sign-in form."""
    r = client.get("/login")
    tk = get_csrf(r.get_data(as_text=True))
    return client.post("/login", data={"email": email,
                                       "password": password,
                                       "csrf_token": tk})


shutil.rmtree("instance", ignore_errors=True)
shutil.rmtree("backups", ignore_errors=True)

# ---- Section 1: pure module ----
print("== Section 1: paystack.py unit contract ==")
secret = "sk_test_abcdef1234567890abcdef1234567890"
body = b'{"event": "transfer.success"}'
good_sig = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
expect(paystack_mod.verify_webhook_signature(body, good_sig, secret),
       "webhook: valid signature accepted")
expect(not paystack_mod.verify_webhook_signature(
    body + b" ", good_sig, secret), "webhook: tampered body rejected")
expect(not paystack_mod.verify_webhook_signature(
    body, good_sig, "sk_other"), "webhook: wrong key rejected")
expect(not paystack_mod.verify_webhook_signature(body, None, secret),
       "webhook: missing header rejected")
expect(paystack_mod.resolve_bank_code(
    [{"name": "Guaranty Trust Bank", "code": "058"}],
    "guaranty trust bank") == "058", "banks: casefold exact match")
expect(paystack_mod.resolve_bank_code(
    [{"name": "Zenith Bank", "code": "057"}],
    "Zenith Bank Plc") == "057", "banks: containment match")
expect(paystack_mod.resolve_bank_code(
    [{"name": "Zenith Bank", "code": "057"}], "OPay") is None,
    "banks: no match -> None")
expect(paystack_mod.resolve_bank_code([], None) is None,
       "banks: blank input -> None")
expect(not paystack_mod.is_enabled("") and paystack_mod.is_enabled(
    "sk_x"), "enabled flag honours the key")

# ---- fixtures ----
with app.app_context():
    admin = User(name="Ada Admin", email="pay-admin@brydl.ng",
                 role="admin")
    admin.set_password("admin-pass-1")
    worker = User(name="Ngozi Work", email="ngozi@brydl.ng",
                  role="annotator", terms_version="1.0",
                  bank_name="Guaranty Trust Bank",
                  bank_account_name="Ngozi Work",
                  bank_account_type="savings")
    worker.set_password("worker-pass-1")
    worker.set_bank_account("0123456789")
    db.session.add_all([admin, worker])
    db.session.flush()
    admin_id, worker_id = admin.id, worker.id
    pay = Payment(annotator_id=worker_id, amount=Decimal("3.20"),
                  period_start=utc_now(), period_end=utc_now(),
                  tasks_count=8, status="pending")
    db.session.add(pay)
    db.session.commit()
    payment_id = pay.id

admin_c = app.test_client()
login(admin_c, "pay-admin@brydl.ng", "admin-pass-1")
app.config["PAYSTACK_SECRET_KEY"] = secret
app.config["PAYSTACK_BASE_URL"] = "https://api.paystack.test"
RATE = app.config["USD_TO_NGN_RATE"]
EXPECTED_KOBO = int(
    (Decimal("3.20") * RATE).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    ) * 100
)

calls = {"recipient": None, "transfer": None}


def fake_list_banks(sk, base_url="x", timeout_seconds=15):
    """Stub bank list."""
    assert sk == secret and base_url == "https://api.paystack.test"
    return True, [{"name": "Guaranty Trust Bank", "code": "058"}]


def fake_create_recipient(sk, name, number, code, base_url="x",
                          timeout_seconds=15):
    """Stub recipient creation; remember what was sent."""
    calls["recipient"] = {"name": name, "number": number, "code": code}
    return True, "RCPT_88"


def fake_transfer(sk, recipient, kobo, reference, reason,
                  base_url="x", timeout_seconds=15):
    """Stub transfer; remember the payload."""
    calls["transfer"] = {"recipient": recipient, "kobo": kobo,
                         "reference": reference, "reason": reason}
    return True, {"status": "pending", "reference": reference,
                  "transfer_code": "TRF_1"}

# ---- Section 2: disabled feature falls back ----
print("== Section 2: feature disabled ==")
app.config["PAYSTACK_SECRET_KEY"] = ""
r = admin_c.post(
    f"/admin/payments/{payment_id}/paystack",
    data={"csrf_token": get_csrf(admin_c.get(
        "/admin/payments").get_data(as_text=True))},
    follow_redirects=True)
page = r.get_data(as_text=True)
expect("Paystack is not configured" in page,
       "disabled key flashes the CSV fallback notice")
with app.app_context():
    expect(db.session.get(Payment, payment_id).status == "pending",
           "payment untouched while disabled")
app.config["PAYSTACK_SECRET_KEY"] = secret

# ---- Section 3: payout happy path ----
print("== Section 3: payout via Paystack ==")
paystack_mod.list_banks = fake_list_banks
paystack_mod.create_recipient = fake_create_recipient
paystack_mod.initiate_transfer = fake_transfer
page = admin_c.get("/admin/payments").get_data(as_text=True)
expect("Pay via Paystack" in page, "payout button shows when enabled")
r = admin_c.post(f"/admin/payments/{payment_id}/paystack", data={
    "csrf_token": get_csrf(page)}, follow_redirects=True)
page = r.get_data(as_text=True)
expect("processing and will be confirmed" in page,
       "processing transfer flashes the info notice")
with app.app_context():
    p = db.session.get(Payment, payment_id)
    expect(p.status == "pending" and p.paystack_reference
           and p.paystack_reference.startswith(f"brydl-p{payment_id}-"),
           "reference stored, row still pending (webhook settles)")
    w = db.session.get(User, worker_id)
    expect(w.paystack_recipient_code == "RCPT_88",
           "recipient code cached on the annotator")
expect(calls["recipient"]["number"] == "0123456789"
       and calls["recipient"]["code"] == "058"
       and calls["recipient"]["name"] == "Ngozi Work",
       "recipient call carried the decrypted NUBAN + bank code")
expect(calls["transfer"]["kobo"] == EXPECTED_KOBO
       and calls["transfer"]["recipient"] == "RCPT_88",
       f"transfer amount exact ({EXPECTED_KOBO} kobo at the fx rate)")
with app.app_context():
    audit = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "payout.paystack",
        AuditLog.entity_id == payment_id))
    audit_detail = json.dumps(audit.detail or {}) if audit else ""
expect(audit is not None, "payout.paystack audited")
expect("0123456789" not in audit_detail,
       "audit detail carries no account number")

# ---- Section 4: webhook settles ----
print("== Section 4: webhook settlement ==")
reference = calls["transfer"]["reference"]
event = {"event": "transfer.success",
         "data": {"reference": reference, "status": "success"}}
raw = json.dumps(event).encode()
sig = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
r = app.test_client().post(
    "/webhooks/paystack", data=raw,
    headers={"x-paystack-signature": sig})
expect(r.status_code == 200, "valid webhook accepted")
with app.app_context():
    p = db.session.get(Payment, payment_id)
    expect(p.status == "paid" and p.payment_method == "paystack"
           and p.paid_at is not None,
           "webhook flipped the payout to paid")
with app.app_context():
    webhook_audit = db.session.scalar(db.select(AuditLog).where(
        AuditLog.action == "payout.webhook")) is not None
expect(webhook_audit, "payout.webhook audited")
# re-delivery must be a no-op
r = app.test_client().post(
    "/webhooks/paystack", data=raw,
    headers={"x-paystack-signature": sig})
expect(r.status_code == 200, "webhook re-delivery acknowledged")
r2 = app.test_client().post(
    "/webhooks/paystack", data=b"{}",
    headers={"x-paystack-signature": hmac.new(
        secret.encode(), b"{}", hashlib.sha512).hexdigest()})
expect(r2.status_code == 200, "unrelated event ignored (200)")

# invalid signature -> 400, nothing settles
pay2 = Payment(annotator_id=worker_id, amount=Decimal("1.00"),
               period_start=utc_now(), period_end=utc_now(),
               tasks_count=2, status="pending")
with app.app_context():
    db.session.add(pay2)
    db.session.commit()
    pay2_id, pay2_ref = pay2.id, f"brydl-p{pay2.id}-bad"
    db.session.execute(
        db.update(Payment).where(Payment.id == pay2_id)
        .values(paystack_reference=pay2_ref))
    db.session.commit()
ev_fail = {"event": "transfer.failed", "data": {"reference": pay2_ref}}
raw_fail = json.dumps(ev_fail).encode()
r = app.test_client().post(
    "/webhooks/paystack", data=raw_fail,
    headers={"x-paystack-signature": "deadbeef"})
expect(r.status_code == 400, "bad signature -> 400")
with app.app_context():
    expect(db.session.get(Payment, pay2_id).status == "pending",
           "unsigned webhook changed nothing")
raw_ok = json.dumps(ev_fail).encode()
sig_ok = hmac.new(secret.encode(), raw_ok, hashlib.sha512).hexdigest()
r = app.test_client().post(
    "/webhooks/paystack", data=raw_ok,
    headers={"x-paystack-signature": sig_ok})
expect(r.status_code == 200, "valid failure webhook accepted")
with app.app_context():
    expect(db.session.get(Payment, pay2_id).status == "failed",
           "failed transfer marks the payout failed")
r = app.test_client().post(
    "/webhooks/paystack", data=json.dumps(
        {"event": "transfer.success",
         "data": {"reference": "brydl-unknown-1"}}).encode(),
    headers={"x-paystack-signature": hmac.new(
        secret.encode(),
        json.dumps({"event": "transfer.success",
                    "data": {"reference": "brydl-unknown-1"}}).encode(),
        hashlib.sha512).hexdigest()})
expect(r.status_code == 200, "unknown reference acknowledged, ignored")

# ---- Section 5: guard rails ----
print("== Section 5: guard rails ==")
with app.app_context():
    pay3 = Payment(annotator_id=worker_id, amount=Decimal("0.50"),
                   period_start=utc_now(), period_end=utc_now(),
                   tasks_count=1, status="pending")
    db.session.add(pay3)
    db.session.commit()
    pay3_id = pay3.id
    bad = User(name="Bad Bank", email="badbank@brydl.ng",
               role="annotator", terms_version="1.0",
               bank_name="Mystery Money Bank",
               bank_account_name="Bad Bank",
               bank_account_type="savings")
    bad.set_password("bad-pass-1")
    bad.set_bank_account("9999999999")
    db.session.add(bad)
    db.session.commit()
    bad_id = bad.id
    pay4 = Payment(annotator_id=bad_id, amount=Decimal("0.50"),
                   period_start=utc_now(), period_end=utc_now(),
                   tasks_count=1, status="pending")
    db.session.add(pay4)
    db.session.commit()
    pay4_id = pay4.id
r = admin_c.post(f"/admin/payments/{pay3_id}/paystack", data={},
                 follow_redirects=True)
expect("Security check failed" in r.get_data(as_text=True),
       "payout without CSRF bounced")
r = admin_c.post(
    f"/admin/payments/{pay4_id}/paystack",
    data={"csrf_token": get_csrf(admin_c.get(
        "/admin/payments").get_data(as_text=True))},
    follow_redirects=True)
expect("not recognised by Paystack" in r.get_data(as_text=True),
       "unknown bank name flashes the CSV instruction")
with app.app_context():
    u = db.session.get(User, bad_id)
    expect(u.paystack_recipient_code is None
           and db.session.get(Payment, pay4_id).status == "pending",
           "no recipient created, payout untouched")
anon = app.test_client()
expect(anon.post(f"/admin/payments/{payment_id}/paystack",
                 data={}).status_code in (302, 400),
       "anonymous payout POST refused")
page = anon.get("/privacy")
expect(page.status_code == 200, "NDPR page public")
body = page.get_data(as_text=True)
expect("NDPR" in body and "encrypted at rest" in body
       and "Your rights" in body and "Kano" in body,
       "NDPR notice covers controller, security and rights")
expect("Data Protection (NDPR)" in anon.get("/").get_data(as_text=True),
       "footer links the notice from the landing page")

print()
if FAILURES:
    print(f"PAYSTACK TESTS FAILED ({len(FAILURES)}):")
    for label in FAILURES:
        print(f"  - {label}")
    sys.exit(1)
print("ALL PAYSTACK TESTS PASSED")
