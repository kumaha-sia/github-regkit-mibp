"""GitHub sign-up automation driven by Camoufox (Firefox anti-detect) + Litensi mail."""
from __future__ import annotations

import json
import hashlib
import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from camoufox.sync_api import Camoufox
import requests

from .config import Config
from .litensi import LitensiClient, LitensiError
from .tempik import TempikClient, TempikError
from .profiles import (
    generate_password,
    generate_username,
    parse_public_profile,
    username_from_email,
)
from .storage.models import Account as AccountRecord, Job as JobRecord
from .storage.sqlite import SqliteStorage
from .crypto import encrypt
from .errors import (
    GitHubRateLimited,
    RegistrationCancelled,
    SignupBlocked,
    SignupError,
)
from .net.bridge import LocalAuthProxyBridge
from .net.proxy import (
    ProxyError,
    ProxyManager,
    parse_proxy as _parse_proxy,
    proxy_is_socks as _proxy_is_socks,
    proxy_needs_bridge as _proxy_needs_bridge,
    socks_exit_ip as _socks_exit_ip,
    validate_geoip as _validate_geoip,
)
from .browser.human import (
    human_click as _human_click,
    human_delay as _human_delay,
    human_fill as _human_fill,
    human_mouse_to_element as _human_mouse_to_element,
    human_random_pause as _human_random_pause,
    human_scroll as _human_scroll,
    page_text as _page_text,
    first as _first,
    fill as _fill,
    raise_if_cancelled as _raise_if_cancelled,
    sleep_with_cancel as _sleep_with_cancel,
    wait_step as _wait_step,
)


def silence_playwright_noise() -> None:
    """Suppress the asyncio 'Task exception was never retrieved' spam.

    Playwright leaves in-flight Channel.send tasks behind when the browser is
    closed mid-operation; asyncio then dumps a TargetClosedError traceback for
    each of them. Harmless noise — filter it at the logging level.
    """
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"
RECOVERY_DIR = ACCOUNTS_DIR / "recovery"
DB_PATH = ROOT / "accounts" / "regkit.db"

# one manager per process — shared by all flows in this module. Thread-safety
# is the same as before (single job thread); the object just replaces globals.
_proxy_manager = ProxyManager()


def _ensure_sticky_proxy(url: str, log=None) -> str:
    return _proxy_manager.ensure_sticky() if url else url


def _stop_proxy_bridge() -> None:
    _proxy_manager.stop()


def _rotate_sticky_proxy() -> None:
    """Discard a blocked sticky port and allocate a new one."""
    _proxy_manager.rotate()


def _get_bridge(proxy_url: str, log=None) -> Optional[dict]:
    """Start (once) and return the local auth bridge's browser proxy dict."""
    return _proxy_manager.ensure_bridge(proxy_url)


def _resolve_exit_ip(proxy_url: str) -> str:
    return _proxy_manager.resolve_exit_ip(proxy_url)


def _save_recovery_per_account(email: str, recovery: str, log) -> None:
    """Store one account's multiline recovery codes in accounts/recovery/.

    Kept for legacy compat; SQLite holds the same data in the account row
    (written atomically by run_job's storage.add).
    """
    if not recovery:
        return
    try:
        RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
        (RECOVERY_DIR / f"{key}.txt").write_text(recovery.strip() + "\n", encoding="utf-8")
        log(f"[*] recovery codes saved for {email}")
    except Exception as exc:
        log(f"[i] recovery codes write failed: {exc}")

_EMAIL_INPUTS = ["#email", "input[name='email']", "input[type='email']"]
_PASSWORD_INPUTS = ["#password", "input[name='password']"]
_USERNAME_INPUTS = ["#login", "input[name='login']"]
_OTP_INPUTS = [
    "#otp",
    "input[name='otp']",
    "input[autocomplete='one-time-code']",
    "#launch-code-0",  # verify page: 8 single-digit boxes launch-code-0..7
]
# The main signup form (NOT the Google/Apple OAuth forms which live in their own <form> tags)
_SIGNUP_FORM = "form[action*='signup']"
_SUBMIT_SELECTORS = [f"{_SIGNUP_FORM} button[type='submit']", "#submit", "button[type='submit']"]



_DATADOME_HARD_BLOCK_MARKERS = (
    "access is temporarily restricted",
    "we detected unusual activity",
    "your access is restricted",
    "you have been temporarily blocked",
    # Indonesian localization of the DataDome block page
    "akses dibatasi untuk sementara",
    "kami mendeteksi aktivitas yang tidak biasa",
    "ada robot di jaringan",
)

