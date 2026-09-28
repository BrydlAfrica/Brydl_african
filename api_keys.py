"""Machine API key helpers (Phase 10).

Issue-format and hashing for project-scoped Bearer keys. Keys are
generated with secrets (CSPRNG), carry a recognizable ``brydl_``
prefix (so a leaked credential is identifiable in a scan), and are
stored only as sha256 — the same show-once contract as client-portal
links and temporary passwords.
"""

import hashlib
import secrets

KEY_PREFIX = "brydl_"
# Display form stored alongside the hash: prefix + first 4 key chars.
PREFIX_CHARS = 11


def generate_key() -> str:
    """Return a fresh API key: ``brydl_`` + 32 urlsafe bytes."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    """Return the sha256 hex digest of the raw key (storage form).

    Args:
        raw: The key exactly as issued (with the brydl_ prefix).

    Returns:
        The 64-character lowercase hex digest.
    """
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def display_prefix(raw: str) -> str:
    """Return the safe display form of a key (never the whole key)."""
    return (raw[:PREFIX_CHARS] + "…") if raw else ""
