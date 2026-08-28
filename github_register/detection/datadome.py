"""Anti-bot detection: DataDome hard blocks, rate limits, challenge pages.

All functions take a Playwright `page` (or `context`) and answer ONE
question: what did the anti-bot layer serve us? Raising happens in the
flow layer; detection stays pure so it is trivially testable.
"""
from __future__ import annotations

import re

from ..browser.human import page_text
from ..errors import GitHubRateLimited

# DataDome hard block: full-page interstitial, no checkbox to solve.
DATADOME_HARD_BLOCK_MARKERS = (
    "access is temporarily restricted",
    "we detected unusual activity",
    "your access is restricted",
    "you have been temporarily blocked",
    # Indonesian localization of the DataDome block page
    "akses dibatasi untuk sementara",
    "kami mendeteksi aktivitas yang tidak biasa",
    "ada robot di jaringan",
)

# GitHub's own secondary rate limit wording.
RATE_LIMIT_MARKERS = (
    "secondary rate limit",
    "too many requests",
    "you have exceeded a secondary rate limit",
    "please wait a few minutes before you try again",
)

# GitHub risk engine device interstitial.
RISK_CHECK_MARKERS = (
    "login to continue",
    "log in with a different device",
)

_IP_RE = re.compile(r"IP[:\s]+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")


def is_hard_block(page) -> bool:
    """DataDome hard block: 'Access is temporarily restricted' — no checkbox to solve."""
    text = ""
    try:
        text = page_text(page).lower()
    except Exception:
        pass
    return any(marker in text for marker in DATADOME_HARD_BLOCK_MARKERS)


def reject_blocked(page) -> None:
    """Raise SignupBlocked when GitHub's risk-engine interstitial is shown."""
    from ..errors import SignupBlocked

    text = page_text(page).lower()
    for marker in RISK_CHECK_MARKERS:
        if marker in text:
            raise SignupBlocked(f"github risk check: {marker}")


def raise_if_rate_limited(page) -> None:
    text = page_text(page).lower()
    if any(marker in text for marker in RATE_LIMIT_MARKERS):
        raise GitHubRateLimited(
            "GitHub secondary rate limit reached. Stop the job and wait before trying again; "
            "do not rotate/retry this limit."
        )


def challenge_hint(page) -> str:
    """Return a short description of the anti-bot page GitHub served, or ''."""
    if "captcha-delivery" in (page.url or ""):
        return "DataDome challenge (geo.captcha-delivery.com)"
    try:
        html = page.content()[:2000]
    except Exception:
        html = ""
    if "captcha-delivery" in html or "id=\"cmsg\"" in html:
        return "DataDome challenge page"
    if "cf-chl" in html:
        return "Cloudflare challenge"
    return ""


def blocked_ip(page) -> str:
    """Extract the blocked IP DataDome printed on the page/URL, or ''."""
    try:
        m = _IP_RE.search(page_text(page)[:2000])
        if m:
            return m.group(1)
    except Exception:
        pass
    try:
        m = _IP_RE.search(page.url or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def log_block_ip(page, log, exit_ip: str = "") -> None:
    """When a hard block is detected, log the blocked IP and current proxy exit IP.

    DataDome pages often include the blocked IP in the page text or URL.
    This helps diagnose whether the proxy is leaking the real IP.
    """
    found = blocked_ip(page)
    proxy_ip = exit_ip or "(unknown — proxy exit IP not resolved)"
    if found:
        log(f"[!] DataDome blocked IP: {found} | proxy exit IP: {proxy_ip}")
        if found == proxy_ip:
            log("[i] blocked IP matches proxy exit — proxy is active but this IP is flagged")
        elif proxy_ip and proxy_ip != "(unknown)":
            log("[!] BLOCKED IP != PROXY EXIT — proxy may be leaking! Check proxy config")
        else:
            log("[!] blocked IP looks like your real IP — proxy is NOT active")
    else:
        log(f"[!] DataDome hard block detected | proxy exit IP: {proxy_ip}")


def try_click_datadome(page, log) -> None:
    """Best-effort click on the DataDome checkbox iframe (headed mode)."""
    try:
        for frame in page.frames:
            if "captcha-delivery" in (frame.url or ""):
                for sel in (
                    "#ddv1-test-tracking",
                    "input[type='checkbox']",
                    "[id*='checkbox']",
                    "label",
                ):
                    loc = frame.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=3000)
                        log("[*] clicked DataDome checkbox")
                        return
                # no checkbox: click somewhere in the challenge frame to trigger it
                try:
                    frame.locator("body").click(timeout=3000)
                    log("[*] poked DataDome challenge frame")
                except Exception:
                    pass
                return
    except Exception:
        pass