_RATE_LIMIT_MARKERS = (
    "secondary rate limit",
    "too many requests",
    "you have exceeded a secondary rate limit",
    "please wait a few minutes before you try again",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _form_validation_hint(page) -> str:
    """Return a concise visible validation error when Create account is disabled."""
    try:
        alerts = page.locator("[role='alert'], .is-error, .error, .flash-error").all()
        messages = []
        for alert in alerts:
            try:
                text = (alert.inner_text(timeout=500) or "").strip()
            except Exception:
                continue
            if text and "may only contain alphanumeric" not in text.lower():
                messages.append(text)
        if messages:
            return " | ".join(messages[:3])[:300]
    except Exception:
        pass
    return ""


def _click_submit(page) -> None:
    """Click the real signup Continue button, never the Google/Apple OAuth buttons.

    The signup page has 3 forms: 2 OAuth (/sessions/social/*) and 1 main
    (action contains 'signup'). Scope the submit click to the main form;
    fall back to legacy selectors for later steps (OTP/preferences pages).
    """
    scoped = page.locator("form[action*='signup'] button[type='submit']").first
    try:
        if scoped.count() and scoped.is_visible() and scoped.is_enabled():
            scoped.click()
            return
    except Exception:
        pass
    _first(page, _SUBMIT_SELECTORS, visible=True).click()


def _reject_blocked(page) -> None:
    """GitHub risk engine may force a 'Login to continue' device interstitial."""
    text = _page_text(page).lower()
    for marker in ("login to continue", "log in with a different device"):
        if marker in text:
            raise SignupBlocked(f"github risk check: {marker}")


def _cancel_order(mail, order_id: str, log) -> None:
    """Cancel a mailbox order. No-op for Tempik (inbox lives until session expires)."""
    if isinstance(mail, TempikClient):
        return  # Tempik has no cancel/free lifecycle
    try:
        mail.set_status(order_id, "CANCELED")
        log(f"[*] litensi order {order_id} canceled")
    except Exception as exc:
        if "CANCEL AFTER" in str(exc):
            log(f"[i] litensi order {order_id} auto-expires (cancel only allowed after 4 min)")
        else:
            log(f"[!] cancel order failed: {exc}")


def _is_hard_block(page) -> bool:
    """DataDome hard block: 'Access is temporarily restricted' — no checkbox to solve."""
    text = ""
    try:
        text = _page_text(page).lower()
    except Exception:
        pass
    return any(marker in text for marker in _DATADOME_HARD_BLOCK_MARKERS)


def _log_block_ip(page, log) -> None:
    """When a hard block is detected, log the blocked IP and current proxy exit IP.

    DataDome pages often include the blocked IP in the page text or URL.
    This helps diagnose whether the proxy is leaking the real IP.
    """
    import re

    blocked_ip = ""
    try:
        text = _page_text(page)[:2000]
        m = re.search(r"IP[:\s]+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", text)
        if m:
            blocked_ip = m.group(1)
    except Exception:
        pass
    if not blocked_ip:
        try:
            m = re.search(r"IP[:\s]+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", page.url or "")
            if m:
                blocked_ip = m.group(1)
        except Exception:
            pass
    proxy_ip = _proxy_manager.exit_ip or "(unknown — proxy exit IP not resolved)"
    if blocked_ip:
        log(f"[!] DataDome blocked IP: {blocked_ip} | proxy exit IP: {proxy_ip}")
        if blocked_ip == proxy_ip:
            log("[i] blocked IP matches proxy exit — proxy is active but this IP is flagged")
        elif proxy_ip and proxy_ip != "(unknown)":
            log("[!] BLOCKED IP != PROXY EXIT — proxy may be leaking! Check proxy config")
        else:
            log("[!] blocked IP looks like your real IP — proxy is NOT active")
    else:
        log(f"[!] DataDome hard block detected | proxy exit IP: {proxy_ip}")


def _raise_if_rate_limited(page) -> None:
    text = _page_text(page).lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        raise GitHubRateLimited(
            "GitHub secondary rate limit reached. Stop the job and wait before trying again; "
            "do not rotate/retry this limit."
        )


def _challenge_hint(page) -> str:
    """Return a short description of the anti-bot page GitHub served, or ''."""
    if "captcha-delivery" in page.url:
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


def _try_click_datadome(page, log) -> None:
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


def _form_ready(page) -> bool:
    sel = ", ".join(_EMAIL_INPUTS)
    try:
        return page.locator(sel).first.is_visible()
    except Exception:
        return False


def _homepage_warmup(page, log, stop=None, dwell: int = 12) -> bool:
    """Browse the GitHub homepage like a human before navigating to /signup.

    Loads the homepage, scrolls, moves mouse, waits for DataDome tags.js to
    set the trust cookie, then checks if the page is usable. Returns True if
    warm-up succeeded (no hard block), False if the IP is hard-blocked.
    """
    log(f"[*] homepage warm-up ({dwell}s) — letting DataDome trust cookie settle")
    # retry homepage load up to 2 times on timeout (network may be slow)
    page_loaded = False
    for load_attempt in range(2):
        try:
            page.goto("https://github.com/", wait_until="domcontentloaded", timeout=90_000)
            page_loaded = True
            break
        except Exception as exc:
            msg = str(exc).lower()
            # NS_ERROR_ABORT / connection failures = proxy broken, NOT DataDome
            if "ns_error_abort" in msg or "err_" in msg or "connection" in msg or "refused" in msg:
                log(f"[!] proxy connection failed — cannot reach github.com ({exc})")
                log("[!] check proxy credentials, port, or try a different sticky port")
                raise SignupError(f"proxy connection failed: {exc}")
            if "timeout" in msg and load_attempt == 0:
                log(f"[!] homepage timeout — retrying ({exc})")
                _sleep_with_cancel(3, stop)
                continue
            log(f"[!] homepage goto failed: {exc}")
            return False
    if not page_loaded:
        return False
    if _is_hard_block(page):
        _log_block_ip(page, log)
        return False

    # simulate human browsing: mouse movement, scrolling, reading pauses
    scroll_done = False
    for i in range(dwell):
        _raise_if_cancelled(stop)
        # random mouse movement (40% chance each second)
        if random.random() < 0.40:
            _human_mouse_move(page)
        # scroll pattern: down at ~3s, more at ~7s, back up at ~10s
        if i == random.randint(2, 4) and not scroll_done:
            _human_scroll(page, "down", random.randint(200, 500))
            scroll_done = True
        elif i == random.randint(6, 8):
            _human_scroll(page, "down", random.randint(100, 300))
        elif i == random.randint(9, 11):
            if random.random() < 0.5:
                _human_scroll(page, "up", random.randint(100, 300))
        # variable sleep (not fixed 1s)
        _human_delay(1.0, 0.3, stop)
        if _is_hard_block(page):
            _log_block_ip(page, log)
            return False
    log("[*] homepage warm-up complete")
    return True


def _open_signup(page, log, attempts: int = 3, stop=None) -> None:
    """Open github.com/signup with homepage warm-up first.

    Strategy:
      1. Homepage warm-up (12s) — earn DataDome trust cookie before /signup
      2. Navigate to /signup via 'Sign up' link (human path) or direct goto
      3. On DataDome challenge: longer warm-up (20s) + retry
      4. Final: 120s manual solve window
    """
    sel = ", ".join(_EMAIL_INPUTS)
    last_hint = ""

    # --- Phase 1: homepage warm-up before first /signup attempt ---
    if not _homepage_warmup(page, log, stop=stop, dwell=12):
        _log_block_ip(page, log)
        raise SignupBlocked(
            "DataDome HARD BLOCK on homepage warm-up — this IP is blocked. "
            "Change IP, disable VPN/WARP, or configure a residential proxy."
        )

    for attempt in range(1, attempts + 1):
        _raise_if_cancelled(stop)
        # navigate to /signup: prefer clicking the link (human path), fallback to goto
        try:
            link = page.get_by_role("link", name="Sign up").first
            if link.count():
                link.click(timeout=10_000)
                log("[*] navigated to /signup via 'Sign up' link")
            else:
                page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=60_000)
                log("[*] navigated to /signup via direct goto")
        except Exception as exc:
            log(f"[!] navigation to /signup failed ({exc}); trying direct goto")
            try:
                page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc2:
                log(f"[!] goto /signup also failed: {exc2}")

        # wait for the email form to appear
        deadline = time.time() + 30
        while time.time() < deadline:
            _raise_if_cancelled(stop)
            _raise_if_rate_limited(page)
            if _is_hard_block(page):
                _log_block_ip(page, log)
                # Phase 2: longer warm-up retry on hard block
                if attempt < attempts:
                    log("[!] hard block on /signup — trying longer homepage warm-up (20s)")
                    if _homepage_warmup(page, log, stop=stop, dwell=20):
                        break  # warm-up OK, retry /signup in next attempt
                    else:
                        _log_block_ip(page, log)
                        raise SignupBlocked(
                            "DataDome HARD BLOCK persists after warm-up — IP is flagged. "
                            "Change IP or proxy provider."
                        )
                else:
                    raise SignupBlocked(
                        "DataDome HARD BLOCK: 'Access is temporarily restricted' — this IP is "
                        "temporarily blocked by GitHub. Change IP, disable VPN/WARP, change network, "
                        "or configure a residential proxy and retry."
                    )
            if _form_ready(page):
                log("[*] github.com/signup email form is ready")
                return
            hint = _challenge_hint(page)
            if hint:
                last_hint = hint
                _try_click_datadome(page, log)
            _sleep_with_cancel(2, stop)

        if attempt < attempts:
            log(f"[!] {last_hint or 'form not ready'} — reload attempt {attempt + 1}/{attempts}")

    if last_hint:
        # final long wait: challenge may need a manual click in the visible window
        log(f"[!] {last_hint} — waiting up to 120s; solve the check in the browser window "
            f"if visible, or configure a residential proxy")
        _try_click_datadome(page, log)
        deadline = time.time() + 120
        while time.time() < deadline:
            _raise_if_cancelled(stop)
            _raise_if_rate_limited(page)
            if _form_ready(page):
                log("[*] challenge passed, email form is ready")
                return
            _sleep_with_cancel(2, stop)
    raise SignupError(f"email form did not appear ({last_hint or 'no challenge marker'}); "
                      f"IP is blocked by DataDome — use a residential proxy in config")


def _username_error(page) -> str:
    """Return the username validation error shown under the field, or ''.

    IMPORTANT: 'Username may only contain alphanumeric...' is a PERMANENT helper
    (id=username-helper), not an error. Real errors render inside the auto-check
    element above it (role=alert / .is-error text), e.g. 'Username is not
    available' or 'Username xyz is not available'.
    """
    try:
        # error text lives in <auto-check> successors with role=alert
        alerts = page.locator("auto-check [role='alert'], .is-error, [role='alert']").all()
        for a in alerts:
            try:
                txt = (a.inner_text(timeout=1000) or "").strip().lower()
            except Exception:
                continue
            if "username" in txt and "may only contain" not in txt:
                if "not available" in txt or "already taken" in txt:
                    return "taken"
                if txt:
                    return "invalid"
        # fallback: visible error paragraphs mentioning the typed name
        text = _page_text(page)[:1200].lower()
        if "username is not available" in text or "username is already taken" in text:
            return "taken"
    except Exception:
        pass
    return ""


