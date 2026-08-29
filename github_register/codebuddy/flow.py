"""CodeBuddy registration browser flow (Steps 1-7).

Uses an already-registered GitHub account to authorize CodeBuddy via the
device-code OAuth flow:

  Step 1: Router API auth + device-code (before browser)
  Step 2: Browser → verification_uri → agree + "Sign up with GitHub"
  Step 3: GitHub login (if not already logged in)
  Step 4: GitHub 2FA (if enabled on the account)
  Step 5: GitHub OAuth Authorize
  Step 6: First poll (may still be pending)
  Step 7: CodeBuddy region selection + Submit
  Step 8: Re-poll after region submit

Reuses: browser.human (human_delay, human_fill, human_click, page_text, first),
detection.datadome (is_hard_block, challenge_hint, try_click_datadome),
flow.session (context_and_page, browser_ctx_options).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..browser.human import (
    human_click,
    human_delay,
    human_fill,
    human_mouse_to_element,
    human_random_pause,
    human_scroll,
    first,
    page_text,
    raise_if_cancelled,
    sleep_with_cancel,
)
from ..codebuddy.api import RouterClient, RouterError
from ..codebuddy.selectors import (
    AGREE_CHECKBOX,
    ALREADY_AUTHORIZED_MARKERS,
    APP_SUSPENDED_MARKERS,
    AUTHORIZE_BUTTON,
    AUTHORIZE_MARKERS,
    GITHUB_SIGNUP_BUTTON,
    LOGIN_MARKERS,
    LOGIN_PASS_INPUTS,
    LOGIN_USER_INPUTS,
    REGION_INPUT,
    REGION_MARKERS,
    REGION_SEARCH,
    REGION_SUBMIT,
    SIGN_IN_BUTTON,
    TWOFA_INPUTS,
    TWOFA_MARKERS,
    TWOFA_VERIFY_BUTTON,
)
from ..errors import SignupError


@dataclass
class CodeBuddyResult:
    success: bool
    connection_id: Optional[int] = None
    region: str = ""
    error: str = ""
    step: str = ""


def detect_page(page) -> str:
    """Classify the current page by text markers.

    Returns one of: 'login', '2fa', 'authorize', 'region', 'already_authorized',
    'app_suspended', 'unknown'.
    """
    try:
        text = page_text(page)[:3000].lower()
    except Exception:
        return "unknown"
    if any(m in text for m in ALREADY_AUTHORIZED_MARKERS):
        return "already_authorized"
    if any(m in text for m in APP_SUSPENDED_MARKERS):
        return "app_suspended"
    if any(m in text for m in REGION_MARKERS):
        return "region"
    if any(m in text for m in LOGIN_MARKERS):
        return "login"
    if any(m in text for m in TWOFA_MARKERS):
        return "2fa"
    if any(m in text for m in AUTHORIZE_MARKERS):
        return "authorize"
    return "unknown"


def _step1_agree_and_github(page, log, stop) -> None:
    """Step 2: agree checkbox + click 'Sign up with GitHub'."""
    raise_if_cancelled(stop)
    human_scroll(page, "down", 200)
    human_delay(1.0, 0.3, stop)
    # try clicking the agree checkbox first
    try:
        cb = first(page, AGREE_CHECKBOX, visible=True)
        human_click(page, cb)
        log("[*] agreement checkbox clicked")
        human_delay(0.5, 0.2, stop)
    except Exception:
        log("[i] no agree checkbox found — proceeding (may not be required)")
    # click "Sign up with GitHub"
    try:
        btn = first(page, GITHUB_SIGNUP_BUTTON, visible=True)
        human_mouse_to_element(page, btn)
        human_delay(0.3, 0.15, stop)
        btn.click(timeout=10_000)
        log("[*] 'Sign up with GitHub' clicked")
    except Exception as exc:
        # fallback: JS click
        clicked = page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll('button, a')];
                const btn = btns.find(b =>
                    b.offsetParent !== null && !b.disabled &&
                    /github/i.test((b.textContent || '').trim())
                );
                if (btn) { btn.click(); return true; }
                return false;
            }"""
        )
        if clicked:
            log("[*] 'Sign up with GitHub' clicked via DOM fallback")
        else:
            raise SignupError(f"cannot click 'Sign up with GitHub': {exc}")
    human_delay(2.0, 0.5, stop)


