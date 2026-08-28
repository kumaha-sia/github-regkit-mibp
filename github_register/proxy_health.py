"""Proxy IP blacklist backed by SQLite (was .datadome-blacklist.json).

The runner calls `is_blacklisted(ip)` before launching a browser on a
resolved exit IP, and `add_to_blacklist(ip)` when a DataDome hard block
is detected. Entries auto-expire after 6 hours — IPs may get unblocked.
"""
from __future__ import annotations

import time

from .storage.sqlite import SqliteStorage

_BLACKLIST_EXPIRY_SEC = 3600 * 6

# shared storage handle — the runner/web server each hold their own
# SqliteStorage; this module takes the one the runner passes in.
_storage: SqliteStorage | None = None


def init(storage: SqliteStorage) -> None:
    """Bind the shared SQLite storage handle."""
    global _storage
    _storage = storage


def is_blacklisted(ip: str) -> bool:
    if not ip or _storage is None:
        return False
    return _storage.blacklist_contains(ip)


def add_to_blacklist(ip: str) -> None:
    if not ip or _storage is None:
        return
    _storage.blacklist_add(ip)


def check_ip_clean(ip: str, proxy_url: str = "") -> bool:
    """Quick check if an IP can reach GitHub without a DataDome block.

    Tests by loading github.com homepage (no proxy needed for the check itself
    if the IP is the direct exit; uses proxy if provided).
    Returns True if the IP is clean, False if blocked.
    """
    if not ip:
        return False
    if is_blacklisted(ip):
        return False
    try:
        import requests as _requests

        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        resp = _requests.get(
            "https://github.com/",
            proxies=proxies,
            timeout=15,
            allow_redirects=True,
        )
        if resp.status_code == 403:
            add_to_blacklist(ip)
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
        return False


def purge_expired() -> None:
    """Drop entries older than the expiry window. Called periodically."""
    if _storage is None:
        return
    _storage.blacklist_purge_expired(ttl_sec=_BLACKLIST_EXPIRY_SEC)