def _dom_click_create_account(page) -> bool:
    """JS .click() on the ENABLED submit button — bypasses pointer hit-testing.

    When the button is enabled, JS click() runs the page's real handler (this
    is how a keyboard Enter on a focused form submits). Unlike force=True it
    does NOT fire a pointer event into whatever overlay covers the button, so
    it cannot trigger the 'Sorry, something went wrong' flash error.
    Returns True when the click landed on an enabled button.
    """
    return bool(
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='signup']");
                const b = form && form.querySelector("button[type='submit']");
                if (!b || b.disabled) return false;
                b.click();
                return true;
            }"""
        )
    )


def _click_create_account(page, log, wait_enabled: int = 30, stop=None) -> None:
    """Click 'Create account' once it is ENABLED.

    GitHub gates the button on the octocaptcha token, so first wait for
    `disabled` to clear. Then submit in the safest order:
      1. native pointer click (most human-like)
      2. JS DOM click on the enabled button — pointer events can be eaten by
         an invisible Octocaptcha/DataDome overlay; DOM click cannot
    A force=True pointer click is deliberately NOT used: it fires a real
    pointer event at the overlay's coordinates and has produced GitHub's
    'Sorry, something went wrong' flash error.
    """
    btn = page.locator("form[action*='signup'] button[type='submit']").first
    deadline = time.time() + wait_enabled
    enabled = False
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                enabled = True
                break
        except Exception:
            pass
        _sleep_with_cancel(0.8, stop)
    if enabled:
        # an invisible/visible Octocaptcha overlay is often what eats the
        # pointer click — poke the captcha frame first so it can finish
        _try_click_datadome(page, log)
        # human-like: move mouse to button, hover briefly, then click
        _human_mouse_to_element(page, btn)
        _human_delay(0.3, 0.15, stop)  # brief hover before click
        try:
            btn.click(timeout=10_000)
            log("[*] 'Create account' clicked (button enabled)")
            return
        except Exception as exc:
            log(f"[i] native click intercepted ({exc}); trying DOM click on enabled button")
            if _dom_click_create_account(page):
                log("[*] 'Create account' clicked via DOM (overlay bypassed)")
                return
            log("[!] DOM click found the button disabled again — validation regressed")
    # Do not force-submit a disabled form. Its disabled state means GitHub has
    # not completed its email/password/username/Octocaptcha checks yet; forcing
    # it creates false submits, secondary rate-limit pressure, and stuck flows.
    hint = _form_validation_hint(page)
    raise SignupError(
        "Create account stayed disabled after validation wait"
        + (f": {hint}" if hint else " (Octocaptcha or async validation still pending)")
    )


def _fill_and_create_account(page, base_username: str, tries: int, log, stop=None) -> str:
    """Fill username, wait 3s, CLICK 'Create account', verify the page reacts.

    If GitHub answers with a username error, append one digit and retry
    (name -> name2 -> name3 ...). Returns the accepted username once the
    page actually moves past the signup form.
    """
    name = base_username
    for attempt in range(1, tries + 1):
        _raise_if_cancelled(stop)
        _human_fill(page, _USERNAME_INPUTS, name, stop=stop)
        # GitHub debounces username availability; wait for the server result.
        # Variable delay: 3-5s (not fixed 3.5s)
        _human_delay(3.5, 0.8, stop)
        _human_random_pause(stop)
        # scroll down to see the submit button (human behavior)
        _human_scroll(page, "down", random.randint(50, 150))
        _click_create_account(page, log, stop=stop)

        # wait for reaction: error under username field OR page moving forward
        deadline = time.time() + 15
        reacted = False
        while time.time() < deadline:
            _raise_if_cancelled(stop)
            _raise_if_rate_limited(page)
            _sleep_with_cancel(1, stop)
            err = _username_error(page)
            if err == "taken":
                log(f"[*] username {name} taken, retry with +1 digit ({attempt}/{tries})")
                name = f"{base_username}{attempt + 1}"  # name2, name3, ...
                reacted = True
                break
            if err == "invalid":
                raise SignupError(f"username {name} rejected as invalid")
            # page moved on from the signup form -> submit accepted
            if not _form_ready(page):
                return name
            if "signup" not in page.url:
                return name
        if reacted:
            continue  # username was taken — loop with the next suffix
        # no error and no movement: the submit never registered (button still
        # disabled by octocaptcha?) — one JS-click retry, then fail loudly
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='signup']");
                const b = form && form.querySelector("button[type='submit']");
                if (b) b.click();
            }"""
        )
        _sleep_with_cancel(5, stop)
        if not _form_ready(page) or "signup" not in page.url:
            return name
        raise SignupError(
            f"'Create account' did nothing after two clicks (username={name}); "
            f"octocaptcha/DataDome gate never lifted — retry the run or change IP"
        )
    raise SignupError(f"username still taken after {tries} tries (base={base_username})")


def _verify_input_visible(page) -> bool:
    """Is any e-mail verification code input visible? (launch-code page)"""
    for sel in _OTP_INPUTS:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


def _verify_page_markers(page) -> bool:
    """Text markers of the email-verification ('launch code') page."""
    try:
        text = _page_text(page)[:2000].lower()
    except Exception:
        return False
    return any(
        m in text
        for m in ("launch code", "verify your email", "check your email",
                  "enter the code", "we sent a code", "verification code")
    )


def _logged_in(context) -> bool:
    """Reliable success signal: GitHub sets cookie logged_in=yes on a real session."""
    try:
        for c in context.cookies():
            if c.get("name") == "logged_in" and str(c.get("value", "")).lower() == "yes":
                return True
    except Exception:
        pass
    return False


def _post_submit_state(page, context) -> str:
    """Classify what GitHub shows after 'Create account'.

    Returns one of:
      'verify' — email verification (launch code) page: code input visible or markers
      'done'   — logged in (cookie logged_in=yes) or a welcome/onboarding page
      'pending'— still transitioning
    """
    if _verify_input_visible(page):
        return "verify"
    if _logged_in(context):
        return "done"
    url = page.url or ""
    text = ""
    try:
        text = _page_text(page)[:2000].lower()
    except Exception:
        pass
    if _verify_page_markers(page):
        return "verify"
    if "signup" in url:
        return "pending"
    # off /signup without verify markers and without login cookie — ambiguous,
    # treat onboarding/welcome/created-successfully text as done, else pending
    if any(m in text for m in ("welcome to github", "let's get started", "get started",
                               "what do you want to do", "your github journey",
                               "your account was created successfully")):
        return "done"
    return "pending"


def _wait_post_submit(page, context, timeout: int = 120, log=None, stop=None) -> str:
    """Wait after submit until the state is stable (not 'pending').

    Anti-race: require the state to hold for 2 consecutive checks (≥4s) before
    deciding, so a mid-transition page can't be misread as 'done'.
    """
    stable_state = ""
    stable_hits = 0
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        state = _post_submit_state(page, context)
        if state != "pending":
            if state == stable_state:
                stable_hits += 1
            else:
                stable_state = state
                stable_hits = 1
            if stable_hits >= 2:
                return state
        else:
            stable_state = ""
            stable_hits = 0
        if log and time.time() - last_log >= 3:
            log(f"[*] post-submit state={state or 'pending'} url={page.url}")
            last_log = time.time()
        _sleep_with_cancel(2, stop)
    raise SignupError(
        f"post-submit state never stabilized; url={page.url} "
        f"body={_page_text(page)[:200]!r}"
    )


