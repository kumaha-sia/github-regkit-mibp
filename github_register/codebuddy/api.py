"""Router API client for CodeBuddy device-code OAuth flow.

Flow:
  1. POST /api/auth/login with password -> auth_token (Set-Cookie)
  2. GET  /api/oauth/codebuddy-intl/device-code -> device_code + verification_uri
  3. User opens verification_uri in browser, authorizes on GitHub
  4. POST /api/oauth/codebuddy-intl/poll -> {success, connection} or {pending}

Headers required by the router (from source code analysis):
  X-Domain: www.codebuddy.ai
  X-No-Authorization: ***
  X-Product: SaaS
  X-Requested-With: XMLHttpRequest
  Cookie: auth_token=eyJ...
  User-Agent: Mozilla/5.0
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

import requests


class RouterError(RuntimeError):
    pass


class RouterClient:
    """Minimal HTTP client for the CodeBuddy router device-code flow."""

    def __init__(self, base_url: str, password: str, log: Optional[Callable[[str], None]] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.password = password
        self.log = log or (lambda msg: None)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "X-Requested-With": "XMLHttpRequest",
        })
        self._auth_token: Optional[str] = None

    # ------------------------------------------------------------------ auth

    def login(self) -> str:
        """Step 1: authenticate with the router to get an auth_token cookie."""
        url = f"{self.base_url}/api/auth/login"
        resp = self.session.post(
            url,
            json={"password": self.password},
            headers={"X-Domain": "www.codebuddy.ai", "X-No-Authorization": "***", "X-Product": "SaaS"},
            timeout=15,
        )
        if not resp.ok:
            raise RouterError(f"router login failed: HTTP {resp.status_code} {resp.text[:200]}")
        # auth_token is set as a cookie
        token = resp.cookies.get("auth_token", "")
        if not token:
            # some routers return it in the JSON body
            try:
                body = resp.json()
                token = body.get("auth_token", "")
            except Exception:
                pass
        if not token:
            raise RouterError("router login succeeded but no auth_token returned")
        self._auth_token = token
        self.session.cookies.set("auth_token", token)
        self.log("[*] router auth: login successful")
        return token

    # --------------------------------------------------------- device code

    def request_device_code(self) -> dict:
        """Step 2: request a device code for CodeBuddy OAuth.

        Returns the full response dict:
          {device_code, verification_uri, user_code, interval, _isCodeBuddy, codeVerifier}
        """
        if not self._auth_token:
            raise RouterError("must call login() before request_device_code()")
        url = f"{self.base_url}/api/oauth/codebuddy-intl/device-code"
        resp = self.session.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Domain": "www.codebuddy.ai",
                "X-No-Authorization": "***",
                "X-Product": "SaaS",
            },
            timeout=15,
        )
        if not resp.ok:
            raise RouterError(
                f"device-code request failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RouterError(f"device-code response is not JSON: {exc}") from exc
        if not data.get("device_code") or not data.get("verification_uri"):
            raise RouterError(f"device-code response missing required fields: {data}")
        self.log(
            f"[*] device code: {data['device_code'][:12]}... "
            f"verification_uri={data['verification_uri'][:60]}"
        )
        return data

    # ------------------------------------------------------------------ poll

    def poll(
        self,
        device_code: str,
        code_verifier: Optional[str] = None,
        interval: int = 5,
        timeout: int = 120,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Step 4/5/7: poll the router until the user authorizes or timeout.

        Returns one of:
          {"success": True,  "connection": {"id": int, "provider": str}}
          {"success": False, "error": str, "pending": True}
          {"success": False, "error": "timeout", "pending": False}
        """
        if not self._auth_token:
            raise RouterError("must call login() before poll()")
        url = f"{self.base_url}/api/oauth/codebuddy-intl/poll"
        body = {
            "deviceCode": device_code,
            "codeVerifier": code_verifier,
            "extraData": None,
        }
        deadline = time.time() + timeout
        attempt = 0
        last_error = ""
        while time.time() < deadline:
            if cancel_cb and cancel_cb():
                return {"success": False, "error": "cancelled", "pending": False}
            attempt += 1
            try:
                resp = self.session.post(
                    url,
                    json=body,
                    headers={
                        "X-Domain": "www.codebuddy.ai",
                        "X-No-Authorization": "***",
                        "X-Product": "SaaS",
                    },
                    timeout=15,
                )
                if resp.ok:
                    data = resp.json()
                    if data.get("success"):
                        conn = data.get("connection", {})
                        self.log(
                            f"[*] poll succeeded after {attempt} attempts: "
                            f"connection_id={conn.get('id')} provider={conn.get('provider')}"
                        )
                        return {"success": True, "connection": conn}
                    if data.get("pending"):
                        last_error = data.get("error", "authorization_pending")
                        self.log(f"[i] poll {attempt}: pending ({last_error})")
                    else:
                        last_error = data.get("error", "unknown error")
                        self.log(f"[!] poll {attempt}: error ({last_error})")
                        # non-pending error = stop polling (e.g. expired_token, access_denied)
                        return {"success": False, "error": last_error, "pending": False}
                else:
                    last_error = f"HTTP {resp.status_code}"
                    self.log(f"[!] poll {attempt}: HTTP {resp.status_code} {resp.text[:100]}")
            except requests.RequestException as exc:
                last_error = str(exc)
                self.log(f"[!] poll {attempt}: network error ({exc})")
            # wait for the next poll interval
            elapsed = min(interval, max(0, deadline - time.time()))
            if elapsed > 0:
                time.sleep(elapsed)
        self.log(f"[!] poll timeout after {attempt} attempts (last error: {last_error})")
        return {"success": False, "error": "timeout", "pending": False}

    # ------------------------------------------------------- convenience

    def get_auth_token(self) -> Optional[str]:
        return self._auth_token
