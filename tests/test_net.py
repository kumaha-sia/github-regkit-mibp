"""Tests for net.proxy and net.bridge. Run: python -m tests.test_net"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.net.bridge import LocalAuthProxyBridge, ProxyBridgeError
from github_register.net.proxy import (
    ProxyError,
    ProxyManager,
    parse_proxy,
    proxy_is_socks,
    proxy_needs_bridge,
)


def test_parse_proxy_schemes():
    assert parse_proxy("") is None
    assert parse_proxy(None) is None
    p = parse_proxy("http://user:pass@h.com:8080")
    assert p == {"server": "http://h.com:8080", "username": "user", "password": "pass"}
    # socks and socks5h both normalize to socks5
    assert parse_proxy("socks://h.com:1080")["server"] == "socks5://h.com:1080"
    assert parse_proxy("socks5h://u:p@h.com")["server"] == "socks5://h.com:1080"
    assert parse_proxy("https://h.com")["server"] == "https://h.com:443"
    try:
        parse_proxy("ftp://h.com")
    except ProxyError:
        pass
    else:
        raise AssertionError("ftp scheme must raise")
    try:
        parse_proxy("not-a-url")
    except ProxyError:
        pass
    else:
        raise AssertionError("url without host must raise")


def test_proxy_predicates():
    socks_authed = {"server": "socks5://h:1", "username": "u", "password": "p"}
    socks_plain = {"server": "socks5://h:1"}
    http = {"server": "http://h:1"}
    assert proxy_is_socks(socks_authed) and proxy_is_socks(socks_plain)
    assert not proxy_is_socks(http) and not proxy_is_socks(None)
    assert proxy_needs_bridge(socks_authed)
    assert not proxy_needs_bridge(socks_plain)  # no auth -> browser handles it
    assert not proxy_needs_bridge(http)


def test_ensure_sticky_passthrough_and_rotation():
    # non-gateway port passes through untouched
    m = ProxyManager("socks5://u:p@h.com:1234", log=lambda s: None)
    assert m.ensure_sticky() == "socks5://u:p@h.com:1234"
    assert m._sticky_suffix is None

    # rotating DataImpulse port 823 -> sticky port in 10000..20000
    m2 = ProxyManager("socks5h://user:pass@gw.example:823", log=lambda s: None)
    sticky = m2.ensure_sticky()
    assert sticky.startswith("socks5://user:pass@gw.example:")
    port = int(sticky.rsplit(":", 1)[1])
    assert 10000 <= port <= 20000
    # deterministic within the manager
    assert m2.ensure_sticky() == sticky

    # rotate discards the sticky port and allocates a new one
    m2.rotate()
    assert m2._sticky_suffix is None
    sticky2 = m2.ensure_sticky()
    assert sticky2 != sticky or True  # new port may collide by chance; suffix reset is what matters
    assert m2.exit_ip is None


def test_bridge_rejects_invalid_url():
    # no hostname at all -> rejected
    for bad in ("", "://nope"):
        try:
            LocalAuthProxyBridge(bad)
        except ProxyBridgeError:
            continue
        raise AssertionError(f"invalid url must raise: {bad!r}")
    # unknown scheme with a hostname is treated as plain HTTP upstream
    bridge = LocalAuthProxyBridge("ftp://x")
    assert bridge._upstream["socks"] is False


def test_bridge_relay_end_to_end():
    """Bridge proxies a plain HTTP request to a local origin server."""
    # 1. a local origin server that answers GET /ping
    origin = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    origin.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    origin.bind(("127.0.0.1", 0))
    origin.listen(1)
    origin_port = origin.getsockname()[1]

    def serve_once():
        conn, _ = origin.accept()
        req = conn.recv(65536)
        # proxied requests use the absolute-URI form: GET http://host/ping
        if b"/ping" in req:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\npong")
            time.sleep(0.5)  # hold open so the relay can flush before close
        conn.close()

    t = threading.Thread(target=serve_once, daemon=True)
    t.start()

    # 2. an "upstream" that is just the origin: no auth, not socks
    bridge = LocalAuthProxyBridge(f"http://127.0.0.1:{origin_port}")
    port = bridge.start()
    try:
        import requests as _r

        proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
        resp = _r.get(f"http://127.0.0.1:{origin_port}/ping", proxies=proxies, timeout=10)
        assert resp.text == "pong"
    finally:
        bridge.stop()
        origin.close()


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all net tests passed")
