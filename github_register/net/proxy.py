"""Proxy URL handling: parsing, sticky-port rotation, exit-IP lookup, geoip.

DataImpulse-style residential gateways rotate the exit IP on every TCP
connection by default (rotating ports 823/824). A browser opens dozens of
parallel connections — mid-session IP changes are an instant anti-bot flag
("same cookie, different countries within seconds").

Fix: use a STICKY port instead. Gateways assign ports 10000–20000 for
sticky SOCKS5 — all connections through the same port exit through the
SAME IP for the session lifetime (~30 min default).
"""
from __future__ import annotations

import secrets
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests

from .bridge import LocalAuthProxyBridge


class ProxyError(RuntimeError):
    pass


def parse_proxy(url: str) -> Optional[dict]:
    """'http(s)/socks5(h)://user:pass@host:port' -> Playwright proxy dict, or None.

    Scheme normalization (Playwright accepts only these):
      socks://   -> socks5://   (bare 'socks' is rejected by Firefox)
      socks5h:// -> socks5://   ('h' variant is a curl/requests-only notation;
                                 Firefox resolves DNS remotely by default)
    """
    url = (url or "").strip()
    if not url:
        return None
    p = urlsplit(url)
    if not p.hostname:
        raise ProxyError(f"invalid proxy url: {url}")
    scheme = (p.scheme or "http").lower()
    if scheme in ("socks", "socks5h"):
        scheme = "socks5"
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise ProxyError(f"unsupported proxy scheme: {p.scheme}:// (use http/socks5)")
    port = p.port or (1080 if scheme.startswith("socks") else (443 if scheme == "https" else 80))
    proxy = {"server": f"{scheme}://{p.hostname}:{port}"}
    if p.username:
        proxy["username"] = p.username
        proxy["password"] = p.password or ""
    return proxy


def proxy_is_socks(proxy: Optional[dict]) -> bool:
    return bool(proxy) and str(proxy.get("server", "")).startswith("socks")


def proxy_needs_bridge(proxy: Optional[dict]) -> bool:
    """Firefox rejects authed SOCKS5; bridge it locally."""
    return bool(proxy) and str(proxy.get("server", "")).startswith("socks") and proxy.get("username")


def socks_exit_ip(url: str, timeout: int = 12) -> str:
    """Resolve the proxy exit IP using the 'socks5h://' scheme (remote DNS).

    Gateways that reject IP-based connections under a 'ruleset' when the
    client resolves DNS locally (plain socks5://) die with
    '0x02: Connection not allowed by ruleset' — so we look the exit IP up
    ourselves over socks5h and hand it to the browser via geoip=<ip>.
    """
    p = urlsplit(url.strip())
    scheme = "socks5h" if (p.scheme or "socks").lower().startswith("socks") else (p.scheme or "http")
    auth = f"{p.username}:{p.password}@" if p.username else ""
    port = p.port or 1080
    proxies = {"http": f"{scheme}://{auth}{p.hostname}:{port}",
               "https": f"{scheme}://{auth}{p.hostname}:{port}"}
    last_exc: Exception | None = None
    # sticky ports can take a few seconds to warm up (allocate the IP) — retry
    for attempt in range(2):
        for check_url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.co/ip"):
            try:
                resp = requests.get(check_url, proxies=proxies, timeout=20)
                ip = (resp.text or "").strip()
                if resp.ok and ip:
                    return ip
            except Exception as exc:
                last_exc = exc
        if attempt == 0:
            time.sleep(3)  # give the sticky session a moment to warm up
    raise ProxyError(f"socks exit-IP lookup failed: {last_exc}")


def validate_geoip(ip: str) -> bool:
    """Check if an IP is in a public geoip database (fast, no proxy needed).

    Browsers with fingerprint spoofing fail with 'IP not found in database'
    for obscure IP ranges. This pre-check avoids launching a browser that
    will immediately error.
    """
    for api in (
        f"https://ipapi.co/{ip}/json/",
        f"https://ipinfo.io/{ip}/json",
    ):
        try:
            resp = requests.get(api, timeout=8)
            if resp.ok:
                data = resp.json()
                # success = has country code
                if data.get("country") or data.get("country_code"):
                    return True
        except Exception:
            continue
    return False


class ProxyManager:
    """Owns the proxy lifecycle for one job: sticky port, bridge, exit IP.

    Replaces the module-level globals (_sticky_suffix, _last_exit_ip,
    _bridge) so two jobs could in principle run side by side and the
    whole thing becomes unit-testable.
    """

    def __init__(self, proxy_url: str = "", log: Optional[Callable[[str], None]] = None):
        self.proxy_url = (proxy_url or "").strip()
        self.log = log or (lambda msg: None)
        self._sticky_suffix: Optional[str] = None
        self.exit_ip: Optional[str] = None
        self._bridge: Optional[LocalAuthProxyBridge] = None

    # ------------------------------------------------------------ sticky port

    def ensure_sticky(self) -> str:
        """Switch a rotating gateway endpoint to a sticky one, or pass through.

        Rotating = port 823 (HTTP) / 824 (SOCKS5); sticky = ports
        10000-20000. We pick a random sticky port per manager so each job
        gets a fresh stable IP. The port is deterministic within the job.
        """
        p = urlsplit(self.proxy_url)
        port = p.port or 0
        # only switch known rotating ports
        if port in (823, 824):
            if self._sticky_suffix is None:
                self._sticky_suffix = str(10000 + int(secrets.token_hex(4), 16) % 10001)
                self.log(
                    f"[*] sticky proxy port: {self._sticky_suffix}"
                    " (IP stabil ~30 menit, DataImpulse)"
                )
            scheme = (p.scheme or "socks5").lower()
            if scheme in ("socks", "socks5h"):
                scheme = "socks5"
            auth = f"{p.username}:{p.password}@" if p.username else ""
            return f"{scheme}://{auth}{p.hostname}:{self._sticky_suffix}"
        return self.proxy_url  # already sticky or non-gateway — untouched

    # ---------------------------------------------------------------- bridge

    def _stop_bridge(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None

    def ensure_bridge(self, proxy_url: str) -> Optional[dict]:
        """Start the local auth bridge when the upstream needs it.

        Returns the Playwright proxy dict (pointing at 127.0.0.1) or None
        when the upstream speaks to the browser directly.
        """
        proxy = parse_proxy(proxy_url)
        if not proxy_needs_bridge(proxy):
            return None
        if self._bridge is None:
            self._bridge = LocalAuthProxyBridge(proxy_url)
            self._bridge.start()
            self.log(
                f"[*] local auth bridge 127.0.0.1:{self._bridge.port} -> "
                f"{proxy['server'].split('://', 1)[1]}"
            )
        return self._bridge.browser_proxy()

    # --------------------------------------------------------------- rotation

    def rotate(self) -> None:
        """Discard a blocked sticky port and allocate a new one."""
        self._stop_bridge()
        self._sticky_suffix = None
        self.exit_ip = None

    def stop(self) -> None:
        self._stop_bridge()

    # --------------------------------------------------------------- exit IP

    def resolve_exit_ip(self, proxy_url: str) -> str:
        ip = socks_exit_ip(proxy_url)
        self.exit_ip = ip  # consumed by trust-cookie IP binding
        return ip

    def clear_exit_ip(self) -> None:
        self.exit_ip = None  # no IP to bind — do NOT restore stale cookies
