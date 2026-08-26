"""Webhook notifications for job completion.

Supports:
- Generic webhook (POST JSON with bearer token)
- Telegram Bot API (sendMessage)
- Discord webhook (POST JSON)

Configure via config.json:
  notify_url: "https://api.telegram.org/bot<token>/sendMessage"
  notify_token: "<telegram_chat_id>"  (for Telegram)

  or:
  notify_url: "https://discord.com/api/webhooks/<id>/<token>"
  notify_token: ""  (not needed for Discord)

  or generic:
  notify_url: "https://your-server.com/hook"
  notify_token: "bearer-secret"
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


def send_notification(
    url: str,
    token: str,
    message: str,
    ok_count: int = 0,
    fail_count: int = 0,
    total: int = 0,
    accounts_file: str = "",
) -> bool:
    """Send a job-completion notification. Returns True on success."""
    if not url:
        return False

    try:
        # Telegram Bot API
        if "api.telegram.org" in url:
            chat_id = token or ""
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            return resp.ok

        # Discord webhook
        if "discord.com/api/webhooks" in url:
            resp = requests.post(
                url,
                json={"content": message},
                timeout=10,
            )
            return resp.ok

        # Generic webhook
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.post(
            url,
            json={
                "message": message,
                "ok": ok_count,
                "fail": fail_count,
                "total": total,
                "accounts_file": accounts_file,
            },
            headers=headers,
            timeout=10,
        )
        return resp.ok

    except Exception as exc:
        log.warning("notification failed: %s", exc)
        return False


def format_job_message(
    ok: int, fail: int, total: int, accounts_file: str = "", error: str = ""
) -> str:
    """Format a human-readable job completion message."""
    if error:
        return f"\u26a0\ufe0f <b>GitHub Register</b> — job error\n{error}"
    emoji = "\u2705" if fail == 0 else "\u26a0\ufe0f"
    lines = [
        f"{emoji} <b>GitHub Register</b> — job complete",
        f"  OK: <b>{ok}</b> / {total}",
        f"  FAIL: <b>{fail}</b> / {total}",
    ]
    if accounts_file:
        lines.append(f"  File: <code>{accounts_file}</code>")
    return "\n".join(lines)
