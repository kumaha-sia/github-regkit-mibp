"""Tempik disposable mail client.

Tempik is a self-hosted disposable mail server on Cloudflare Workers + D1.
API: https://tempik.webkarya.net/api
No auth needed — anonymous sessions.

Flow:
  1. GET  /api/session              → { sessionId }
  2. POST /api/inboxes              → { address }  (header: x-session-id)
  3. GET  /api/inboxes/:addr/messages → [ { id, subject, body, from_address, received_at } ]
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

import requests

from .profiles import extract_github_code


class TempikError(RuntimeError):
    pass


class TempikClient:
    def __init__(self, api_base: str = "https://tempik.webkarya.net/api", domains: str = "webkarya.net"):
        self.api_base = api_base.rstrip("/")
        self.domains = [d.strip() for d in domains.split(",") if d.strip()]
        self.session = requests.Session()
        self.session_id: Optional[str] = None
        self._email: Optional[str] = None

    def _get_session(self) -> str:
        """Get or create a Tempik session."""
        if self.session_id:
            return self.session_id
        resp = self.session.get(f"{self.api_base}/session", timeout=10)
        if not resp.ok:
            raise TempikError(f"tempik session failed: HTTP {resp.status_code}")
        data = resp.json()
        sid = data.get("sessionId") or data.get("session_id") or data.get("id")
        if not sid:
            raise TempikError(f"tempik session bad response: {data}")
        self.session_id = str(sid)
        return self.session_id

    def create_mailbox(self, domain: str = "") -> tuple[str, str]:
        """Create a new mailbox. Returns (email, session_id).

        The session_id is used as order_id for compatibility with the runner.
        """
        sid = self._get_session()
        target_domain = domain or (self.domains[0] if self.domains else "")
        if not target_domain:
            raise TempikError("no domain configured for tempik")
        resp = self.session.post(
            f"{self.api_base}/inboxes",
            json={"domain": target_domain},
            headers={"x-session-id": sid},
            timeout=10,
        )
        if not resp.ok:
            raise TempikError(f"tempik create mailbox failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        email = data.get("address") or data.get("email")
        if not email:
            raise TempikError(f"tempik mailbox bad response: {data}")
        self._email = email
        return email, sid

    def get_messages(self, address: str) -> list[dict]:
        """Get all messages for an address."""
        sid = self._get_session()
        url = f"{self.api_base}/inboxes/{address}/messages"
        resp = self.session.get(
            url,
            headers={"x-session-id": sid},
            timeout=10,
        )
        if not resp.ok:
            raise TempikError(
                f"tempik get messages failed: HTTP {resp.status_code} "
                f"url={url} body={resp.text[:200]}"
            )
        data = resp.json()
        return data if isinstance(data, list) else []

    def wait_for_code(
        self,
        order_id: str = "",  # first positional (LitensiClient compat) — ignored
        email: str = "",
        timeout: int = 180,
        poll_interval: float = 3.0,
        log: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
        **kwargs,  # absorb any extra keyword args from caller
    ) -> str:
        """Poll for GitHub verification code. Returns 8-digit code.

        Tempik has no minimum poll interval — emails arrive in D1 almost
        instantly via Cloudflare Email Routing.
        """
        address = email or self._email or ""
        if not address:
            raise TempikError("no email address provided for wait_for_code")
        started = time.time()
        while time.time() - started < timeout:
            if cancel_cb and cancel_cb():
                raise TempikError("cancelled while waiting for mail")
            try:
                messages = self.get_messages(address)
                for msg in messages:
                    body = msg.get("body") or msg.get("text") or ""
                    subject = msg.get("subject") or ""
                    full_text = f"{subject}\n{body}"
                    code = extract_github_code(full_text)
                    if code:
                        if log:
                            log(f"[*] tempik: code {code} found in message from {msg.get('from_address', '?')}")
                        return code
            except TempikError:
                raise
            except Exception as exc:
                if log:
                    log(f"[!] tempik poll error: {exc}")
            if log:
                elapsed = int(time.time() - started)
                log(f"[*] tempik: no code yet ({elapsed}s elapsed)")
            time.sleep(poll_interval)
        raise TempikError(f"no GitHub code after {timeout}s")

    def mark_success(self, order_id: str) -> dict:
        """No-op for Tempik — no success/cancel lifecycle."""
        return {}

    def cancel_order(self, order_id: str) -> None:
        """No-op for Tempik — inbox lives until session expires."""
        pass

    @property
    def last_order_id(self) -> str:
        """Compatibility with LitensiClient interface."""
        return self.session_id or ""
