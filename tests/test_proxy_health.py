"""Tests for proxy_health SQLite-backed blacklist. Run: python -m tests.test_proxy_health"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.proxy_health import (
    add_to_blacklist,
    init,
    is_blacklisted,
    purge_expired,
)
from github_register.storage.sqlite import SqliteStorage


def _tmp_storage():
    return SqliteStorage(Path(tempfile.mkdtemp()) / "ph.db")


def test_init_unbound_is_noop():
    init(None)
    assert is_blacklisted("1.2.3.4") is False
    add_to_blacklist("1.2.3.4")  # must not raise
    purge_expired()  # must not raise


def test_blacklist_roundtrip():
    s = _tmp_storage()
    init(s)
    assert is_blacklisted("9.9.9.9") is False
    add_to_blacklist("9.9.9.9")
    assert is_blacklisted("9.9.9.9") is True
    assert is_blacklisted("") is False  # empty ip is always clean


def test_purge_expired():
    s = _tmp_storage()
    init(s)
    add_to_blacklist("10.0.0.1")
    # backdate the entry
    conn = s._conn()
    with conn:
        conn.execute(
            "UPDATE proxy_blacklist SET blocked_at = ? WHERE ip = '10.0.0.1'",
            (time.time() - 100_000,),
        )
    purge_expired()
    assert is_blacklisted("10.0.0.1") is False


def test_re_add_upserts():
    s = _tmp_storage()
    init(s)
    add_to_blacklist("8.8.8.8")
    add_to_blacklist("8.8.8.8")  # second add must not duplicate or error
    assert is_blacklisted("8.8.8.8") is True


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all proxy_health tests passed")