def _browser_ctx_options(cfg: Config, log=None) -> dict:
    """Launch options tuned for DataDome (see 2026 field guides):

    - fresh_profile=True: a NEW browser per account (incognito-like, zero
      cached state — no stacked GitHub logins). The DataDome trust cookie is
      carried over separately via .datadome-trust.json (see _save_trust_cookie
      / _restore_trust_cookie) so the signup page keeps loading.
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
    import platform

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
    proxy_url = _ensure_sticky_proxy(cfg.proxy, log=log) if (cfg.proxy or "").strip() else ""
    proxy = _parse_proxy(proxy_url) if proxy_url else None
    if proxy:
        if _proxy_needs_bridge(proxy):
            # Firefox cannot authenticate to SOCKS5 — run a local no-auth HTTP
            # bridge that relays to the authed upstream with remote DNS.
            # The manager reuses an already-running bridge so a NEW bridge is
            # NOT started for every fresh-profile launch (bridge is
            # sticky-session-bound).
            opts["proxy"] = _get_bridge(proxy_url) or proxy
        else:
            opts["proxy"] = proxy
        if _proxy_is_socks(proxy):
            try:
                exit_ip = _socks_exit_ip(proxy_url)
                # reject IPv6 — DataDome is stricter with IPv6, and Camoufox
                # geoip works best with IPv4
                if ":" in exit_ip:
                    if log:
                        log(f"[!] IPv6 exit IP detected ({exit_ip}) — rotating to get IPv4")
                    _rotate_sticky_proxy()
                    raise SignupError("IPv6 exit IP, need IPv4")
                # validate IP is in a known geoip database (Camoufox will fail
                # with "IP not found in database" for obscure ranges)
                if not _validate_geoip(exit_ip):
                    if log:
                        log(f"[!] IP {exit_ip} not in geoip database — rotating")
                    _rotate_sticky_proxy()
                    raise SignupError(f"IP {exit_ip} not in geoip database")
                opts["geoip"] = exit_ip
                _proxy_manager.exit_ip = exit_ip  # consumed by trust-cookie IP binding
                if log:
                    log(f"[*] socks proxy exit IP: {exit_ip} (geoip pinned, sticky)")
            except Exception as exc:
                # re-raise so the caller (register_one) can retry with a new IP
                if "IPv6 exit IP" in str(exc) or "not in geoip database" in str(exc):
                    raise
                # SSL/connection errors mean the proxy itself is broken — rotate
                msg = str(exc).lower()
                if "ssl" in msg or "wrong_version" in msg or "connection" in msg or "refused" in msg:
                    if log:
                        log(f"[!] proxy connection broken ({exc}) — rotating sticky port")
                    _rotate_sticky_proxy()
                    raise SignupError(f"proxy connection failed: {exc}")
                opts["geoip"] = False
                _proxy_manager.exit_ip = None  # no IP to bind — do NOT restore stale cookies
                if log:
                    log(f"[!] socks exit-IP lookup failed ({exc}); geoip disabled — "
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


# ---------------------------------------------------------------------------
# DataDome trust-cookie carry-over for fresh-profile mode
#
# A brand-new browser has zero cookies — DataDome will challenge it. We persist
# ONLY the `datadome` cookie (+device id) to .datadome-trust.json after each
# successful run and inject it into every fresh context. No GitHub session
# state is ever carried over, so accounts never stack.
# ---------------------------------------------------------------------------

_TRUST_FILE = ROOT / ".datadome-trust.json"
_TRUST_COOKIE_NAMES = {"datadome", "datadome_proxied", "device_id", "_device_id"}


def _save_trust_cookie(context, log=None) -> None:
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
                "exit_ip": _proxy_manager.exit_ip or "",
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }),
            encoding="utf-8",
        )
        if log:
            log(f"[*] datadome trust cookie saved ({len(keep)} cookies, ip={_proxy_manager.exit_ip or 'n/a'})")
    except Exception as exc:
        if log:
            log(f"[i] trust cookie save failed: {exc}")


def _restore_trust_cookie(context, log=None) -> None:
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
        if not _proxy_manager.exit_ip:
            if log:
                log("[i] trust cookie skipped (current exit IP is unknown; lookup failed)")
            return
        if bound_ip and _proxy_manager.exit_ip and bound_ip != _proxy_manager.exit_ip:
            if log:
                log(f"[i] trust cookie skipped (bound to IP {bound_ip}, current IP {_proxy_manager.exit_ip})")
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


def _context_and_page(browser):
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


def _clean_github_session_cookies(context, log) -> None:
    """Between accounts: drop GitHub login cookies, keep DataDome/trust cookies.

    A persistent profile survives across accounts, so 'logged_in'/'user_session'
    cookies must be cleared to avoid signing INTO the previous account instead
    of signing UP a new one. DataDome (datadome) cookies are kept — they carry
    the anti-bot trust that lets /signup load at all.
    """
    drop = {"logged_in", "user_session", "__Host-user_session_same_site", "_gh_sess", "dotcom_user"}
    try:
        cookies = context.cookies()
        keep = [c for c in cookies if c.get("name") not in drop]
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


def _fill_launch_code(page, code: str, log) -> None:
    """Fill the 8-box launch-code page (one digit per #launch-code-N input).

    Falls back to a single OTP input when the boxes are not present.
    """
    boxes = page.locator("input[id^='launch-code-']")
    count = boxes.count()
    if count >= len(code):  # 8 boxes for an 8-digit code
        for i, digit in enumerate(code):
            boxes.nth(i).fill(digit)
            time.sleep(0.15)  # small human cadence between boxes
        log(f"[*] launch code typed into {len(code)} boxes")
        try:
            page.locator(
                "button[class*='Button--primary'], button[class*='Button-module__Button--primary']"
            ).first.click(timeout=5000)
            log("[*] launch code submitted")
        except Exception:
            log("[*] no submit button found — launch code may auto-submit")
        return
    # single input fallback
    otp = _first(page, _OTP_INPUTS, visible=True)
    if not otp.input_value():
        otp.fill(code)
    _click_submit(page)
    log("[*] OTP submitted")


_LOGIN_INPUTS = ["#login_field", "input[name='login']", "input#login"]
_LOGIN_PASS_INPUTS = ["#password", "input[name='password']", "input[type='password']"]


def _try_login(page, username: str, password: str, context, log) -> bool:
    """GitHub sends fresh signups to /login: sign in to obtain logged_in=yes.

    Returns True when the login cookie is present afterwards.
    """
    try:
        user = page.locator(", ".join(_LOGIN_INPUTS)).first
        if not user.is_visible():
            return _logged_in(context)
        user.fill(username, timeout=5000)
        page.locator(", ".join(_LOGIN_PASS_INPUTS)).first.fill(password, timeout=5000)
        time.sleep(0.5)
        # the sign-in button lives in form[action='/session'] but is NOT
        # type=submit (only Google/Apple are). Click the form's own button.
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='session']");
                if (!form) return;
                // prefer a real submit element, else the last button in the form
                let btn = form.querySelector("input[type='submit'], button:not([type='button'])");
                if (!btn) {
                    const btns = form.querySelectorAll("button");
                    btn = btns[btns.length - 1];
                }
                if (btn) btn.click();
            }"""
        )
        log("[*] login form submitted after signup")
    except Exception as exc:
        log(f"[i] auto-login skipped: {exc}")
    deadline = time.time() + 30
    while time.time() < deadline:
        if _logged_in(context):
            return True
        time.sleep(1.5)
    return False


def _create_repository(page, username: str, base_name: str, log) -> str:
    """Stage 4 (user recording): create the first repository on /new.

    The name field auto-generates a suggestion; we type our own name and submit.
    Returns the repository name created.
    """
    def _submit() -> None:
        """Submit the visible enabled repo form without clicking an overlay."""
        btn = page.get_by_role("button", name="Create repository").first
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if btn.count() and btn.is_visible() and btn.is_enabled():
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise SignupError("Create repository stayed disabled after validation wait")

        try:
            btn.click(timeout=10_000)
            log("[*] 'Create repository' clicked")
            return
        except Exception as exc:
            log(f"[i] repository native click intercepted ({exc}); trying DOM click")

        clicked = bool(page.evaluate(
            """() => {
                const buttons = [...document.querySelectorAll('button')];
                const button = buttons.find((b) =>
                    b.offsetParent !== null && !b.disabled &&
                    (b.textContent || '').trim() === 'Create repository'
                );
                if (!button) return false;
                button.click();
                return true;
            }"""
        ))
        if not clicked:
            raise SignupError("Create repository button was not visible/enabled for DOM click")
        log("[*] 'Create repository' clicked via DOM (overlay bypassed)")

    name = base_name or "hello"
    page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
    # try multiple selectors — GitHub may have changed the repo name input
    repo_selectors = [
        "#repository-name-input",
        "input[name='repository[name]']",
        "input[aria-label='Repository name']",
        "input[placeholder*='repository' i]",
        "input[placeholder*='repo' i]",
        "input[data-testid='repository-name-input']",
    ]
    inp = None
    for sel in repo_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                inp = loc
                log(f"[*] repo name input found: {sel}")
                break
        except Exception:
            continue
    if inp is None:
        # last resort: wait for any text input on the page
        try:
            page.wait_for_selector("input[type='text']", state="visible", timeout=15_000)
            inp = page.locator("input[type='text']").first
            log("[*] repo name input found via fallback: input[type='text']")
        except Exception:
            raise SignupError(f"repo form not found; url={page.url} body={_page_text(page)[:300]!r}")
    inp.fill(name)
    time.sleep(1.5)  # let GitHub validate + enable the submit button
    try:
        _submit()
    except Exception as exc:
        raise SignupError(f"cannot click 'Create repository': {exc}")
    # success = redirected to /<username>/<repo>
    deadline = time.time() + 30
    while time.time() < deadline:
        url = page.url or ""
        if "/new" not in url and f"/{username}/" in url:
            log(f"[*] repository created: {url}")
            return name
        # name conflict? GitHub shows an error — retry with a numeric suffix
        err = ""
        try:
            err = _page_text(page)[:600].lower()
        except Exception:
            pass
        if "already exists" in err and "/new" in url:
            log(f"[*] repo {name} exists, retry with suffix")
            name = f"{base_name}{int(time.time()) % 10000}"
            page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("#repository-name-input", state="visible", timeout=20_000)
            page.locator("#repository-name-input").first.fill(name)
            time.sleep(1.5)
            _submit()
        time.sleep(1)
    raise SignupError(f"repository creation not confirmed; url={page.url}")


