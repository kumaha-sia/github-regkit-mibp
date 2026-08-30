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
from .flow.session import (
    browser_ctx_options as _browser_ctx_options,
    clean_github_session_cookies as _clean_github_session_cookies,
    context_and_page as _context_and_page,
    logged_in as _logged_in,
    restore_trust_cookie as _restore_trust_cookie,
    save_trust_cookie as _save_trust_cookie,
)
from .flow.verify import (
    fill_launch_code as _fill_launch_code,
    post_submit_state as _post_submit_state,
    try_login as _try_login,
    wait_post_submit as _wait_post_submit,
)
from . import proxy_health


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


def _post_form_flow(
    page, context, cfg: Config, email: str, password: str, username: str,
    mail, order_id: str, log, stop,
) -> tuple[str, str, str, Any]:
    """Everything AFTER the signup form was accepted: email verification
    (launch code), auto-login, first repository (stage 4), TOTP 2FA (stage 5).
    Returns (username, totp_secret, recovery_codes, cb_result)."""
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
        # confirm the activation: code was used (mark SUCCESS / no-op for tempik)
        try:
            delivered = mail.last_order_id or order_id
            mail.mark_success(delivered)
            provider = getattr(cfg, "email_provider", "litensi") or "litensi"
            log(f"[*] {provider} order {delivered} confirmed SUCCESS")
        except Exception as exc:
            log(f"[i] confirm SUCCESS failed: {exc}")
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
            return _finalize_account(page, context, cfg, email, password, username, log, stop)
        # GitHub sends fresh signups to /login: sign in with the new creds
        if "/login" in (page.url or ""):
            if _try_login(page, email, password, context, log):
                log("[*] logged_in cookie confirmed after auto-login")
                return _finalize_account(page, context, cfg, email, password, username, log, stop)
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


def _simulate_human_activity(page, log, stop):
    """Simulate human reading, starring a repo, and following a user to warm up the account."""
    log("[*] starting human activity simulation (warm-up)")
    try:
        # Repos to star
        repos = ["torvalds/linux", "microsoft/vscode", "facebook/react", "tensorflow/tensorflow"]
        repo = random.choice(repos)
        log(f"[*] human activity: visiting {repo}")
        page.goto(f"https://github.com/{repo}", wait_until="domcontentloaded", timeout=30000)
        _human_delay(1.5, 0.5, stop)
        
        # Scroll down and up a bit
        _human_scroll(page, direction="down", distance=random.randint(200, 800))
        _human_delay(1.0, 0.5, stop)
        _human_scroll(page, direction="up", distance=random.randint(200, 800))
        _human_delay(0.5, 0.5, stop)
        
        # Click the Star button (the primary "Star" button)
        star_btn = _first(page, [
            "form.unstarred button[aria-label^='Star']",
            "button[value='Star']",
            "button:has-text('Star')"
        ], visible=True)
        if star_btn:
            _human_click(page, star_btn, stop)
            log(f"[*] human activity: starred {repo}")
        else:
            log("[i] human activity: star button not found")
            
        _human_delay(2.0, 0.5, stop)

        # Users to follow
        users = ["torvalds", "defunkt", "mojombo", "gaearon", "yyx990803"]
        user = random.choice(users)
        log(f"[*] human activity: visiting profile {user}")
        page.goto(f"https://github.com/{user}", wait_until="domcontentloaded", timeout=30000)
        _human_delay(1.5, 0.5, stop)
        
        _human_scroll(page, direction="down", distance=random.randint(100, 500))
        
        # Click the Follow button
        follow_btn = _first(page, [
            "input[value='Follow']",
            "button[aria-label^='Follow']",
            "button:has-text('Follow')"
        ], visible=True)
        if follow_btn:
            _human_click(page, follow_btn, stop)
            log(f"[*] human activity: followed {user}")
        else:
            log("[i] human activity: follow button not found")

        _human_delay(2.0, 0.5, stop)
        log("[*] human activity simulation completed")
    except Exception as exc:
        log(f"[i] human activity simulation skipped/failed: {exc}")


