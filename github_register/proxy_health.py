"""Proxy health check and IP blacklist management.

Before starting a job, test proxy IPs and skip ones that are DataDome-flagged.
Maintains a persistent blacklist in .datadome-blacklist.json.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import requests

from .config import Config

_BLACKLIST_FILE = Path(".datadome-blacklist.json")
_BLACKLIST_EXPIRY_SEC = 3600 * 6  # 6 hours — IPs may get unblocked


def _load_blacklist() -> dict[str, float]:
    """Load the IP blacklist: {ip: timestamp_blocked}."""
    try:
        if _BLACKLIST_FILE.is_file():
            data = json.loads(_BLACKLIST_FILE.read_text(encoding="utf-8"))
            # purge expired entries
            now = time.time()
            return {ip: ts for ip, ts in data.items() if now - ts < _BLACKLIST_EXPIRY_SEC}
    except Exception:
        pass
    return {}


def _save_blacklist(blacklist: dict[str, float]) -> None:
    try:
        _BLACKLIST_FILE.write_text(json.dumps(blacklist, indent=2), encoding="utf-8")
    except Exception:
        pass


def is_blacklisted(ip: str) -> bool:
    """Check if an IP is in the blacklist."""
    if not ip:
        return False
    bl = _load_blacklist()
    return ip in bl


def add_to_blacklist(ip: str) -> None:
    """Add an IP to the blacklist."""
    if not ip:
        return
    bl = _load_blacklist()
    bl[ip] = time.time()
    _save_blacklist(bl)


def check_ip_clean(ip: str, proxy_url: str = "") -> bool:
    """Quick check if an IP can reach GitHub without DataDome block.

    Tests by loading github.com homepage (no proxy needed for the check itself
    if the IP is the direct exit; uses proxy if provided).
    Returns True if the IP is clean, False if blocked.
    """
    if not ip:
        return False
    if is_blacklisted(ip):
        return False
    try:
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        resp = requests.get(
            "https://github.com/",
            proxies=proxies,
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code == 403:
            return False
        text = resp.text[:2000].lower() if resp.text else ""
        if "access is temporarily restricted" in text:
            add_to_blacklist(ip)
            return False
        if "we detected unusual activity" in text:
            add_to_blacklist(ip)
            return False
        return True
    except Exception:
        return False  # can't verify — assume clean to avoid false negatives