def _fetch_public_profile() -> dict[str, str]:
    """Fetch one display identity and one quote without using their credentials."""
    random_user = requests.get("https://randomuser.me/api/", timeout=15).json()
    quote = requests.get("https://zenquotes.io/api/random", timeout=15).json()
    return parse_public_profile(random_user, quote)


def _visible_dom_click(page, matcher_js: str) -> bool:
    """Click a visible enabled button through DOM when overlays eat pointer input."""
    return bool(page.evaluate(
        f"""() => {{
            const button = [...document.querySelectorAll('button')].find({matcher_js});
            if (!button || button.disabled || button.offsetParent === null) return false;
            button.click();
            return true;
        }}"""
    ))


def _complete_profile(page, username: str, cfg: Config, log) -> None:
    """Set recorded status and public profile fields after 2FA is secured."""
    if not (cfg.set_profile_status or cfg.complete_profile):
        return
    profile = None
    if cfg.complete_profile:
        custom = {
            "name": cfg.profile_name.strip(),
            "bio": cfg.profile_bio.strip(),
            "location": cfg.profile_location.strip(),
        }
        # Avoid external APIs entirely when every profile field is configured.
        profile = _fetch_public_profile() if not all(custom.values()) else {}
        profile = {key: custom[key] or profile[key] for key in custom}
    page.goto(f"https://github.com/{username}", wait_until="domcontentloaded", timeout=60_000)

    if cfg.set_profile_status:
        status = cfg.profile_status.strip() or "On vacation"
        # Try multiple selectors for the status launcher button
        launcher_selectors = [
            "button:has-text('Set status')",
            "button[aria-label*='status' i]",
            "react-partial-anchor button",
            "button:has-text('Edit status')",
            "summary:has-text('status')",
        ]
        launcher_opened = False
        for sel in launcher_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=5000)
                    launcher_opened = True
                    log(f"[*] profile status launcher clicked: {sel}")
                    break
            except Exception:
                continue
        if not launcher_opened:
            launcher_opened = _visible_dom_click(
                page,
                "b => /status/i.test(b.getAttribute('aria-label') || '') || "
                "(b.textContent || '').trim() === 'Set status' || "
                "(b.textContent || '').trim() === 'Edit status'",
            )
            if not launcher_opened:
                log("[i] profile status launcher not found; status skipped")
            else:
                log("[*] profile status launcher clicked via DOM")
        if launcher_opened:
            # try multiple selectors for the status input
            status_input = None
            input_selectors = [
                "#user-status-status-input",
                "input[aria-label*='status' i]",
                "input[placeholder*='status' i]",
                "textarea[aria-label*='status' i]",
            ]
            for sel in input_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count():
                        loc.wait_for(state="visible", timeout=5000)
                        status_input = loc
                        log(f"[*] status input found: {sel}")
                        break
                except Exception:
                    continue
            if status_input is None:
                raise SignupError("profile status popup did not open")
            status_input.fill(status, timeout=8_000)
            if status_input.input_value(timeout=3_000) != status:
                raise SignupError("profile status input did not retain the configured value")

            submit = page.locator("#__primerPortalRoot__ button").filter(
                has_text="Set status"
            ).last
            try:
                submit.click(timeout=8_000)
            except Exception:
                if not _visible_dom_click(
                    page,
                    "b => b.closest('#__primerPortalRoot') && "
                    "(b.textContent || '').trim() === 'Set status'",
                ):
                    raise SignupError("cannot submit profile status")
                log(f"[*] profile status submitted via DOM: {status}")

            # A successful submit closes the status popup. It is the reliable
            # confirmation independent of profile-page text rendering timing.
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    if not status_input.is_visible():
                        log(f"[*] profile status saved: {status}")
                        break
                except Exception:
                    log(f"[*] profile status saved: {status}")
                    break
                time.sleep(0.4)
            else:
                raise SignupError(f"profile status did not save: {status}")

    if not profile:
        return
    edit_button = page.locator("button[name='button']").filter(has_text="Edit profile").first
    try:
        edit_button.click(timeout=10_000)
    except Exception as exc:
        log(f"[i] Edit profile native click intercepted ({exc}); trying DOM click")
        if not _visible_dom_click(
            page,
            "b => (b.textContent || '').trim() === 'Edit profile' || "
            "b.classList.contains('js-profile-editable-edit-button')",
        ):
            raise SignupError("cannot open Edit profile (button not found for DOM click)")
        log("[*] Edit profile clicked via DOM (overlay bypassed)")

    name_input = page.locator("#user_profile_name").first
    bio_input = page.locator("#user_profile_bio").first
    location_input = page.locator("input[name='user[profile_location]']").first
    for field in (name_input, bio_input, location_input):
        field.wait_for(state="visible", timeout=15_000)
    name_input.fill(profile["name"])
    bio_input.fill(profile["bio"])
    location_input.fill(profile["location"])

    try:
        page.locator(f"form[action='/users/{username}'] button").filter(
            has_text="Save"
        ).first.click(timeout=10_000)
    except Exception:
        if not _visible_dom_click(page, "b => (b.textContent || '').trim() === 'Save'"):
            raise SignupError("cannot submit Edit profile")
    try:
        page.wait_for_timeout(1_500)
        # After a successful save, either profile text is rendered or the form
        # retains the saved input value during its partial refresh.
        if profile["name"] not in _page_text(page) and name_input.input_value() != profile["name"]:
            raise SignupError("profile save was not confirmed")
    except SignupError:
        raise
    except Exception:
        pass
    log(f"[*] profile completed: {profile['name']} | {profile['location']}")


