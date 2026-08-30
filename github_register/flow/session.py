"""Browser session lifecycle: ctx options, trust-cookie carry-over, context/page.

Everything about how Camoufox is configured and launched, plus the
DataDome trust-cookie bridge between fresh-profile runs. The caller
passes a ProxyManager so this module never touches module-level globals.
"""
from __future__ import annotations

import json
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..browser.selectors import LOGIN_PASS_INPUTS
from ..config import Config
from ..errors import SignupError
from ..net.proxy import (
    ProxyManager,
    parse_proxy,
    proxy_is_socks,
    proxy_needs_bridge,
    socks_exit_ip,
    validate_geoip,
)

ROOT = Path(__file__).resolve().parent.parent.parent

_TRUST_FILE = ROOT / ".datadome-trust.json"
_TRUST_COOKIE_NAMES = {"datadome", "datadome_proxied", "device_id", "_device_id"}

_GITHUB_SESSION_COOKIES = {
    "logged_in", "user_session", "__Host-user_session_same_site",
    "_gh_sess", "dotcom_user",
}


def browser_ctx_options(
    cfg: Config,
    proxy_manager: ProxyManager,
    log=None,
) -> dict:
    """Launch options tuned for DataDome (see 2026 field guides).

    - fresh_profile=True: a NEW browser per account (incognito-like, zero
      cached state — no stacked GitHub logins). The DataDome trust cookie is
      carried over separately via .datadome-trust.json (see save_trust_cookie
      / restore_trust_cookie) so the signup page keeps loading.
    - persistent profile (fresh_profile=False): keeps the whole profile incl.
      the `datadome` cookie (accumulated trust) — but GitHub sessions stack.
    - geoip=True: timezone/locale aligned with the (proxy) exit IP
    - os=host OS: Picasso canvas hash matches the REAL device class we run on
    - headful by default: headless rendering is a Picasso tell

    SOCKS proxies: Camoufox's own geoip probe uses plain 'socks5://' which many
    gateways (DataImpulse: '0x02 connection not allowed by ruleset') reject
    because DNS is resolved locally. For SOCKS we resolve the exit IP ourselves
    via 'socks5h://' and pass geoip=<ip> so Camoufox skips its probe.
    """
    opts = {"headless": cfg.headless, "humanize": True, "geoip": True}
    host_os = platform.system()
    if host_os == "Darwin":
        opts["os"] = "macos"  # canvas/GPU class must match the real machine
    elif host_os == "Linux":
        opts["os"] = "linux"
    elif host_os == "Windows":
        opts["os"] = "windows"
    # sticky session FIRST: one stable exit IP for the whole job — rotating
    # IPs mid-session (DataImpulse default) are an instant DataDome flag
    proxy_url = proxy_manager.ensure_sticky() if (cfg.proxy or "").strip() else ""
    proxy = parse_proxy(proxy_url) if proxy_url else None
    if proxy:
        if proxy_needs_bridge(proxy):
            # Firefox cannot authenticate to SOCKS5 — run a local no-auth HTTP
            # bridge that relays to the authed upstream with remote DNS.
            opts["proxy"] = proxy_manager.ensure_bridge(proxy_url) or proxy
        else:
            opts["proxy"] = proxy
        
        try:
            exit_ip = socks_exit_ip(proxy_url)
            # reject IPv6 — DataDome is stricter with IPv6, and Camoufox
            # geoip works best with IPv4
            if ":" in exit_ip:
                if log:
                    log(f"[!] IPv6 exit IP detected ({exit_ip}) — rotating to get IPv4")
                proxy_manager.rotate()
                raise SignupError("IPv6 exit IP, need IPv4")
            # validate IP is in a known geoip database (Camoufox will fail
            # with "IP not found in database" for obscure ranges)
            country = validate_geoip(exit_ip)
            if not country:
                if log:
                    log(f"[!] IP {exit_ip} not in geoip database — rotating")
                proxy_manager.rotate()
                raise SignupError(f"IP {exit_ip} not in geoip database")
            opts["geoip"] = exit_ip
            proxy_manager.exit_ip = exit_ip  # consumed by trust-cookie IP binding
            proxy_manager.country = country  # consumed by country dropdown selector
            if log:
                log(f"[*] proxy exit IP: {exit_ip} (geoip pinned, sticky, country: {country})")
        except Exception as exc:
            # re-raise so the caller (register_one) can retry with a new IP
            if "IPv6 exit IP" in str(exc) or "not in geoip database" in str(exc):
                raise
            # SSL/connection errors mean the proxy itself is broken — rotate
            msg = str(exc).lower()
            if "ssl" in msg or "wrong_version" in msg or "connection" in msg or "refused" in msg:
                if log:
                    log(f"[!] proxy connection broken ({exc}) — rotating sticky port")
                proxy_manager.rotate()
                raise SignupError(f"proxy connection failed: {exc}")
            opts["geoip"] = False
            proxy_manager.exit_ip = None  # no IP to bind — do NOT restore stale cookies
            if log:
                log(f"[!] proxy exit-IP lookup failed ({exc}); geoip disabled — "
                    f"timezone/locale may mismatch the proxy country. "
                    f"Trust cookie will NOT be restored (IP unknown).")
    if getattr(cfg, "fresh_profile", False):
        # fresh browser per account — no user_data_dir at all
        if log:
            log("[*] fresh profile mode: browser baru tanpa cache (trust DataDome di-clone)")
    elif cfg.browser_profile_dir:
        opts["persistent_context"] = True
        opts["user_data_dir"] = str((ROOT / cfg.browser_profile_dir).resolve())
    return opts


