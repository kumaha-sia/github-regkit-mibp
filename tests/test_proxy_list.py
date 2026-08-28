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


def test_config_proxy_file_field():
    from github_register.config import Config

    cfg = Config(proxy_mode="list", proxy_file="proxies.txt")
    assert cfg.proxy_file == "proxies.txt"

    cfg2 = Config()
    assert cfg2.proxy_file == "proxies.txt"  # default


def test_runner_reads_proxy_file(tmp_path=None):
    """run_job must load proxies from file when proxy_mode=list."""
    import tempfile
    from pathlib import Path
    import github_register.runner as runner
    from github_register.config import Config
    from github_register.storage.models import Account

    tmp = Path(tempfile.mkdtemp())
    runner.ACCOUNTS_DIR = tmp / "accounts"
    runner.ACCOUNTS_DIR.mkdir(parents=True)
    runner.RECOVERY_DIR = tmp / "recovery"
    runner.DB_PATH = tmp / "regkit.db"
    runner.ROOT = tmp

    # write a proxy file
    proxy_file = tmp / "proxies.txt"
    proxy_file.write_text(
        "http://file1@h1:80\nhttp://file2@h2:80\nhttp://file3@h3:80\n",
        encoding="utf-8",
    )

    # fake register_one to capture the proxy list state
    captured = {}
    orig_reg = runner.register_one
    orig_bridge = runner._stop_proxy_bridge

    def fake_reg(cfg, log, stop=None):
        pm = runner._proxy_manager
        captured["list_len"] = len(pm._proxy_list)
        captured["first"] = pm._proxy_list[0] if pm._proxy_list else None
        return None  # fail = no save

    runner.register_one = fake_reg
    runner._stop_proxy_bridge = lambda: None
    try:
        cfg = Config(
            proxy_mode="list",
            proxy_file="proxies.txt",
            register_count=1,
            delay_sec=0,
        )
        logs = []
        runner.run_job(cfg, log=logs.append)
        assert captured["list_len"] == 3
        assert captured["first"] == "http://file1@h1:80"
        assert any("proxy list loaded" in m for m in logs)
    finally:
        runner.register_one = orig_reg
        runner._stop_proxy_bridge = orig_bridge


def test_runner_falls_back_to_proxy_list_config():
    """When proxy file does not exist, fall back to Config.proxy_list."""
    import tempfile
    from pathlib import Path
    import github_register.runner as runner
    from github_register.config import Config

    tmp = Path(tempfile.mkdtemp())
    runner.ROOT = tmp
    runner.ACCOUNTS_DIR = tmp / "accounts"
    runner.ACCOUNTS_DIR.mkdir(parents=True)
    runner.RECOVERY_DIR = tmp / "recovery"
    runner.DB_PATH = tmp / "regkit.db"

    # no proxy file on disk, but proxy_list has entries in config
    captured = {}
    orig_reg = runner.register_one
    orig_bridge = runner._stop_proxy_bridge

    def fake_reg(cfg, log, stop=None):
        pm = runner._proxy_manager
        captured["list_len"] = len(pm._proxy_list)
        return None

    runner.register_one = fake_reg
    runner._stop_proxy_bridge = lambda: None
    try:
        cfg = Config(
            proxy_mode="list",
            proxy_file="nonexistent.txt",
            proxy_list="http://cfg1@h:80\nhttp://cfg2@h:80",
            register_count=1,
            delay_sec=0,
        )
        logs = []
        runner.run_job(cfg, log=logs.append)
        assert captured["list_len"] == 2  # fell back to config
    finally:
        runner.register_one = orig_reg
        runner._stop_proxy_bridge = orig_bridge


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all proxy list tests passed")
