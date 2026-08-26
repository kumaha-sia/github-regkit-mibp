"""Encryption helpers for sensitive config and account data at rest.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).
The encryption key is derived from the GITHUB_REGISTER_SECRET env var
via PBKDF2HMAC (100k iterations, SHA-256).

If no env var is set, encryption is disabled (plaintext fallback) and
a warning is logged. This keeps the app working out-of-the-box while
encouraging users to set the secret for production use.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_SECRET_ENV = "GITHUB_REGISTER_SECRET"
_DERIVED_SALT = b"github-regkit-v1-salt"  # fixed salt; the env secret is the real secret
_DISABLED = object()  # sentinel: encryption disabled


def _get_key() -> object:
    """Return a Fernet key, or _DISABLED if encryption is off."""
    secret = os.getenv(_SECRET_ENV, "").strip()
    if not secret:
        return _DISABLED
    from cryptography.fernet import Fernet
    kdf = hashlib.pbkdf2_hmac("sha256", secret.encode(), _DERIVED_SALT, 100_000)
    return base64.urlsafe_b64encode(kdf)


_key_cache: object = _DISABLED


def _fernet():
    """Return a Fernet instance or None if disabled."""
    global _key_cache
    if _key_cache is _DISABLED:
        _key_cache = _get_key()
    if _key_cache is _DISABLED:
        return None
    from cryptography.fernet import Fernet
    return Fernet(_key_cache)


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns 'enc:<base64>' or plaintext if disabled."""
    if not plaintext:
        return plaintext
    f = _fernet()
    if f is None:
        return plaintext  # encryption disabled — plaintext fallback
    try:
        return "enc:" + f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    except Exception as exc:
        log.warning("encrypt failed, storing plaintext: %s", exc)
        return plaintext


def decrypt(value: str) -> str:
    """Decrypt a string. Passes through plaintext if not encrypted."""
    if not value or not value.startswith("enc:"):
        return value
    f = _fernet()
    if f is None:
        log.warning("encrypted value found but GITHUB_REGISTER_SECRET not set — returning as-is")
        return value
    try:
        return f.decrypt(value[4:].encode("ascii")).decode("utf-8")
    except Exception as exc:
        log.warning("decrypt failed: %s", exc)
        return value


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted."""
    return bool(value) and value.startswith("enc:")


def is_enabled() -> bool:
    """Check if encryption is enabled."""
    return _fernet() is not None
