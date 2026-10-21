"""Fernet encryption helpers for RedLab client API keys.

Client-facing LLM endpoints require an API key (``sk-...`` or Bearer
token). RedLab stores those keys Fernet-encrypted at rest — plaintext
keys never hit disk, logs, or error emails. The key comes from the
``REDLAB_FERNET_KEY`` or ``REDLAB_FERNET_KEYS`` environment variable.
When multiple comma-separated keys are configured, ``MultiFernet``
enables seamless zero-downtime key rotation: encryption uses the
primary (first) key, while decryption falls back across all listed
keys in order. When unset locally we derive a deterministic key from
Flask's SECRET_KEY so a fresh dev clone works without extra setup.

Public helpers:

* :func:`encrypt_secret` — encrypt a plaintext secret to a Fernet
  token for storage.
* :func:`decrypt_secret` — decrypt a stored token; raises
  :class:`SecretDecryptionError` on bad ciphertext (never leaks the
  raw key into exception messages).
* :func:`rotate_secret` — re-encrypt a token using the primary key
  during key rotation.
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
from typing import Optional, Union

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

# Cache the Fernet/MultiFernet instance per-process.
_fernet: Optional[Union[Fernet, MultiFernet]] = None


class SecretDecryptionError(Exception):
    """Raised when a stored ciphertext cannot be decrypted.

    Deliberately carries no plaintext or ciphertext in its string
    form — callers may log the exception safely without fear of
    leaking a key.
    """


def _resolve_keys() -> list[bytes]:
    """Return the raw Fernet keys as a list of bytes.

    Resolution order:

    1. ``REDLAB_FERNET_KEYS`` (comma-separated keys for rotation).
    2. ``REDLAB_FERNET_KEY`` (single key or comma-separated).
    3. Derivation from Flask's SECRET_KEY (local-dev fallback).
    """
    raw = (
        os.environ.get("REDLAB_FERNET_KEYS", "").strip()
        or os.environ.get("REDLAB_FERNET_KEY", "").strip()
    )
    if raw:
        keys = [k.strip().encode("ascii") for k in raw.split(",") if k.strip()]
        if keys:
            return keys

    # Dev fallback: derive from SECRET_KEY
    logger.warning(
        "REDLAB_FERNET_KEY not set; deriving key from SECRET_KEY for "
        "local development only. Set REDLAB_FERNET_KEY in production."
    )
    from config import _load_secret_key  # local import to avoid cycles
    digest = hashlib.sha256(_load_secret_key().encode()).digest()
    return [base64.urlsafe_b64encode(digest)]


def _get_fernet() -> Union[Fernet, MultiFernet]:
    """Return a cached MultiFernet or Fernet instance bound to the keys."""
    global _fernet
    if _fernet is None:
        keys = _resolve_keys()
        fernets = [Fernet(k) for k in keys]
        if len(fernets) == 1:
            _fernet = fernets[0]
        else:
            _fernet = MultiFernet(fernets)
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
        logger.warning("RedLab secret decryption failed (bad token).")
        raise SecretDecryptionError(
            "Unable to decrypt stored secret; key may have rotated."
        ) from None
    except Exception as exc:  # noqa: BLE001 — last-resort sanitisation
        logger.warning("RedLab secret decryption raised %s.",
                       type(exc).__name__)
        raise SecretDecryptionError(
            "Unable to decrypt stored secret."
        ) from None


def rotate_secret(ciphertext: Optional[str]) -> str:
    """Re-encrypt a ciphertext with the current primary key.

    Returns the new ciphertext string.
    """
    if not ciphertext:
        return ""
    f = _get_fernet()
    if isinstance(f, MultiFernet):
        try:
            rotated = f.rotate(ciphertext.encode("ascii"))
            return rotated.decode("ascii")
        except Exception:
            # Fall back to decrypt + encrypt
            plain = decrypt_secret(ciphertext)
            return encrypt_secret(plain)
    return ciphertext


def mask_key_fingerprint(plaintext: str) -> str:
    """Return a short, non-reversible fingerprint for UI display.

    Format: ``"<prefix>...<last4>"`` — e.g. ``"sk-...3f2a"``.
    """
    if not plaintext or not isinstance(plaintext, str):
        return "***"
    stripped = plaintext.strip()
    if len(stripped) < 8:
        return "***"
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