def _finalize_account(
    page, context, cfg: Config, email: str, password: str, username: str, log, stop
) -> tuple[str, str, str, Any]:
    """Stages after the account is logged in: repo, 2FA, recovery, profile.

    Returns (username, totp_secret, recovery_codes, cb_result). Post-signup stage
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
        
    # Phase: Human Activity Warm-up
    _simulate_human_activity(page, log, stop)
    
    _save_trust_cookie(context, _proxy_manager.exit_ip or "", log)  # persist DataDome trust

    cb_result = None
    if getattr(cfg, "codebuddy_enabled", False):
        log("[*] Single Session Merging: running CodeBuddy registration...")
        try:
            from types import SimpleNamespace
            from .codebuddy.flow import codebuddy_register
            dummy_account = SimpleNamespace(
                email=email,
                password=password,
                username=username,
                totp_secret=totp_secret,
                recovery_codes=recovery
            )
            cb_result = codebuddy_register(page, context, dummy_account, cfg, log, stop)
        except Exception as exc:
            log(f"[-] CodeBuddy Single Session crashed: {exc}")

    return username, totp_secret, recovery, cb_result


def _run_signup(
    cfg: Config,
    email: str,
    password: str,
    mail,  # LitensiClient or TempikClient
    order_id: str,
    log,
    stop,
) -> tuple[str, str, str, Any]:
    """Run the whole sign-up; returns (username, totp, recovery, cb_result).

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
        with Camoufox(**_browser_ctx_options(cfg, _proxy_manager, log=log if session_attempt == 1 else None)) as browser:
            # works for BOTH modes: persistent context (BrowserContext) and fresh
            # launch (Browser -> new context/page per account)
            context, page = _context_and_page(browser)
            if getattr(cfg, "fresh_profile", False):
                # fresh mode: inject ONLY the DataDome trust cookie (no GitHub state)
                _restore_trust_cookie(context, _proxy_manager.exit_ip or "", log)
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
    # proxy rotation: list mode picks next URL; single mode rotates sticky port
    if getattr(cfg, "proxy_mode", "single") == "list" and _proxy_manager.has_proxy_list():
        next_url = _proxy_manager.next_proxy()
        if next_url is None:
            raise SignupError("all proxies exhausted or blacklisted — cannot continue")
        _proxy_manager.proxy_url = next_url
        _proxy_manager._sticky_suffix = None
        _proxy_manager._stop_bridge()
        log(f"[*] proxy switched: {next_url[:50]}... ({_proxy_manager.remaining_proxies()} remaining)")
    elif cfg.proxy and getattr(cfg, "rotate_ip_per_account", False):
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
                username, totp_secret, recovery, cb_result = _run_signup(
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
                # blacklist the exit IP that got blocked
                if _proxy_manager.exit_ip:
                    proxy_health.add_to_blacklist(_proxy_manager.exit_ip)
                    log(f"[!] exit IP {_proxy_manager.exit_ip} blacklisted")
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
        record = AccountRecord(
            email=email, username=username, password=password,
            totp_secret=totp_secret, recovery_codes=recovery,
        )
        record.cb_result = cb_result
        return record
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
    storage = SqliteStorage(DB_PATH)
    proxy_health.init(storage)
    proxy_health.purge_expired()  # drop stale entries at job start
    # proxy handling: "none" = device IP, "single" = one URL, "list" = file
    proxy_mode = getattr(cfg, "proxy_mode", "single")
    if proxy_mode == "none":
        _proxy_manager.set_proxy_list("")
        _proxy_manager.proxy_url = ""
        cfg.proxy = ""  # ensure browser_ctx_options skips proxy entirely
        log("[*] proxy_mode=none — using device IP directly (no proxy)")
    elif proxy_mode == "list":
        raw_proxies = ""
        source = ""
        proxy_file_path = getattr(cfg, "proxy_file", "proxies.txt") or ""
        candidates = []
        if proxy_file_path:
            p = Path(proxy_file_path)
            candidates.append(p if p.is_absolute() else ROOT / p)
        for cand in candidates:
            if cand.is_file():
                try:
                    raw_proxies = cand.read_text(encoding="utf-8")
                    source = str(cand)
                except Exception as exc:
                    log(f"[!] cannot read proxy file {cand}: {exc}")
                break
        if not raw_proxies.strip():
            raw_proxies = cfg.proxy_list or ""
            source = "config.json proxy_list"
        if raw_proxies.strip():
            count = _proxy_manager.set_proxy_list(
                raw_proxies,
                blacklist_fn=proxy_health.is_blacklisted,
            )
            log(f"[*] proxy list loaded from {source}: {count} unique proxies")
        else:
            _proxy_manager.set_proxy_list("")
            log("[!] proxy_mode=list but no proxies found in file or config")
    else:
        _proxy_manager.set_proxy_list("")
        _proxy_manager.proxy_url = cfg.proxy or ""
    job_id = storage.create(JobRecord(target=cfg.register_count))
    if job_id_cb is not None:
        try:
            job_id_cb(job_id)
        except Exception as exc:
            log(f"[i] job_id_cb error ignored: {exc}")
    job_error = ""
    ok = fail = 0
    log(f"[*] github-regkit | engine=Camoufox (Firefox anti-detect) | site={cfg.litensi_site} "
        f"| headless={cfg.headless} | target={cfg.register_count} | output=regkit.db")
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
                    account_id = storage.add(record)  # single source of truth (encrypted columns)
                    record.id = account_id
                    ok += 1
                    log(f"[+] {record.email} saved to database")

                    cb_result = getattr(record, "cb_result", None)
                    if cb_result and cb_result.success:
                        storage.add_codebuddy_account(
                            account_id, cb_result.connection_id or 0, cb_result.region
                        )
                        log(f"[+] CodeBuddy Single Session registered: {record.email} (region={cb_result.region})")
                    elif cb_result:
                        log(f"[-] CodeBuddy Single Session failed for {record.email}: {cb_result.error}")
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
    return ok, fail, Path(DB_PATH)
