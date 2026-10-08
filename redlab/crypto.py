"""Fernet encryption helpers for RedLab client API keys.

Client-facing LLM endpoints require an API key (``sk-...`` or Bearer
token). RedLab stores those keys Fernet-encrypted at rest — plaintext
keys never hit disk, logs, or error emails. The key comes from the
``REDLAB_FERNET_KEY`` environment variable (a urlsafe-base64 32-byte
key produced by ``Fernet.generate_key()``). When unset locally we
derive a deterministic key from Flask's SECRET_KEY so a fresh dev
clone works without extra setup — mirroring the pattern used by
:mod:`security_crypto` for bank numbers, but with a *separate* env var
so the two encryption domains can be rotated independently.

Public helpers:

* :func:`encrypt_secret` — encrypt a plaintext secret to a Fernet
  token for storage.
* :func:`decrypt_secret` — decrypt a stored token; raises
  :class:`SecretDecryptionError` on bad ciphertext (never leaks the
  raw key into exception messages).
* :func:`mask_key_fingerprint` — produce a ``"sk-...3f2a"``-style
  short fingerprint safe to display in the UI and log.

The plaintext key is **never** logged here. The error handler on
:func:`decrypt_secret` is explicitly coded so a malformed token does
not echo the secret back into a traceback.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Cache the Fernet instance per-process — building it is cheap but
# caching avoids repeated hashing / key-parsing on every request.
_fernet: Optional[Fernet] = None


class SecretDecryptionError(Exception):
    """Raised when a stored ciphertext cannot be decrypted.

    Deliberately carries no plaintext or ciphertext in its string
    form — callers may log the exception safely without fear of
    leaking a key.
    """


def _resolve_key() -> bytes:
    """Return the raw Fernet key bytes.

    Resolution order:

    1. ``REDLAB_FERNET_KEY`` env var (verbatim, must be valid
       urlsafe-base64 Fernet key).
    2. Derivation from Flask's SECRET_KEY (local-dev / fresh-deploy
       convenience — never used in production once the env var is
       set; a warning is logged so operators see it).
    """
    raw = os.environ.get("REDLAB_FERNET_KEY", "").strip()
    if raw:
        return raw.encode()
    # Dev fallback: derive from SECRET_KEY (imported lazily to avoid
    # pulling Flask config at module import time).
    logger.warning(
        "REDLAB_FERNET_KEY not set; deriving key from SECRET_KEY for "
        "local development only. Set REDLAB_FERNET_KEY in production."
    )
    from config import _load_secret_key  # local import to avoid cycles
    digest = hashlib.sha256(_load_secret_key().encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance bound to the resolved key."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_resolve_key())
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a client API key (or other secret) for storage.

    Args:
        plaintext: The secret string to encrypt (e.g. ``"sk-abc123..."``).

    Returns:
        A urlsafe ASCII Fernet token (ciphertext).
    """
    if not isinstance(plaintext, str):
        raise TypeError("encrypt_secret expects a str")
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: Optional[str]) -> str:
    """Decrypt a stored Fernet token back to the original secret.

    Args:
        ciphertext: The stored token, or None (returns "").

    Returns:
        The plaintext secret.

    Raises:
        SecretDecryptionError: If the ciphertext is malformed, was
            encrypted with a different key, or is tampered. The
            exception text never includes the secret or token.
    """
    if not ciphertext:
        return ""
    try:
        token = ciphertext.encode("ascii")
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        # Log a fingerprint (never the token) so an ops rotation event
        # is diagnosable without exposing secrets.
        logger.warning("RedLab secret decryption failed (bad token).")
        raise SecretDecryptionError(
            "Unable to decrypt stored secret; key may have rotated."
        ) from None
    except Exception as exc:  # noqa: BLE001 — last-resort sanitisation
        # Wrap ANY other failure so bug paths (UnicodeDecodeError, etc.)
        # cannot leak bytes into a traceback string.
        logger.warning("RedLab secret decryption raised %s.",
                       type(exc).__name__)
        raise SecretDecryptionError(
            "Unable to decrypt stored secret."
        ) from None


def mask_key_fingerprint(plaintext: str) -> str:
    """Return a short, non-reversible fingerprint for UI display.

    Format: ``"<prefix>...<last4>"`` — e.g. ``"sk-...3f2a"`` for a
    typical OpenAI-style key, ``"Bearer ...R8kP"`` for raw Bearer
    tokens. Keys shorter than 8 characters are fully masked
    (``"***"``) so a trivially-short secret is never echoed in full.

    The fingerprint is for operator recognition only — it is NOT a
    cryptographic identifier (do not use for lookup).
    """
    if not plaintext or not isinstance(plaintext, str):
        return "***"
    stripped = plaintext.strip()
    if len(stripped) < 8:
        return "***"
    # Pick a short human prefix (up to the first '-' or 4 chars).
    dash_idx = stripped.find("-")
    if 0 < dash_idx <= 8:
        prefix = stripped[:dash_idx]
    else:
        prefix = stripped[:4]
    last4 = stripped[-4:]
    return f"{prefix}...{last4}"


def reset_fernet_for_tests() -> None:
    """Test helper: drop the cached Fernet instance so a test can
    rotate the env var and rebuild. NOT for production use."""
    global _fernet
    _fernet = None