def _enable_2fa(page, log) -> tuple[str, str]:
    """Stage 5 (user recording): enable TOTP 2FA and return the secret.

    Flow (from the recording):
      Settings → Password and authentication → 'Enable two-factor authentication'
      → 'Authenticator apps and browser extension' → click 'setup key' to reveal
      the secret in a textfield → READ the secret → compute TOTP via pyotp →
      fill input[name='otp'] → Continue → save recovery codes → 'I have saved my
      recovery codes' → Done.
    """
    import pyotp

    page.goto("https://github.com/settings/security", wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("#settings-frame", state="visible", timeout=30_000)
    except Exception:
        raise SignupError(f"security settings page failed; url={page.url}")

    # 'Enable two-factor authentication' is an <a href> link (NOT a button):
    # /settings/two_factor_authentication/setup/intro — navigate straight to it.
    # NOTE: GitHub REGENERATES the TOTP secret on every load of this page, so
    # read the secret only from the page we actually fill the code into.
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
            page.goto(
                "https://github.com/settings/two_factor_authentication/setup/intro",
                wait_until="domcontentloaded", timeout=60_000,
            )
    except Exception:
        pass  # already on the page; proceed

    # wait for the setup wizard — try multiple selectors (GitHub may have changed DOM)
    wizard_selectors = [
        "div[data-target='two-factor-setup-verification.mashedSecret']",
        "[data-target*='mashedSecret']",
        "[data-target*='two-factor']",
        "div[role='dialog']",
        "#two-factor-setup",
    ]
    wizard_loaded = False
    for sel in wizard_selectors:
        try:
            page.wait_for_selector(sel, state="attached", timeout=15_000)
            wizard_loaded = True
            log(f"[*] 2FA wizard found: {sel}")
            break
        except Exception:
            continue
    if not wizard_loaded:
        # check if we're already on a 2FA page (maybe different URL structure)
        if "two_factor" not in (page.url or ""):
            raise SignupError(f"2FA setup wizard did not load; url={page.url}")

    # reveal the setup key via the 'setup key' button
    reveal_selectors = [
        "#dialog-show-two-factor-setup-verification-mashed-secret",
        "button:has-text('setup key')",
        "button:has-text('Setup key')",
        "button:has-text('text code')",
        "details summary:has-text('setup key')",
    ]
    for sel in reveal_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=5000)
                log(f"[*] 2FA setup key revealed via: {sel}")
                break
        except Exception:
            continue
    time.sleep(1)

    # read the TOTP secret — try multiple selectors and patterns
    secret = ""
    secret_selectors = [
        "div[data-target='two-factor-setup-verification.mashedSecret']",
        "[data-target*='mashedSecret']",
        "code[data-target*='secret']",
        "samp",
        "code",
    ]
    for sel in secret_selectors:
        try:
            txt = page.locator(sel).first.inner_text(timeout=3000) or ""
            txt = txt.strip().replace(" ", "")
            if txt and len(txt) >= 16 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in txt.upper()):
                secret = txt
                log(f"[*] TOTP secret found via: {sel}")
                break
        except Exception:
            continue
    if not secret:
        # fallback: scan page HTML for a base32-looking secret (16-32 chars)
        import re

        body = ""
        try:
            body = page.content()
        except Exception:
            body = ""
        m = re.search(r"\b([A-Z2-7]{16,32})\b", body or "")
        if m:
            secret = m.group(1)
    if not secret or len(secret) < 16:
        raise SignupError(f"TOTP secret not found (got {secret!r})")
    log(f"[*] TOTP secret captured: {secret}")

    # close the setup-key dialog if it opened
    try:
        page.locator("[aria-label='Close']").first.click(timeout=3000)
    except Exception:
        pass

    # compute the current TOTP code and submit it
    totp = pyotp.TOTP(secret)
    code = totp.now()
    log(f"[*] TOTP code generated: {code}")
    # the ENABLED otp input is the one with aria-label; input[name='otp'] is a
    # hidden/disabled twin (from the recording) — fill the enabled one.
    otp_input = page.locator(
        "input[aria-label='Verify the code from the app']:not([disabled])"
    ).first
    try:
        otp_input.fill(code, timeout=10_000)
    except Exception:
        otp_input = page.locator("input[name='otp']:not([disabled])").first
        otp_input.fill(code, timeout=10_000)

    # --- helper: click the VISIBLE enabled wizard button by its label ---
    # The wizard keeps all steps' buttons in the DOM; Playwright's is_visible()
    # is unreliable there, so use the browser's own visibility semantics
    # (offsetParent !== null) to find the ACTIVE step's button.
    def _click_active_wizard_button(page, label: str) -> bool:
        try:
            clicked = page.evaluate(
                """(label) => {
                    const btns = [...document.querySelectorAll(
                        "button[data-target='single-page-wizard-step.nextButton'], " +
                        "button[data-action='click:two-factor-setup-recovery-codes#onDownloadClick'], " +
                        "button[data-action='click:single-page-wizard-step#onNext']"
                    )];
                    for (const b of btns) {
                        if (b.offsetParent !== null && !b.disabled &&
                            (b.textContent || '').trim().toLowerCase() === label.toLowerCase()) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                label,
            )
            return bool(clicked)
        except Exception:
            return False

    if not _click_active_wizard_button(page, "Continue"):
        # fallback: any visible enabled next button (its label may be icon-only)
        try:
            page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll(
                        "button[data-target='single-page-wizard-step.nextButton']"
                    )];
                    for (const b of btns) {
                        if (b.offsetParent !== null && !b.disabled) { b.click(); return true; }
                    }
                    return false;
                }"""
            )
        except Exception:
            pass
    log("[*] TOTP code submitted → Continue")
    time.sleep(3)

    # ---- recovery codes step ----
    recovery = ""
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            # prefer the dedicated element, else scan the page text
            codes: list[str] = []
            try:
                rc_el = page.locator("two-factor-setup-recovery-codes, [data-target='two-factor-setup-recovery-codes']")
                if rc_el.count():
                    txt = rc_el.first.inner_text(timeout=3000) or ""
                else:
                    txt = _page_text(page)
            except Exception:
                txt = _page_text(page)
            import re as _re

            codes = list(dict.fromkeys(_re.findall(r"\b[a-z0-9]{5,6}-[a-z0-9]{5,6}\b", txt, _re.I)))
            if codes:
                recovery = "\n".join(codes[:16])
                break
            time.sleep(1)
        if recovery:
            log(f"[*] recovery codes captured ({len(recovery.splitlines())} codes)")
    except Exception:
        pass

    # download recovery codes (as recorded), then confirm & finish
    try:
        with page.expect_download(timeout=10_000) as dl_info:
            page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll('button')].find(
                        b => b.offsetParent !== null && !b.disabled &&
                             /download/i.test((b.textContent || '').trim())
                    );
                    if (b) b.click();
                }"""
            )
        download = dl_info.value
        log(f"[*] recovery codes downloaded: {download.suggested_filename}")
        try:
            path = str(download.path())
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    dl_text = f.read()
                if dl_text and not recovery:
                    import re as _re

                    codes = list(dict.fromkeys(_re.findall(r"\b[a-z0-9]{5,6}-[a-z0-9]{5,6}\b", dl_text, _re.I)))
                    if codes:
                        recovery = "\n".join(codes[:16])
                        log(f"[*] recovery codes from download ({len(codes)} codes)")
        except Exception:
            pass
    except Exception as exc:
        log(f"[i] recovery codes download skipped: {exc}")

    if _click_active_wizard_button(page, "I have saved my recovery codes"):
        log("[*] recovery codes confirmed")
    else:
        # fallback: click by data-action nextButton (visible one)
        page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll(
                    "button[data-target='single-page-wizard-step.nextButton']"
                )];
                for (const b of btns) {
                    if (b.offsetParent !== null && !b.disabled) { b.click(); return true; }
                }
                return false;
            }"""
        )
        log("[*] recovery codes confirmed (fallback)")
    time.sleep(2)
    if _click_active_wizard_button(page, "Done"):
        log("[*] 2FA wizard finished")
    else:
        page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll(
                    "button[data-target='single-page-wizard-step.nextButton']"
                )];
                for (const b of btns) {
                    if (b.offsetParent !== null && !b.disabled) { b.click(); return true; }
                }
                return false;
            }"""
        )
    time.sleep(2)

    # persist recovery codes next to the accounts file for account recovery
    if recovery:
        try:
            rc_path = ROOT / "github_recovery_codes.txt"
            with rc_path.open("a", encoding="utf-8") as f:
                f.write(f"=== {page.url} @ {datetime.now().isoformat(timespec='seconds')} ===\n")
                f.write(recovery + "\n\n")
            log(f"[*] recovery codes saved to {rc_path.name}")
        except Exception as exc:
            log(f"[i] recovery codes write failed: {exc}")
    return secret, recovery


def _fill_signup_form(page, cfg, email, password, log, stop) -> str:
    """Fill the single-page signup form (email -> password -> username).

    Returns the accepted username. Raises SignupError with a clear reason when
    the form cannot be completed (validation error, overlay, rate limit).
    """
    # human-like: scroll down to see the form, random pause to "read" it
    _human_scroll(page, "down", random.randint(100, 250))
    _human_delay(1.0, 0.4, stop)  # "look at the form"
    _human_random_pause(stop)

    # Fill in the same order as a person: email -> wait -> password ->
    # wait -> username. Each blur gives GitHub's async form validators and
    # Octocaptcha time to settle before Create account is considered.
    _human_fill(page, _EMAIL_INPUTS, email, stop=stop)
    _human_delay(1.2, 0.5, stop)  # variable pause after email
    _raise_if_rate_limited(page)
    _human_random_pause(stop)

    _human_fill(page, _PASSWORD_INPUTS, password, stop=stop)
    _human_delay(1.0, 0.4, stop)  # variable pause after password
    _raise_if_rate_limited(page)
    _human_random_pause(stop)

    # 3s pause after username -> CLICK Create account -> on username error
    # append one digit and retry (name -> name2 -> name3 ...)
    return _fill_and_create_account(
        page, username_from_email(email), cfg.max_username_tries, log, stop=stop
    )


def _post_form_flow(
    page, context, cfg: Config, email: str, password: str, username: str,
    mail, order_id: str, log, stop,
) -> tuple[str, str, str]:
    """Everything AFTER the signup form was accepted: email verification
    (launch code), auto-login, first repository (stage 4), TOTP 2FA (stage 5).
    Returns (username, totp_secret, recovery_codes)."""
    # after submit GitHub either shows the email verification (launch code)
    # page, or (high-trust sessions) logs straight in.
    state = _wait_post_submit(page, context, timeout=120, log=log, stop=stop)
    if state == "verify":
        log(f"[*] verification page: {page.url}")
        # Litensi needs order_id as first arg; Tempik needs email as first arg.
        # Pass both as keyword args — each client picks what it needs.
        code = mail.wait_for_code(
            order_id,
            email=email,
            timeout=cfg.otp_timeout_sec,
            log=log,
            cancel_cb=stop,
        )
        log(f"[*] verification code: {code}")
        _fill_launch_code(page, code, log)
        # confirm the activation with Litensi: code was used (setstatus SUCCESS)
        try:
            delivered = mail.last_order_id or order_id
            mail.mark_success(delivered)
            log(f"[*] litensi order {delivered} confirmed SUCCESS")
        except Exception as exc:
            log(f"[i] litensi confirm SUCCESS failed: {exc}")
        # after OTP: must reach a logged-in state
        state2 = _wait_post_submit(page, context, timeout=90, log=log, stop=stop)
        if state2 == "verify":
            raise SignupError("verification code rejected (still on verify page)")
    # state 'done' required — no more accepting bare redirects
    totp_secret = ""
    recovery = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        _raise_if_cancelled(stop)
        _raise_if_rate_limited(page)
        if _logged_in(context):
            log("[*] logged_in cookie confirmed — account is active")
            return _finalize_account(page, context, cfg, email, username, log, stop)
        # GitHub sends fresh signups to /login: sign in with the new creds
        if "/login" in (page.url or ""):
            if _try_login(page, email, password, context, log):
                log("[*] logged_in cookie confirmed after auto-login")
                return _finalize_account(page, context, cfg, email, username, log, stop)
            raise SignupError("auto-login after signup failed")
        if _post_submit_state(page, context) == "pending":
            _sleep_with_cancel(2, stop)
            continue
        if _wait_post_submit(page, context, timeout=20, log=log, stop=stop) == "done":
            continue  # loop will hit the _logged_in check above
        _sleep_with_cancel(2, stop)
    raise SignupError(
        f"account not confirmed logged-in after flow; url={page.url} "
        f"body={_page_text(page)[:200]!r}"
    )


def _finalize_account(
    page, context, cfg: Config, email: str, username: str, log, stop
) -> tuple[str, str, str]:
    """Stages after the account is logged in: repo, 2FA, recovery, profile.

    Returns (username, totp_secret, recovery_codes). Post-signup stage
    failures never discard an already-verified account — the reason is
    logged and the flow continues.
    """
    totp_secret = ""
    recovery = ""
    if cfg.create_repo:
        try:
            _create_repository(page, username, cfg.repo_name, log)
        except Exception as exc:
            log(f"[i] create repo stage skipped: {exc}")
    if cfg.enable_2fa:
        try:
            totp_secret, recovery = _enable_2fa(page, log)
        except Exception as exc:
            log(f"[i] 2FA stage failed (account still saved): {exc}")
    _save_recovery_per_account(email, recovery, log)
    try:
        _complete_profile(page, username, cfg, log)
    except Exception as exc:
        log(f"[i] profile stage skipped (account still saved): {exc}")
    _save_trust_cookie(context, log)  # persist DataDome trust for the next fresh run
    return username, totp_secret, recovery


def _run_signup(
    cfg: Config,
    email: str,
    password: str,
    mail,  # LitensiClient or TempikClient
    order_id: str,
    log,
    stop,
) -> tuple[str, str]:
    """Run the whole sign-up; returns (username, totp).

    GitHub's signup is now a SINGLE page: Email* / Password* / Username* in one
    form (action=/signup?social=false), submit = "Create account" button.
    OAuth (Google/Apple) buttons live in separate <form> tags — never click them.

    The Octocaptcha token sometimes never settles on a given page load — the
    Create account button stays disabled forever. Two-tier retry strategy:

    Tier 1 (fast, cheap): within the SAME browser session, do `page.reload()`
    (Cmd+R equivalent) and re-fill the form with the SAME data (email +
    password + username). Up to `page_reloads` in-session retries.

    Tier 2 (slow, expensive): if Tier 1 exhausts, close the browser and open
    a completely fresh session (new fingerprint / cookies) and try again. Up
    to `session_reloads` full-session restarts.
    """
    page_reloads = 3      # in-session refresh (Cmd+R) attempts before switching session
    session_reloads = 2   # full browser restarts (new fingerprint) after page reloads fail
    last_exc: Exception | None = None
    for session_attempt in range(1, session_reloads + 2):
        _raise_if_cancelled(stop)
        if session_attempt > 1:
            log(f"[*] SESSION switch {session_attempt - 1}/{session_reloads} "
                f"(fresh browser + new fingerprint)")
        with Camoufox(**_browser_ctx_options(cfg, log=log if session_attempt == 1 else None)) as browser:
            # works for BOTH modes: persistent context (BrowserContext) and fresh
            # launch (Browser -> new context/page per account)
            context, page = _context_and_page(browser)
            if getattr(cfg, "fresh_profile", False):
                # fresh mode: inject ONLY the DataDome trust cookie (no GitHub state)
                _restore_trust_cookie(context, log)
            else:
                # persistent mode: wipe login state, keep DataDome trust cookies
                _clean_github_session_cookies(context, log)
            page.set_default_timeout(20_000)
            _open_signup(page, log, stop=stop, attempts=2 if session_attempt > 1 else 3)
            _reject_blocked(page)

            # --- Tier 1: in-session page reloads with same data ---
            page_last_exc: Exception | None = None
            username: str | None = None
            for page_attempt in range(1, page_reloads + 1):
                _raise_if_cancelled(stop)
                if page_attempt > 1:
                    log(f"[*] PAGE reload {page_attempt - 1}/{page_reloads - 1} "
                        f"(Reload with same data)")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60_000)
                    except Exception as exc:
                        log(f"[!] page.reload() failed ({exc}); falling back to goto()")
                        try:
                            page.goto(
                                "https://github.com/signup",
                                wait_until="domcontentloaded",
                                timeout=60_000,
                            )
                        except Exception as exc2:
                            page_last_exc = SignupError(f"page reload/goto failed: {exc2}")
                            break
                    # wait for the form to be ready again on the reloaded page
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        _raise_if_cancelled(stop)
                        _raise_if_rate_limited(page)
                        if _form_ready(page):
                            break
                        _sleep_with_cancel(1, stop)
                    else:
                        page_last_exc = SignupError("form not ready after page reload")
                        continue
                    _reject_blocked(page)

                try:
                    username = _fill_signup_form(page, cfg, email, password, log, stop)
                    log(f"[*] form submitted: email + password + username={username}")
                    break  # success — leave Tier 1 loop
                except SignupError as exc:
                    msg = str(exc)
                    reloadable = (
                        "stayed disabled" in msg
                        or "click" in msg.lower()
                        or "overlay" in msg.lower()
                        or "form" in msg.lower()
                    )
                    if reloadable and page_attempt < page_reloads:
                        page_last_exc = exc
                        log(f"[!] page attempt {page_attempt}/{page_reloads} failed "
                            f"({msg[:120]}); will refresh page and retry with same data")
                        continue
                    # either not-reloadable, or Tier 1 exhausted -> propagate to Tier 2 handler
                    page_last_exc = exc
                    break

            if username is None:
                # Tier 1 failed — decide whether to switch session (Tier 2)
                exc = page_last_exc or SignupError("form submit failed with unknown reason")
                msg = str(exc)
                reloadable = (
                    "stayed disabled" in msg
                    or "click" in msg.lower()
                    or "overlay" in msg.lower()
                    or "form" in msg.lower()
                )
                if reloadable and session_attempt <= session_reloads:
                    last_exc = exc
                    log(f"[!] {page_reloads} page-reloads exhausted; switching SESSION "
                        f"({msg[:120]})")
                    continue  # browser closes here; outer loop starts a fresh one
                raise exc

            # form accepted — continue with the rest of the flow in this same session
            return _post_form_flow(
                page, context, cfg, email, password, username,
                mail, order_id, log, stop,
            )
            # non-SignupError exceptions propagate immediately (with-block closes browser)
    raise SignupError(
        f"signup form never completed after {page_reloads} page-reloads x "
        f"{session_reloads + 1} sessions: {last_exc}"
    )


def _make_mail_provider(cfg: Config):
    """Factory: create the appropriate mail client based on config."""
    provider = getattr(cfg, "email_provider", "litensi") or "litensi"
    if provider == "tempik":
        return TempikClient(
            api_base=getattr(cfg, "tempik_api_base", "https://tempik.webkarya.net/api"),
            domains=getattr(cfg, "tempik_domains", "webkarya.net"),
        )
    return LitensiClient(cfg.litensi_api_id, cfg.litensi_api_key, cfg.litensi_site, cfg.litensi_zone)


def register_one(
    cfg: Config, log: Callable[[str], None], cancel_cb: Optional[Callable[[], bool]] = None
) -> Optional["AccountRecord"]:
    """Register one account; returns an AccountRecord or None on failure."""
    stop = cancel_cb or (lambda: False)
    # rotate IP before each account to avoid DataDome flagging reused IPs
    if cfg.proxy and getattr(cfg, "rotate_ip_per_account", False):
        _rotate_sticky_proxy()
        log("[*] IP rotated for new account (rotate_ip_per_account=true)")
    provider_name = getattr(cfg, "email_provider", "litensi") or "litensi"
    mail = _make_mail_provider(cfg)
    email, order_id = mail.create_mailbox()
    log(f"[*] mailbox: {email} (provider={provider_name}, order {order_id})")
    try:
        password = generate_password()
        hard_left = int(getattr(cfg, "proxy_hard_block_retries", 0) or 0) if cfg.proxy else 0
        rate_left = int(getattr(cfg, "proxy_rate_limit_retries", 0) or 0) if cfg.proxy else 0
        ipv6_left = 3  # max IPv6 rotations before giving up
        while True:
            _raise_if_cancelled(stop)
            try:
                username, totp_secret, recovery = _run_signup(
                    cfg, email, password, mail, order_id, log, stop
                )
                break
            except SignupError as exc:
                msg = str(exc)
                if "IPv6 exit IP" in msg:
                    if ipv6_left <= 0:
                        log("[!] IPv6 rotation exhausted — proceeding with current IP")
                    else:
                        ipv6_left -= 1
                        log(f"[!] IPv6 detected — rotating to get IPv4 ({ipv6_left} left)")
                        _rotate_sticky_proxy()
                        _sleep_with_cancel(3, stop)
                        continue
                if "not in geoip database" in msg or "not found in database" in msg:
                    if ipv6_left <= 0:
                        log("[!] geoip rotation exhausted — proceeding anyway")
                    else:
                        ipv6_left -= 1
                        log(f"[!] IP not in geoip database — rotating ({ipv6_left} left)")
                        _rotate_sticky_proxy()
                        _sleep_with_cancel(3, stop)
                        continue
                raise
            except SignupBlocked as exc:
                if hard_left <= 0:
                    raise
                hard_left -= 1
                log(f"[!] DataDome hard block ({exc}); rotating sticky proxy, {hard_left} retries left")
                _rotate_sticky_proxy()
                _sleep_with_cancel(5, stop)
            except SignupError as exc:
                # "Sorry, something went wrong" or repeated submit failures
                # → rotate IP and retry (same as hard block treatment)
                msg = str(exc).lower()
                if "something went wrong" in msg or "stayed disabled" in msg:
                    if hard_left <= 0:
                        raise
                    hard_left -= 1
                    log(f"[!] submit failed ({str(exc)[:100]}); rotating IP, {hard_left} retries left")
                    _rotate_sticky_proxy()
                    _sleep_with_cancel(5, stop)
                else:
                    raise
            except GitHubRateLimited as exc:
                if rate_left <= 0:
                    raise
                rate_left -= 1
                log(f"[!] GitHub secondary rate limit ({exc}); rotating sticky proxy/IP, "
                    f"{rate_left} retries left")
                _rotate_sticky_proxy()
                _sleep_with_cancel(8, stop)
        # Recovery codes ride on the record; the legacy 5th marker (has_recovery)
        # is derived at export time, not stored in the flow layer.
        return AccountRecord(
            email=email, username=username, password=password,
            totp_secret=totp_secret, recovery_codes=recovery,
        )
    except KeyboardInterrupt:
        raise
    except RegistrationCancelled:
        raise
    except GitHubRateLimited:
        raise
    except Exception as exc:
        log(f"[-] account failed: {exc}")
        return None
    finally:
        # order already confirmed SUCCESS -> nothing to cancel; else free the mailbox
        if mail.last_order_id:
            log("[*] mailbox already confirmed (SUCCESS) — no cancel needed")
        else:
            _cancel_order(mail, order_id, log)


def run_job(
    cfg: Config,
    cancel_cb: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int, Path]:
    """Register `register_count` accounts; returns (ok, fail, output_file).

    `progress_cb(ok, fail)` (optional) is invoked after each account attempt so
    external observers (e.g. the web UI) can render live stats instead of only
    seeing the final totals when the job returns.
    """
    if log is None:
        log = lambda msg: print(f"[{_now()}] {msg}")  # noqa: E731
    stop = cancel_cb or (lambda: False)

    def _emit_progress(ok_count: int, fail_count: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(ok_count, fail_count)
        except Exception as exc:
            # progress reporting must never break the job
            log(f"[i] progress_cb error ignored: {exc}")

    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    out = ACCOUNTS_DIR / f"github_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    storage = SqliteStorage(DB_PATH)
    job_id = storage.create(JobRecord(target=cfg.register_count))
    job_error = ""
    ok = fail = 0
    log(f"[*] github-regkit | engine=Camoufox (Firefox anti-detect) | site={cfg.litensi_site} "
        f"| headless={cfg.headless} | target={cfg.register_count} | output={out.name}")
    _emit_progress(ok, fail)  # initial snapshot: 0/0
    try:
        for i in range(1, cfg.register_count + 1):
            if stop():
                break
            log(f"--- account {i}/{cfg.register_count} ---")
            record = None
            try:
                record = register_one(cfg, log, stop)
            except KeyboardInterrupt:
                raise
            except RegistrationCancelled:
                log("[!] stop requested — browser flow cancelled")
                break
            except GitHubRateLimited as exc:
                log(f"[!] rate-limit retries exhausted — stopping job: {exc}")
                job_error = str(exc)
                break
            except LitensiError as exc:  # provider-level error: abort job, not just this account
                log(f"[!] litensi error, aborting: {exc}")
                job_error = str(exc)
                break
            if record is not None:
                record.job_id = job_id
                try:
                    storage.add(record)  # single source of truth (encrypted columns)
                    # dual-write the legacy txt for backward compat / manual export
                    enc_line = encrypt(record.to_legacy_line())
                    with out.open("a", encoding="utf-8") as f:
                        f.write(enc_line + "\n")
                    ok += 1
                    log(f"[+] {record.email} saved to {out.name}")
                except Exception as exc:
                    fail += 1
                    log(f"[!] save failed for {record.email}: {exc}")
            else:
                fail += 1
            log(f"[*] stats: OK {ok} | FAIL {fail}")
            _emit_progress(ok, fail)  # live update after each account
            if i < cfg.register_count and not stop():
                _sleep_with_cancel(cfg.delay_sec, stop)
    except RegistrationCancelled:
        # A web Stop click may arrive during inter-account delay, not only
        # inside register_one. This is expected control flow, not a job error.
        log("[!] stop requested — job ended cleanly")
    except Exception as exc:
        job_error = str(exc)
        raise
    finally:
        _stop_proxy_bridge()  # stop the local auth bridge if it was started
        try:
            status = "stopped" if stop() and not job_error else ("error" if job_error else "done")
            storage.finish(job_id, ok=ok, fail=fail, status=status, error=job_error)
        except Exception as exc:
            log(f"[i] job record finish failed: {exc}")
        log(f"[*] done: OK {ok} | FAIL {fail}")
        _emit_progress(ok, fail)  # final snapshot
    return ok, fail, out
