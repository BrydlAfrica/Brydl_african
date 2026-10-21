"""Async outbound HTTP client for RedLab targets.

Wraps ``httpx.AsyncClient`` to call client LLM endpoints with:

* Per-request timeouts (connect=5s, read=20s).
* One retry with 1s backoff on 429 / 5xx responses.
* In-memory Fernet decryption of the target API key.
* Strict SSRF gating and connection-IP pinning (anti-rebinding) via
  :func:`redlab.ssrf.resolve_and_validate_endpoint_url`.
* A clean error-taxonomy (machine-readable codes) so Phase-5 breach
  detection and Phase-8 logs don't depend on httpx error types.
* A regex that redacts anything that looks like a leaked ``sk-…``
  key or ``Bearer …`` token from any error / response returned to
  the caller, so internal log aggregation can never accidentally
  persist a key an LLM spat out.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import ssl as _ssl
from typing import Any, Optional

import httpcore
from httpcore._backends.anyio import AnyIOBackend
import httpx

from redlab.crypto import decrypt_secret
from redlab.ssrf import resolve_and_validate_endpoint_url

logger = logging.getLogger(__name__)

# -- Timeouts & Limits -----------------------------------------------------
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB response size cap

# -- Retry policy ----------------------------------------------------------
MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# -- Error codes returned to callers ---------------------------------------
# (ok, content, error_code): ok=True  → content is assistant text
#                             ok=False → content is a short message,
#                                        error_code is one of these
ERR_ENDPOINT_BLOCKED = "endpoint_blocked"
ERR_CLIENT_AUTH = "client_auth_failed"
ERR_RATE_LIMITED = "client_rate_limited"
ERR_UNREACHABLE = "client_unreachable"
ERR_BAD_RESPONSE = "client_bad_response"
ERR_INTERNAL = "internal_error"

# -- Key-redaction regex ----------------------------------------------------
# Skims any "sk-<base64>" (OpenAI-style, ≥20 chars) or "Bearer <token>"
# sequence out of text returned to the caller. The sk-pattern matches
# letters/digits/underscore/dashes, which covers most vendor key
# formats.
_SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE)


class PinnedNetworkBackend(AnyIOBackend):
    """Network backend that connects directly to a pre-validated IP,
    defeating DNS-rebinding (TOCTOU) attacks while preserving TLS SNI."""

    def __init__(self, pinned_ip: str):
        super().__init__()
        self.pinned_ip = pinned_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Any] = None,
    ):
        return await super().connect_tcp(
            self.pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class PinnedIPTransport(httpx.AsyncHTTPTransport):
    """Async HTTP transport pinned to a pre-validated IP address."""

    def __init__(
        self,
        pinned_ip: str,
        ssl_context: Optional[_ssl.SSLContext] = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if ssl_context is None:
            ssl_context = _ssl.create_default_context()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=PinnedNetworkBackend(pinned_ip),
        )


def _redact(text: str) -> str:
    """Return `text` with any API-key-shaped substrings masked."""
    if not text:
        return text
    text = _SK_RE.sub("sk-...redacted", text)
    text = _BEARER_RE.sub("Bearer ...redacted", text)
    return text


def build_history(messages) -> list[dict[str, str]]:
    """Convert ORM Message rows (or dicts) into an OpenAI ``messages`` list.

    Accepts any iterable of either:
      * RedLab Message model instances (``.role``/``.content`` attrs)
      * plain dicts with ``role`` / ``content`` keys.

    System messages are inserted only when the model calls for them;
    Phase 3/4 client calls send ``[user, assistant, ..., user]``.
    """
    out: list[dict[str, str]] = []
    for m in messages:
        if hasattr(m, "role"):
            role = m.role
            content = m.content or ""
        else:
            role = m.get("role", "user")
            content = m.get("content", "") or ""
        out.append({"role": str(role), "content": str(content)})
    return out


def _mock_enabled() -> bool:
    return os.environ.get("REDLAB_MOCK_MODEL", "").strip().lower() in (
        "1", "true", "yes",
    )


async def call_client_model(
    target,
    history_messages: list[dict[str, str]],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> tuple[bool, str, Optional[str]]:
    """Call the client model endpoint and return the assistant content.

    Args:
        target: A ``redlab.models.Target`` instance (needs
            ``.endpoint``, ``.api_key_ciphertext`` and optionally
            ``.model_name``). May be ``None`` if mock model is
            forced by ``REDLAB_MOCK_MODEL=1``.
        history_messages: OpenAI-form messages list (from
            :func:`build_history`).
        client: Optional pre-built ``httpx.AsyncClient`` (used by
            tests to inject a mock transport). If ``None`` a
            short-lived client is constructed.

    Returns:
        ``(ok, content, error_code)``:
          * ``ok=True`` → content is the assistant text, error_code None.
          * ``ok=False`` → content is a redacted short error message,
            error_code is one of the ERR_* constants.
    """
    from redlab.mockmodel import mock_respond

    # --- Mock short-circuit -------------------------------------------------
    endpoint = getattr(target, "endpoint", "") or ""
    if _mock_enabled() or not endpoint or endpoint.lower() == "mock":
        unsafe, content = mock_respond(
            history_messages,
            seed_modifier=getattr(target, "id", "mock") or "mock",
        )
        # Mock path still goes through redaction (defence in depth).
        return True, _redact(content), None

    # --- SSRF & DNS Pinning ------------------------------------------------
    ok, pinned_ip, reason = resolve_and_validate_endpoint_url(endpoint)
    if not ok or not pinned_ip:
        logger.warning("RedLab connector: endpoint %s blocked (%s)",
                       endpoint, reason)
        return False, f"endpoint blocked: {reason}", ERR_ENDPOINT_BLOCKED

    # --- Build request -----------------------------------------------------
    try:
        api_key = decrypt_secret(getattr(target, "api_key_ciphertext", ""))
    except Exception:  # noqa: BLE001
        logger.exception("RedLab connector: cannot decrypt API key")
        return False, "api key decrypt failed", ERR_INTERNAL

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    model_name = getattr(target, "model_name", "") or "default"
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": history_messages,
        "temperature": 0.7,
    }

    def _clear_key():
        nonlocal api_key
        api_key = None
        if "Authorization" in headers:
            headers["Authorization"] = "Bearer [REDACTED]"

    own_client = client is None
    if own_client:
        timeout = httpx.Timeout(
            5.0, connect=CONNECT_TIMEOUT, read=READ_TIMEOUT,
        )
        transport = PinnedIPTransport(pinned_ip)
        client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
    try:
        for attempt in range(MAX_RETRIES + 1):
            try:
                req = client.build_request(
                    "POST", endpoint, json=payload, headers=headers,
                )
                resp = await client.send(req, stream=True)
            except httpx.TimeoutException as exc:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                return (
                    False,
                    _redact(f"model timed out: {type(exc).__name__}"),
                    ERR_UNREACHABLE,
                )
            except httpx.ConnectError as exc:
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                    continue
                return (
                    False,
                    _redact(f"model unreachable: {type(exc).__name__}"),
                    ERR_UNREACHABLE,
                )
            except httpx.HTTPError as exc:
                return (
                    False,
                    _redact(f"request failed: {type(exc).__name__}"),
                    ERR_UNREACHABLE,
                )

            if resp.status_code == 200:
                break

            # Error responses: read a short snippet for classification, then close stream
            try:
                err_bytes = b""
                async for chunk in resp.aiter_bytes():
                    err_bytes += chunk
                    if len(err_bytes) >= 500:
                        break
                snippet = err_bytes.decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                snippet = ""
            finally:
                await resp.aclose()

            if resp.status_code == 401 or resp.status_code == 403:
                # Don't retry on auth failures; redact any response body
                return False, "client rejected credentials", ERR_CLIENT_AUTH
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                continue
            # Non-retryable error: map to code.
            if resp.status_code == 429:
                return False, "client rate limited", ERR_RATE_LIMITED
            if 400 <= resp.status_code < 500:
                return (
                    False,
                    _redact(
                        f"client rejected request ({resp.status_code}): "
                        f"{snippet}"
                    ),
                    ERR_BAD_RESPONSE,
                )
            return (
                False,
                _redact(f"client returned {resp.status_code}: {snippet}"),
                ERR_BAD_RESPONSE,
            )
        else:  # pragma: no cover
            return False, "retries exhausted", ERR_UNREACHABLE

        # Enforce 2 MB maximum response size cap with streaming abort
        content_length = resp.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_RESPONSE_BYTES:
                    await resp.aclose()
                    return (
                        False,
                        "client response exceeded 2MB limit",
                        ERR_BAD_RESPONSE,
                    )
            except ValueError:
                pass

        chunks: list[bytes] = []
        total_bytes = 0
        try:
            async for chunk in resp.aiter_bytes():
                total_bytes += len(chunk)
                if total_bytes > MAX_RESPONSE_BYTES:
                    await resp.aclose()
                    return (
                        False,
                        "client response exceeded 2MB limit",
                        ERR_BAD_RESPONSE,
                    )
                chunks.append(chunk)
        finally:
            await resp.aclose()

        body_bytes = b"".join(chunks)

        # Parse body: accept both OpenAI-style
        # {choices:[{message:{content:"..."}}]} and a simple
        # {"content":"..."} form (mock-compatible).
        try:
            data = _json.loads(body_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return (
                False,
                _redact(f"client returned non-JSON: {body_bytes[:200].decode('utf-8', errors='replace')}"),
                ERR_BAD_RESPONSE,
            )
        content = None
        try:
            if (isinstance(data, dict)
                    and "choices" in data
                    and isinstance(data["choices"], list)
                    and data["choices"]):
                msg = data["choices"][0].get("message") or {}
                content = msg.get("content")
            if content is None and isinstance(data, dict):
                content = data.get("content") or data.get("completion")
        except Exception:  # noqa: BLE001
            content = None
        if not isinstance(content, str):
            return (
                False,
                _redact("client response missing content field"),
                ERR_BAD_RESPONSE,
            )
        return True, _redact(content), None
    finally:
        _clear_key()
        if own_client:
            await client.aclose()
