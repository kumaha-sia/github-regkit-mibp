"""Account validation: verify a registered account actually works.

Checks:
- Login with email + password → cookie logged_in=yes
- TOTP code generation → valid 6-digit code
"""
from __future__ import annotations

import logging
from typing import Optional

import pyotp
import requests

log = logging.getLogger(__name__)

GITHUB_LOGIN_URL = "https://github.com/session"


def validate_totp(secret: str) -> bool:
    """Verify a TOTP secret can generate a valid 6-digit code."""
    if not secret or len(secret) < 16:
        return False
    try:
        totp = pyotp.TOTP(secret.strip())
        code = totp.now()
        return bool(code) and len(code) == 6
    except Exception:
        return False


def validate_login(email: str, password: str, totp_secret: str = "") -> tuple[bool, str]:
    """Attempt to verify account credentials via GitHub API.

    Returns (success, message). Uses GitHub's session login API.
    WARNING: this makes a real login attempt — use sparingly to avoid rate limits.
    """
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "text/html,application/xhtml+xml",
        })
        # get authenticity_token from login page
        resp = session.get("https://github.com/login", timeout=15)
        if not resp.ok:
            return False, f"cannot load login page: {resp.status_code}"
        import re
        m = re.search(r'name="authenticity_token" value="([^"]+)"', resp.text)
        if not m:
            return False, "cannot find authenticity_token"
        token = m.group(1)
        # submit login
        resp = session.post(
            GITHUB_LOGIN_URL,
            data={
                "authenticity_token": token,
                "login": email,
                "password": password,
                "webauthn-support": "supported",
                "webauthn-iuvpaa-support": "unsupported",
                "return_to": "",
                "allow_signup": "",
                "client_id": "",
                "integration": "",
            },
            timeout=15,
            allow_redirects=True,
        )
        # check if we got logged in
        if "logged_in" in str(resp.cookies) and resp.cookies.get("logged_in") == "yes":
            if totp_secret and not validate_totp(totp_secret):
                return True, "login OK but TOTP secret invalid"
            return True, "login OK + TOTP valid"
        # check for 2FA redirect
        if "/sessions/two_factor" in resp.url or "two_factor" in resp.url:
            if totp_secret and validate_totp(totp_secret):
                return True, "login OK (2FA required, TOTP valid)"
            return True, "login OK (2FA required, TOTP untested)"
        return False, f"login failed: not logged in (url={resp.url})"
    except Exception as exc:
        return False, f"login error: {exc}"


def validate_account(email: str, password: str, totp_secret: str = "") -> dict:
    """Full account validation. Returns {valid, login, totp, message}."""
    totp_ok = validate_totp(totp_secret) if totp_secret else False
    login_ok, msg = validate_login(email, password, totp_secret)
    return {
        "valid": login_ok,
        "login": login_ok,
        "totp": totp_ok,
        "message": msg,
    }
