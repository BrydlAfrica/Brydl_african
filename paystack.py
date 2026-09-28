"""Paystack Transfers integration (Tier 2 payouts) — thin and safe.

Everything here is deliberately boring: stdlib HTTP, JSON in/out, and
NO function ever raises. Every call returns an ``(ok, payload)``
tuple where ``ok`` is False and ``payload`` is a short human-readable
string on any failure (network, timeout, non-JSON, API error). The
routes layer turns that into flashes and audit events.

Amounts: Paystack Transfers take KOBO (integer). The conversion from
the stored Decimal USD amount happens in routes/admin.py at the
single money boundary (USD -> NGN at the configured rate -> kobo),
so this module stays currency-agnostic.

Secrets: the secret key is passed in per call from config and is
NEVER logged, never embedded in error strings, and never written to
the audit trail.
"""

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

# Webhook event names this app reacts to.
TRANSFER_SUCCESS_EVENT = "transfer.success"
TRANSFER_FAILED_EVENT = "transfer.failed"


def is_enabled(secret_key: str | None) -> bool:
    """Return True when a Paystack secret key is configured."""
    return bool((secret_key or "").strip())


def _request(
    secret_key: str,
    method: str,
    path: str,
    payload: dict | None = None,
    base_url: str = "https://api.paystack.co",
    timeout_seconds: int = 15,
) -> tuple[bool, object]:
    """One authenticated Paystack API call; never raises.

    Args:
        secret_key: The sk_* secret (sent only in the header).
        method: "GET" or "POST".
        path: API path beginning with "/".
        payload: JSON body for POST (None for GET).
        base_url: API root (overridable for the test gateway).
        timeout_seconds: Socket timeout for the call.

    Returns:
        (True, parsed-json-body) on an HTTP 200 with JSON, else
        (False, short-error-string). The secret never appears in any
        error string.
    """
    url = base_url.rstrip("/") + path
    body = None
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Accept": "application/json",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) \
                as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        return False, f"Paystack returned HTTP {exc.code}."
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "Could not reach Paystack (network/timeout)."
    try:
        return True, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, "Paystack returned a non-JSON response."


def list_banks(
    secret_key: str,
    base_url: str = "https://api.paystack.co",
    timeout_seconds: int = 15,
) -> tuple[bool, object]:
    """Fetch Nigerian banks (code + name) for recipient creation.

    Returns:
        (True, list-of-{name, code}) on success, else (False, error).
    """
    ok, payload = _request(
        secret_key, "GET", "/bank?currency=NGN&perPage=200",
        base_url=base_url, timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, payload
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return False, "Paystack returned an unexpected bank list."
    return True, data


def resolve_bank_code(
    banks: list[dict], bank_name: str | None
) -> str | None:
    """Fuzzy-match a stored bank name to a Paystack bank code.

    Match rules, in order: exact casefold, then one name containing
    the other. Guarantees no match for blank input.

    Args:
        banks: The list from list_banks().
        bank_name: The annotator's stored bank name.

    Returns:
        The Paystack bank code, or None when nothing matches.
    """
    needle = (bank_name or "").strip().casefold()
    if not needle:
        return None
    for bank in banks:
        if not isinstance(bank, dict):
            continue
        name = str(bank.get("name") or "")
        if name.casefold() == needle:
            return str(bank.get("code"))
    for bank in banks:
        if not isinstance(bank, dict):
            continue
        name = str(bank.get("name") or "").casefold()
        if name and (name in needle or needle in name):
            return str(bank.get("code"))
    return None


def create_recipient(
    secret_key: str,
    account_name: str,
    account_number: str,
    bank_code: str,
    base_url: str = "https://api.paystack.co",
    timeout_seconds: int = 15,
) -> tuple[bool, object]:
    """Create a transfer recipient for a Nigerian bank account (NUBAN).

    Args:
        account_name: The bank-account holder's name.
        account_number: The plaintext NUBAN (used once in the API
            call; never logged or stored by this module).
        bank_code: The Paystack bank code from list_banks().

    Returns:
        (True, recipient_code) on success, else (False, error-string).
    """
    ok, payload = _request(
        secret_key, "POST", "/transferrecipient",
        {
            "type": "nuban",
            "name": account_name,
            "account_number": account_number,
            "bank_code": bank_code,
            "currency": "NGN",
        },
        base_url=base_url, timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, payload
    data = payload.get("data") if isinstance(payload, dict) else None
    code = (
        data.get("recipient_code")
        if isinstance(data, dict) and isinstance(
            data.get("recipient_code"), str)
        else None
    )
    if not code:
        return False, "Paystack did not return a recipient code."
    return True, code


def initiate_transfer(
    secret_key: str,
    recipient_code: str,
    amount_kobo: int,
    reference: str,
    reason: str,
    base_url: str = "https://api.paystack.co",
    timeout_seconds: int = 15,
) -> tuple[bool, object]:
    """Start a transfer from the Paystack balance.

    Args:
        recipient_code: The stored recipient code.
        amount_kobo: The amount in kobo (integer, NGN x 100).
        reference: The unique idempotency reference we generate.
        reason: The narration shown to the recipient.

    Returns:
        (True, transfer-data-dict with at least status/reference) on
        an accepted transfer, else (False, error-string).
    """
    ok, payload = _request(
        secret_key, "POST", "/transfer",
        {
            "source": "balance",
            "amount": int(amount_kobo),
            "recipient": recipient_code,
            "reference": reference,
            "reason": reason,
            "currency": "NGN",
        },
        base_url=base_url, timeout_seconds=timeout_seconds,
    )
    if not ok:
        return False, payload
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False, "Paystack returned an unexpected transfer reply."
    return True, data


def verify_transfer(
    secret_key: str, reference: str, base_url: str = "https://api.paystack.co"
) -> tuple[bool, str]:
    """Ask Paystack for a transfer's current status by reference.

    Used by the reconciliation job (Phase 10) for payouts still
    'pending' — the webhook remains the primary settlement path; this
    is the network-recovery second opinion.

    Args:
        secret_key: The sk_* secret (header only).
        reference: The transfer reference recorded on the payment.
        base_url: API root (overridable for tests).

    Returns:
        (True, transfer_status_lowercase) on a verified answer —
        e.g. "success", "failed", "processing", "reversed" — else
        (False, short-error-string). Never raises; the secret never
        appears in an error string.
    """
    ok, body = _request(
        secret_key,
        "GET",
        f"/transfer/verify/{urllib.parse.quote(reference, safe='')}",
        base_url=base_url,
    )
    if not ok:
        return False, str(body)
    data = body.get("data") if isinstance(body, dict) else None
    status = str(data.get("status") or "").lower() \
        if isinstance(data, dict) else ""
    if not status:
        return False, "Paystack returned no transfer status."
    return True, status


def verify_webhook_signature(
    raw_body: bytes, signature: str | None, secret_key: str
) -> bool:
    """Check the x-paystack-signature HMAC of a webhook request.

    Paystack signs the RAW request body with HMAC-SHA512 using the
    secret key. Comparison is constant-time.

    Args:
        raw_body: The exact bytes received.
        signature: The x-paystack-signature header value.
        secret_key: The configured secret key.

    Returns:
        True only when the signature is present and valid.
    """
    if not signature or not secret_key:
        return False
    expected = hmac.new(
        secret_key.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())
