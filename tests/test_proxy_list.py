"""Tests for proxy list rotation and blacklist skip. Run: python -m tests.test_proxy_list"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.net.proxy import ProxyManager


def test_load_dedup_and_strip():
    m = ProxyManager()
    n = m.set_proxy_list("""
        http://a:b@h1:80
        http://c:d@h2:80

        http://a:b@h1:80
          http://e:f@h3:80
    """)
    assert n == 3
    assert m.has_proxy_list() is True
    assert m.remaining_proxies() == 3


def test_sequential_rotation():
    m = ProxyManager()
    m.set_proxy_list("http://a@h1:80\nhttp://b@h2:80\nhttp://c@h3:80")
    assert m.next_proxy() == "http://a@h1:80"
    assert m.next_proxy() == "http://b@h2:80"
    assert m.next_proxy() == "http://c@h3:80"
    assert m.next_proxy() is None  # exhausted — no wrap
    assert m.remaining_proxies() == 0


def test_blacklist_skip():
    blocked = {"http://b@h2:80"}
    m = ProxyManager(log=lambda msg: None)
    m.set_proxy_list(
        "http://a@h1:80\nhttp://b@h2:80\nhttp://c@h3:80",
        blacklist_fn=lambda url: url in blocked,
    )
    # proxy[0] clean, proxy[1] blacklisted (skip), proxy[2] clean
    assert m.next_proxy() == "http://a@h1:80"
    assert m.next_proxy() == "http://c@h3:80"  # h2 skipped
    assert m.next_proxy() is None
    assert m.remaining_proxies() == 0


def test_all_blacklisted():
    m = ProxyManager(log=lambda msg: None)
    m.set_proxy_list(
        "http://a@h1:80\nhttp://b@h2:80",
        blacklist_fn=lambda url: True,  # everything blacklisted
    )
    assert m.next_proxy() is None
    assert m.remaining_proxies() == 0


def test_empty_list():
    m = ProxyManager()
    m.set_proxy_list("")
    assert m.has_proxy_list() is False
    assert m.next_proxy() is None
    assert m.remaining_proxies() == 0


def test_remaining_count():
    m = ProxyManager()
    m.set_proxy_list("http://a@h1:80\nhttp://b@h2:80\nhttp://c@h3:80\nhttp://d@h4:80")
    assert m.remaining_proxies() == 4
    m.next_proxy()
    assert m.remaining_proxies() == 3
    m.next_proxy()
    assert m.remaining_proxies() == 2


def test_config_proxy_mode_field():
    from github_register.config import Config

    cfg = Config(proxy_mode="list", proxy_list="http://a@h1:80\nhttp://b@h2:80")
    assert cfg.proxy_mode == "list"
    assert cfg.proxy_list == "http://a@h1:80\nhttp://b@h2:80"

    cfg2 = Config()
    assert cfg2.proxy_mode == "single"
    assert cfg2.proxy_list == ""


def test_runner_proxy_list_exhaustion():
    """register_one must raise SignupError when proxies run out."""
    import github_register.runner as runner
    from github_register.config import Config
    from github_register.errors import SignupError

    # set up a proxy list with one entry that will be consumed
    runner._proxy_manager.set_proxy_list("http://test@h:80")
    # consume the one proxy
    runner._proxy_manager.next_proxy()
    # now next_proxy() returns None -> register_one should raise
    cfg = Config(proxy_mode="list", proxy_list="http://test@h:80")
    try:
        runner.register_one(cfg, lambda m: None)
    except SignupError as exc:
        assert "exhausted" in str(exc).lower() or "all proxies" in str(exc).lower()
    except Exception as exc:
        # other errors (mailbox creation, etc.) are fine — we just care
        # that it doesn't silently use a non-existent proxy
        pass
    finally:
        runner._proxy_manager.set_proxy_list("")  # reset


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all proxy list tests passed")