def save_trust_cookie(context, exit_ip: str = "", log=None) -> None:
    """Persist only the DataDome trust cookies, bound to the current exit IP.

    A datadome cookie issued for IP A looks forged when replayed from IP B —
    worse than no cookie at all. We therefore store the exit IP alongside and
    only restore when the IP matches (sticky session keeps it stable in-job).
    """
    try:
        cookies = context.cookies()
        keep = [
            c for c in cookies
            if c.get("name") in _TRUST_COOKIE_NAMES and c.get("domain", "").endswith("github.com")
        ]
        if not keep:
            return
        _TRUST_FILE.write_text(
            json.dumps({
                "cookies": keep,
                "exit_ip": exit_ip or "",
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }),
            encoding="utf-8",
        )
        if log:
            log(f"[*] datadome trust cookie saved ({len(keep)} cookies, ip={exit_ip or 'n/a'})")
    except Exception as exc:
        if log:
            log(f"[i] trust cookie save failed: {exc}")


def restore_trust_cookie(context, exit_ip: str = "", log=None) -> None:
    """Inject persisted DataDome trust cookies — ONLY if the exit IP matches.

    Mismatched IP -> skip silently (a fresh challenge is less suspicious than
    a cookie replayed from the wrong IP). When exit IP is unknown (lookup
    failed), also skip — restoring a stale IP-bound cookie is worse than none.
    """
    try:
        if not _TRUST_FILE.is_file():
            return
        data = json.loads(_TRUST_FILE.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if not cookies:
            return
        bound_ip = data.get("exit_ip") or ""
        # No current exit IP? Don't guess — skip restore entirely
        if not exit_ip:
            if log:
                log("[i] trust cookie skipped (current exit IP is unknown; lookup failed)")
            return
        if bound_ip and exit_ip and bound_ip != exit_ip:
            if log:
                log(f"[i] trust cookie skipped (bound to IP {bound_ip}, current IP {exit_ip})")
            return
        # context.add_cookies requires url OR domain+path
        clean = []
        for c in cookies:
            cc = {k: c.get(k) for k in ("name", "value", "domain", "path",
                                        "expires", "httpOnly", "secure", "sameSite") if c.get(k) is not None}
            if "domain" not in cc or "path" not in cc:
                cc["domain"] = ".github.com"
                cc["path"] = "/"
            clean.append(cc)
        context.add_cookies(clean)
        if log:
            log(f"[*] datadome trust cookie restored ({len(clean)} cookies, ip={bound_ip or 'unbound'})")
    except Exception as exc:
        if log:
            log(f"[i] trust cookie restore failed: {exc}")


def context_and_page(browser):
    """Return (context, page) for BOTH launch modes.

    persistent_context=True -> Camoufox returns a BrowserContext with one page
    fresh launch             -> Camoufox returns a Browser; create a context
                                + page ourselves.
    """
    if hasattr(browser, "cookies"):  # BrowserContext (persistent mode)
        context = browser
        page = context.pages[0] if context.pages else context.new_page()
    else:  # Browser (fresh mode)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
    return context, page


def clean_github_session_cookies(context, log) -> None:
    """Between accounts: drop GitHub login cookies, keep DataDome/trust cookies.

    A persistent profile survives across accounts, so 'logged_in'/'user_session'
    cookies must be cleared to avoid signing INTO the previous account instead
    of signing UP a new one. DataDome (datadome) cookies are kept — they carry
    the anti-bot trust that lets /signup load at all.
    """
    try:
        cookies = context.cookies()
        keep = [c for c in cookies if c.get("name") not in _GITHUB_SESSION_COOKIES]
        if len(keep) == len(cookies):
            return  # nothing to clean
        context.clear_cookies()
        for c in keep:
            try:
                context.add_cookies([c])
            except Exception:
                pass
        log("[*] session cookies cleared (DataDome trust kept)")
    except Exception as exc:
        log(f"[i] cookie cleanup skipped: {exc}")


def logged_in(context) -> bool:
    """Reliable success signal: GitHub sets cookie logged_in=yes on a real session."""
    try:
        for c in context.cookies():
            if c.get("name") == "logged_in" and str(c.get("value", "")).lower() == "yes":
                return True
    except Exception:
        pass
    return False
