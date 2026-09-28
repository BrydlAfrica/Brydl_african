"""App-layer encryption for annotator bank account numbers.

Bank numbers are the highest-value PII in the system. They are stored
as Fernet ciphertext (AES-128-CBC + HMAC under the hood), encrypted at
write time and decrypted ONLY for the bank-batch payout export — the
Team page shows ``bank_account_last4`` and never touches the key
operation. The key comes from the BANK_ENC_KEY environment variable
(Fernet key, ``Fernet.generate_key()`` output) or, when unset, is
derived deterministically from SECRET_KEY, so local development and
fresh deploys need no extra setup. Legacy plaintext rows (written
before this module existed) still decrypt: reads that fail the token
check return the stored value as-is, and the next save re-encrypts.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from config import BANK_ENC_KEY
from config import _load_secret_key

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return the process-wide Fernet instance (built once).

    Returns:
        A Fernet bound to BANK_ENC_KEY or the SECRET_KEY-derived key.
    """
    global _fernet
    if _fernet is None:
        if BANK_ENC_KEY:
            key = BANK_ENC_KEY.encode()
        else:
            digest = hashlib.sha256(
                _load_secret_key().encode()
            ).digest()
            key = base64.urlsafe_b64encode(digest)
        _fernet = Fernet(key)
    return _fernet


def encrypt_bank_number(plain: str) -> str:
    """Encrypt a bank account number for storage.

    Args:
        plain: The validated 10-digit NUBAN.

    Returns:
        A Fernet token (ASCII-safe ciphertext).
    """
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_bank_number(stored: str | None) -> str | None:
    """Decrypt a stored bank number, tolerating legacy plaintext.

    Args:
        stored: The ciphertext (or legacy plaintext) column value.

    Returns:
        The plaintext number, the legacy value unchanged, or None.
    """
    if not stored:
        return None
    try:
        return _get_fernet().decrypt(stored.encode()).decode()
    except InvalidToken:
        # Legacy plaintext row from before encryption was enabled.
        logger.info("Legacy plaintext bank number re-read; not yet"
                    " re-encrypted.")
        return stored