def _step2_github_login(page, account, log, stop) -> None:
    """Step 3: fill username + password + click Sign in."""
    raise_if_cancelled(stop)
    log("[*] GitHub login page detected")
    human_delay(1.0, 0.3, stop)
    user_input = first(page, LOGIN_USER_INPUTS, visible=True)
    human_fill(page, LOGIN_USER_INPUTS, account.email, stop=stop)
    human_delay(0.8, 0.3, stop)
    human_fill(page, LOGIN_PASS_INPUTS, account.password, stop=stop)
    human_delay(0.5, 0.2, stop)
    # click "Sign in"
    try:
        btn = first(page, SIGN_IN_BUTTON, visible=True)
        human_click(page, btn)
    except Exception:
        page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='session']");
                if (!form) return;
                let btn = form.querySelector("input[type='submit'], button:not([type='button'])");
                if (!btn) { const btns = form.querySelectorAll('button'); btn = btns[btns.length - 1]; }
                if (btn) btn.click();
            }"""
        )
    log("[*] GitHub login submitted")
    human_delay(2.0, 0.5, stop)


def _step3_github_2fa(page, account, log, stop) -> None:
    """Step 4: generate TOTP + fill + verify."""
    raise_if_cancelled(stop)
    log("[*] GitHub 2FA page detected")
    if not account.totp_secret:
        raise SignupError("account has no TOTP secret but 2FA page is shown")
    import pyotp

    for attempt in range(3):
        raise_if_cancelled(stop)
        totp = pyotp.TOTP(account.totp_secret.strip())
        code = totp.now()
        log(f"[*] TOTP code generated (attempt {attempt + 1}): {code}")
        try:
            otp_input = first(page, TWOFA_INPUTS, visible=True)
            otp_input.fill(code, timeout=5_000)
            human_delay(0.5, 0.2, stop)
            verify_btn = first(page, TWOFA_VERIFY_BUTTON, visible=True)
            human_click(page, verify_btn)
            log("[*] 2FA verify clicked")
        except Exception as exc:
            log(f"[i] 2FA fill/click failed ({exc}); trying DOM fallback")
            page.evaluate(
                f"""() => {{
                    const inp = document.querySelector("input[name='otp'], input[autocomplete='one-time-code']");
                    if (inp) {{ inp.value = '{code}'; inp.dispatchEvent(new Event('input')); }}
                    const btn = document.querySelector("button[type='submit']");
                    if (btn) btn.click();
                }}"""
            )
        human_delay(3.0, 0.5, stop)
        # check if we moved past 2FA
        state = detect_page(page)
        if state != "2fa":
            log(f"[*] 2FA passed — page is now '{state}'")
            return
        log(f"[!] 2FA still on 2FA page (attempt {attempt + 1}); waiting for next TOTP window")
        sleep_with_cancel(5, stop)
    raise SignupError("2FA verification failed after 3 attempts")


def _step4_github_authorize(page, log, stop) -> None:
    """Step 5: click Authorize on the OAuth consent page."""
    raise_if_cancelled(stop)
    log("[*] GitHub OAuth authorize page detected")
    human_delay(1.0, 0.3, stop)
    try:
        btn = first(page, AUTHORIZE_BUTTON, visible=True)
        human_mouse_to_element(page, btn)
        human_delay(0.3, 0.15, stop)
        btn.click(timeout=10_000)
        log("[*] Authorize clicked")
    except Exception as exc:
        # fallback: JS click
        clicked = page.evaluate(
            """() => {
                const form = document.querySelector("form[action*='authorize']");
                if (!form) return false;
                const btn = form.querySelector("input[type='submit'], button[type='submit']");
                if (btn && !btn.disabled) { btn.click(); return true; }
                return false;
            }"""
        )
        if clicked:
            log("[*] Authorize clicked via DOM fallback")
        else:
            raise SignupError(f"cannot click Authorize: {exc}")
    human_delay(3.0, 0.5, stop)


def _step6_select_region(page, preferred_region: str, log, stop) -> str:
    """Step 7: read Current Region, select country, submit.

    Returns the region that was actually selected.
    """
    raise_if_cancelled(stop)
    log("[*] CodeBuddy region selection page detected")
    human_delay(1.0, 0.3, stop)

    # try to read "Current Region <X>" from the page
    detected_region = ""
    try:
        text = page_text(page)[:2000]
        m = re.search(r"Current Region\s+(\w+)", text, re.IGNORECASE)
        if m:
            detected_region = m.group(1).strip()
            log(f"[*] detected Current Region: {detected_region}")
    except Exception:
        pass

    # priority: explicit config > detected Current Region
    region = preferred_region.strip() or detected_region
    if not region:
        log("[!] no preferred region and no Current Region detected — trying 'Singapore'")
        region = "Singapore"

    # open the dropdown
    try:
        inp = first(page, REGION_INPUT, visible=True)
        human_click(page, inp)
        human_delay(0.5, 0.2, stop)
    except Exception as exc:
        log(f"[i] cannot click region input ({exc}) — trying search box")
        try:
            search = first(page, REGION_SEARCH, visible=True)
            search.fill(region)
        except Exception:
            raise SignupError(f"cannot open region dropdown: {exc}")

    # type the region name in the search box
    try:
        search = first(page, REGION_SEARCH, visible=True)
        if search:
            search.fill(region)
            human_delay(0.5, 0.2, stop)
    except Exception:
        pass

    # click the matching list item
    human_delay(1.0, 0.3, stop)
    try:
        item_selectors = [
            f"li:has-text('{region}')",
            f"div:has-text('{region}')",
            f"[role='option']:has-text('{region}')",
            f"button:has-text('{region}')",
        ]
        item = first(page, item_selectors, visible=True)
        human_click(page, item)
        log(f"[*] region selected: {region}")
    except Exception:
        # fallback: JS click
        clicked = page.evaluate(
            f"""() => {{
                const items = [...document.querySelectorAll('li, div[role="option"], button')];
                const item = items.find(i =>
                    i.offsetParent !== null &&
                    i.textContent.trim().toLowerCase().includes('{region.lower()}')
                );
                if (item) {{ item.click(); return true; }}
                return false;
            }}"""
        )
        if clicked:
            log(f"[*] region selected via DOM: {region}")
        else:
            raise SignupError(f"cannot find region '{region}' in dropdown")

    human_delay(1.0, 0.3, stop)

    # click Submit
    try:
        submit_btn = first(page, REGION_SUBMIT, visible=True)
        human_click(page, submit_btn)
        log("[*] region submitted")
    except Exception:
        page.evaluate(
            """() => {
                const btn = [...document.querySelectorAll('button')].find(b =>
                    b.offsetParent !== null && !b.disabled &&
                    /submit/i.test((b.textContent || '').trim())
                );
                if (btn) btn.click();
            }"""
        )
        log("[*] region submitted via DOM fallback")

    human_delay(3.0, 0.5, stop)
    return region


def codebuddy_register(
    page,
    context,
    account,
    cfg,
    log: Callable[[str], None],
    stop: Optional[Callable[[], bool]] = None,
) -> CodeBuddyResult:
    """Full CodeBuddy registration flow (Steps 1-8).

    Args:
        page, context: Playwright page/context.
        account: Account record (email, username, password, totp_secret).
        cfg: Config (router_url, router_password, codebuddy_region, ...).
        log: logging callback.
        stop: cancellation callback.

    Returns:
        CodeBuddyResult with success/failure + connection_id + region.
    """
    stop = stop or (lambda: False)

    # --- Step 1: Router API auth + device code ---
    router_url = getattr(cfg, "router_url", "")
    router_password = getattr(cfg, "router_password", "")
    if not router_url or not router_password:
        return CodeBuddyResult(success=False, error="router_url/router_password not configured", step="api")
    try:
        api = RouterClient(router_url, router_password, log=log)
        api.login()
        dc = api.request_device_code()
    except RouterError as exc:
        return CodeBuddyResult(success=False, error=str(exc), step="api")
    except Exception as exc:
        return CodeBuddyResult(success=False, error=f"router API error: {exc}", step="api")

    device_code = dc["device_code"]
    verification_uri = dc["verification_uri"]
    code_verifier = dc.get("codeVerifier")
    poll_interval = dc.get("interval", 5)

    # --- Step 2: Browser → verification_uri → agree + GitHub signup ---
    try:
        raise_if_cancelled(stop)
        page.goto(verification_uri, wait_until="domcontentloaded", timeout=60_000)
        human_delay(2.0, 0.5, stop)
        _step1_agree_and_github(page, log, stop)
    except SignupError as exc:
        return CodeBuddyResult(success=False, error=str(exc), step="agree+github")

    # --- Steps 3-5: detect and handle GitHub pages ---
    for _ in range(10):  # max 10 detection loops
        raise_if_cancelled(stop)
        state = detect_page(page)
        log(f"[*] page state: {state} | url={page.url}")

        if state == "login":
            try:
                _step2_github_login(page, account, log, stop)
            except SignupError as exc:
                return CodeBuddyResult(success=False, error=str(exc), step="login")
            continue
        if state == "2fa":
            try:
                _step3_github_2fa(page, account, log, stop)
            except SignupError as exc:
                return CodeBuddyResult(success=False, error=str(exc), step="2fa")
            continue
        if state == "authorize":
            try:
                _step4_github_authorize(page, log, stop)
            except SignupError as exc:
                return CodeBuddyResult(success=False, error=str(exc), step="authorize")
            continue
        if state == "already_authorized":
            log("[*] account already authorized on CodeBuddy")
            # poll immediately — connection may already exist
            break
        if state == "app_suspended":
            return CodeBuddyResult(success=False, error="CodeBuddy application suspended", step="authorize")
        if state == "region":
            break  # proceed to region selection
        # unknown — wait and retry
        human_delay(2.0, 0.5, stop)

    # --- Step 6: First poll (may still be pending) ---
    raise_if_cancelled(stop)
    poll1 = api.poll(
        device_code,
        code_verifier=code_verifier,
        interval=poll_interval,
        timeout=30,
        cancel_cb=stop,
    )
    if poll1["success"]:
        conn_id = poll1["connection"].get("id")
        log(f"[*] CodeBuddy connected on first poll: connection_id={conn_id}")
        # may still need region selection
        state = detect_page(page)
        if state == "region":
            region = _step6_select_region(page, getattr(cfg, "codebuddy_region", ""), log, stop)
            return CodeBuddyResult(success=True, connection_id=conn_id, region=region)
        return CodeBuddyResult(success=True, connection_id=conn_id)

    log(f"[*] first poll still pending: {poll1.get('error', 'unknown')}")

    # --- Step 7: Region selection + Submit ---
    state = detect_page(page)
    if state != "region":
        # wait for the page to transition to region selection
        deadline = time.time() + 30
        while time.time() < deadline:
            raise_if_cancelled(stop)
            if detect_page(page) == "region":
                break
            sleep_with_cancel(2, stop)
    if detect_page(page) == "region":
        try:
            region = _step6_select_region(page, getattr(cfg, "codebuddy_region", ""), log, stop)
        except SignupError as exc:
            return CodeBuddyResult(success=False, error=str(exc), step="region")
    else:
        log("[!] region selection page did not appear — proceeding to re-poll")
        region = ""

    # --- Step 8: Re-poll after region submit ---
    raise_if_cancelled(stop)
    poll2 = api.poll(
        device_code,
        code_verifier=code_verifier,
        interval=poll_interval,
        timeout=120,
        cancel_cb=stop,
    )
    if poll2["success"]:
        conn_id = poll2["connection"].get("id")
        log(f"[*] CodeBuddy connected after region submit: connection_id={conn_id}")
        return CodeBuddyResult(success=True, connection_id=conn_id, region=region)

    return CodeBuddyResult(
        success=False,
        error=f"poll timeout after region submit: {poll2.get('error', 'unknown')}",
        region=region,
        step="re-poll",
    )
