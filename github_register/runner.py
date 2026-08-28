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

from .config import Config
from .litensi import LitensiClient, LitensiError
from .tempik import TempikClient
from .profiles import (
    generate_password,
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
from .net.proxy import (
    ProxyManager,
    parse_proxy as _parse_proxy,
    proxy_is_socks as _proxy_is_socks,
    proxy_needs_bridge as _proxy_needs_bridge,
    socks_exit_ip as _socks_exit_ip,
    validate_geoip as _validate_geoip,
)
from .browser.human import (
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
)
from .browser.selectors import (
    EMAIL_INPUTS as _EMAIL_INPUTS,
    LOGIN_PASS_INPUTS as _LOGIN_PASS_INPUTS,
    OTP_INPUTS as _OTP_INPUTS,
    PASSWORD_INPUTS as _PASSWORD_INPUTS,
    SUBMIT_SELECTORS as _SUBMIT_SELECTORS,
    USERNAME_INPUTS as _USERNAME_INPUTS,
    VALIDATION_ALERTS as _VALIDATION_ALERTS,
)
from .detection.datadome import (
    challenge_hint as _challenge_hint,
    is_hard_block as _is_hard_block,
    log_block_ip as _log_block_ip,
    raise_if_rate_limited as _raise_if_rate_limited,
    reject_blocked as _reject_blocked,
    try_click_datadome as _try_click_datadome,
)
from .flow.repo import create_repository as _create_repository
from .flow.twofa import enable_2fa as _enable_2fa
from .flow.profile import complete_profile as _complete_profile
from .flow.signup import (
    click_submit as _click_submit,
    fill_signup_form as _fill_signup_form,
    form_ready as _form_ready,
    open_signup as _open_signup,
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

_LOGIN_USER_INPUTS = ["#login_field", "input[name='login']", "input[type='text']"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    job_id_cb: Optional[Callable[[int], None]] = None,
) -> tuple[int, int, Path]:
    """Register `register_count` accounts; returns (ok, fail, output_file).

    `progress_cb(ok, fail)` (optional) is invoked after each account attempt so
    external observers (e.g. the web UI) can render live stats instead of only
    seeing the final totals when the job returns.

    `job_id_cb(job_id)` (optional) is invoked once with the persistent job row
    id right after it is created, so observers can attach events to the job.
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
    if job_id_cb is not None:
        try:
            job_id_cb(job_id)
        except Exception as exc:
            log(f"[i] job_id_cb error ignored: {exc}")
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
