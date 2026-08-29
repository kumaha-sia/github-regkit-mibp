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
    first_in_frame,
    page_text,
    raise_if_cancelled,
    sleep_with_cancel,
)
from ..codebuddy.api import RouterClient, RouterError
from ..codebuddy.selectors import (
    ACCOUNT_RESTRICTED_MARKERS,
    AGREE_CHECKBOX,
    ALREADY_AUTHORIZED_MARKERS,
    APP_SUSPENDED_MARKERS,
    AUTHORIZE_BUTTON,
    AUTHORIZE_MARKERS,
    GITHUB_SIGNUP_BUTTON,
    LOGIN_MARKERS,
    LOGIN_PASS_INPUTS,
    LOGIN_USER_INPUTS,
    PAGE_EXPIRED_MARKERS,
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
    """Classify the current page by URL + text markers.

    Returns one of: 'login', '2fa', 'authorize', 'region', 'already_authorized',
    'app_suspended', 'codebuddy_login', 'unknown'.

    Checks URL first (fast, reliable for GitHub OAuth pages), then falls
    back to text markers (needed for CodeBuddy pages rendered in iframes).
    """
    try:
        url = page.url.lower()
    except Exception:
        url = ""

    # --- Check iframe URL (GitHub OAuth redirect happens in iframe) ---
    iframe_url = ""
    iframe_frame = None
    try:
        iframe_obj = page.query_selector("iframe")
        if iframe_obj:
            f = iframe_obj.content_frame()
            if f:
                iframe_url = f.url.lower()
                iframe_frame = f
    except Exception:
        pass

    # --- URL-based detection (check both main page + iframe URL) ---
    check_url = f"{url} {iframe_url}".strip() if iframe_url else url

    if "github.com" in check_url:
        if "/sessions/two-factor" in check_url or "/2fa" in check_url:
            return "2fa"
        if "/login" in check_url and "oauth" not in check_url:
            return "login"
        if "/oauth/authorize" in check_url:
            pass  # check text below

    # --- Text-based detection (main page body + iframe body) ---
    texts = []
    try:
        texts.append(page.locator("body").inner_text(timeout=3000))
    except Exception:
        pass
    if iframe_frame:
        try:
            texts.append(iframe_frame.locator("body").inner_text(timeout=3000))
        except Exception:
            pass

    text = " ".join(texts).lower()[:3000]

    if any(m in text for m in ALREADY_AUTHORIZED_MARKERS):
        return "already_authorized"
    if any(m in text for m in APP_SUSPENDED_MARKERS):
        return "app_suspended"
    if any(m in text for m in ACCOUNT_RESTRICTED_MARKERS):
        return "account_restricted"
    if any(m in text for m in PAGE_EXPIRED_MARKERS):
        return "page_expired"
    if any(m in text for m in REGION_MARKERS):
        return "region"
    if any(m in text for m in LOGIN_MARKERS):
        return "login"
    if any(m in text for m in TWOFA_MARKERS):
        return "2fa"
    if any(m in text for m in AUTHORIZE_MARKERS):
        return "authorize"

    # Still on CodeBuddy login page (redirect hasn't happened yet)
    if "codebuddy.ai" in check_url and "/login" in check_url:
        return "codebuddy_login"

    return "unknown"


def _step1_agree_and_github(page, log, stop) -> None:
    """Step 2: agree checkbox + click 'Sign up with GitHub'.

    CodeBuddy login page embeds a Keycloak SSO form inside an <iframe>.
    All interactive elements (checkbox, OAuth buttons) live in the iframe,
    not the main page.

    Key insight: human_click() uses page.mouse which operates in the main
    page coordinate space. But iframe locators return bounding boxes
    relative to the iframe, so mouse moves miss the target. We must use
    locator.click() directly (Playwright handles iframe context automatically)
    or JS dispatch for reliability.
    """
    raise_if_cancelled(stop)

    # SPA: wait for JS to finish loading + rendering the iframe
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass

    # Diagnostic: log page state before iframe wait
    try:
        page_title = page.title()
        page_url = page.url
        log(f"[*] codebuddy: page loaded — title={page_title}")
    except Exception:
        pass

    # Check if page shows an error/expired message instead of iframe
    try:
        body_text = page.evaluate("() => document.body?.innerText?.slice(0, 500) || ''")
        if body_text:
            body_lower = body_text.lower()
            if "expired" in body_lower or "invalid" in body_lower:
                raise SignupError(
                    f"CodeBuddy page shows error: {body_text[:200]}"
                )
    except SignupError:
        raise
    except Exception:
        pass

    # Get the Keycloak iframe — CodeBuddy always renders exactly one
    # Strategy: try multiple iframe selectors (React SPA may use different markup)
    iframe_el = None
    for iframe_selector in ["iframe", "iframe[title*='login']", "iframe[src*='auth']"]:
        try:
            candidate = page.frame_locator(iframe_selector).first
            if candidate.locator("body").count() > 0:
                iframe_el = candidate
                log(f"[*] codebuddy: iframe found via '{iframe_selector}'")
                break
        except Exception:
            continue
    if not iframe_el:
        iframe_el = page.frame_locator("iframe").first  # fallback

    # Wait for the iframe content to render (Keycloak form + OAuth buttons)
    deadline = 30  # seconds total
    elapsed = 0
    iframe_ready = False
    while elapsed < deadline:
        raise_if_cancelled(stop)
        try:
            btn_count = iframe_el.locator(GITHUB_SIGNUP_BUTTON[0]).count()
            if btn_count > 0:
                iframe_ready = True
                break
            # Also check if checkbox is present (alternative readiness signal)
            cb_count = iframe_el.locator("#agree-policy-account").count()
            if cb_count > 0:
                iframe_ready = True
                break
        except Exception:
            pass
        # Every 10 seconds, log diagnostic info
        if elapsed > 0 and elapsed % 10 == 0:
            try:
                iframe_count = page.locator("iframe").count()
                body_snippet = page.evaluate(
                    "() => document.body?.innerHTML?.slice(0, 300) || 'empty'"
                )
                log(
                    f"[i] iframe still not ready ({elapsed}s) — "
                    f"iframe_count={iframe_count}, body={body_snippet[:150]}"
                )
            except Exception:
                pass
        page.wait_for_timeout(1000)
        elapsed += 1

    if not iframe_ready:
        log(f"[!] iframe not ready after {deadline}s — trying anyway")
        # Try reloading the page once
        if not getattr(page, "_cb_reloaded", False):
            try:
                log("[*] codebuddy: reloading verification page...")
                page._cb_reloaded = True
                page.reload(wait_until="networkidle", timeout=30_000)
                human_delay(3.0, 0.5, stop)
                # Re-check iframe after reload
                iframe_el = page.frame_locator("iframe").first
                for _ in range(10):
                    try:
                        btn_count = iframe_el.locator(GITHUB_SIGNUP_BUTTON[0]).count()
                        if btn_count > 0:
                            iframe_ready = True
                            log("[*] codebuddy: iframe ready after reload")
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                if not iframe_ready:
                    raise SignupError(
                        "CodeBuddy iframe did not load after 30s + reload — "
                        "page may show captcha, error, or anti-bot challenge"
                    )
            except SignupError:
                raise
            except Exception as exc:
                log(f"[!] reload failed: {exc}")

    # Get the iframe frame object for JS evaluation
    iframe_frame = None
    try:
        iframe_obj = page.query_selector("iframe")
        if iframe_obj:
            iframe_frame = iframe_obj.content_frame()
    except Exception:
        pass

    # Step 1: Click the agree checkbox (#agree-policy-account)
    # The checkbox has display:none — it's hidden behind a custom <label class="custom-checkbox">
    # with a CSS checkmark. Playwright can't click display:none elements directly.
    # We must click the parent <label> element, which toggles the checkbox via JS.
    checkbox_clicked = False
    try:
        if iframe_frame:
            # Click the <label> wrapper, not the hidden checkbox
            iframe_frame.evaluate(
                """() => {
                    const cb = document.querySelector('#agree-policy-account');
                    if (cb) {
                        const label = cb.closest('label') || cb.parentElement;
                        if (label) label.click();
                    }
                }"""
            )
            human_delay(1.0, 0.2, stop)  # wait for JS to enable OAuth buttons
            # Verify checkbox is checked
            is_checked = iframe_frame.evaluate(
                "() => { const cb = document.querySelector('#agree-policy-account'); return cb ? cb.checked : false; }"
            )
            if is_checked:
                log("[*] agreement checkbox clicked (via label)")
                checkbox_clicked = True
            else:
                log("[!] label click didn't check checkbox — trying direct dispatch")
                raise Exception("checkbox not checked")
        else:
            log("[i] no iframe frame — skipping checkbox")
    except Exception:
        # Fallback: set checked + dispatch events
        try:
            if iframe_frame:
                iframe_frame.evaluate(
                    """() => {
                        const cb = document.querySelector('#agree-policy-account');
                        if (cb) {
                            cb.checked = true;
                            cb.dispatchEvent(new Event('change', {bubbles: true}));
                            if (typeof handlePolicyChange === 'function') handlePolicyChange(cb);
                        }
                    }"""
                )
                human_delay(1.0, 0.2, stop)
                is_checked = iframe_frame.evaluate(
                    "() => { const cb = document.querySelector('#agree-policy-account'); return cb ? cb.checked : false; }"
                )
                if is_checked:
                    log("[*] agreement checkbox checked via JS dispatch")
                    checkbox_clicked = True
                else:
                    log("[i] checkbox JS dispatch failed — proceeding anyway")
            else:
                log("[i] no iframe frame for JS fallback — proceeding")
        except Exception as e:
            log(f"[i] no agree checkbox: {e}")

    # Wait for OAuth buttons to become enabled after checkbox click
    # (Keycloak enables them via JS when checkbox state changes)
    if checkbox_clicked:
        human_delay(1.0, 0.3, stop)  # give JS time to enable buttons

    # Step 2: Click "Sign up with GitHub"
    try:
        btn = first_in_frame(iframe_el, GITHUB_SIGNUP_BUTTON, visible=True)
        # Use direct click — Playwright handles iframe coordinate translation
        btn.click(timeout=10_000)
        log("[*] 'Sign up with GitHub' clicked")
    except Exception as exc:
        # Fallback: JS click inside the iframe via contentDocument
        clicked = False
        if iframe_frame:
            try:
                clicked = iframe_frame.evaluate(
                    """() => {
                        const btn = document.querySelector('#social-github');
                        if (btn) { btn.click(); return true; }
                        // Fallback: find any <a> with GitHub text
                        const links = [...document.querySelectorAll('a')];
                        const gh = links.find(a => /github/i.test(a.textContent || ''));
                        if (gh) { gh.click(); return true; }
                        return false;
                    }"""
                )
            except Exception:
                pass
        if clicked:
            log("[*] 'Sign up with GitHub' clicked via iframe JS fallback")
        else:
            # Last resort: try main page evaluate (cross-origin may fail)
            try:
                clicked = page.evaluate(
                    """() => {
                        const iframe = document.querySelector('iframe');
                        if (!iframe || !iframe.contentDocument) return false;
                        const doc = iframe.contentDocument;
                        const btn = doc.querySelector('#social-github') ||
                            [...doc.querySelectorAll('a')].find(a => /github/i.test(a.textContent || ''));
                        if (btn) { btn.click(); return true; }
                        return false;
                    }"""
                )
            except Exception:
                pass
            if clicked:
                log("[*] 'Sign up with GitHub' clicked via contentDocument fallback")
            else:
                url = page.url
                title = page.title()
                raise SignupError(
                    f"cannot click 'Sign up with GitHub': {exc} "
                    f"(url={url}, title={title})"
                )

    # Step 3: Handle "Service Agreement" popup if it appears
    # CodeBuddy may show a popup with "Confirm" and "Cancel" buttons after
    # clicking the GitHub button. We need to click Confirm to proceed.
    human_delay(1.0, 0.3, stop)  # wait for popup to appear

    popup_handled = False
    # Check inside iframe first (popup may be rendered by Keycloak)
    try:
        iframe_el = page.frame_locator("iframe").first  # re-get fresh
        confirm_btn = iframe_el.locator("button:has-text('Confirm')").first
        if confirm_btn.count() > 0 and confirm_btn.is_visible():
            confirm_btn.click(timeout=5_000)
            log("[*] Service Agreement popup — clicked Confirm (in iframe)")
            popup_handled = True
    except Exception:
        pass

    # Check at page level (popup may be outside iframe)
    if not popup_handled:
        try:
            confirm_btn = page.locator("button:has-text('Confirm')").first
            if confirm_btn.count() > 0 and confirm_btn.is_visible():
                confirm_btn.click(timeout=5_000)
                log("[*] Service Agreement popup — clicked Confirm (page-level)")
                popup_handled = True
        except Exception:
            pass

    # JS fallback: scan all buttons in iframe + page
    if not popup_handled:
        try:
            # Try iframe first
            if iframe_frame:
                clicked = iframe_frame.evaluate(
                    """() => {
                        const btns = [...document.querySelectorAll('button')];
                        const confirm = btns.find(b =>
                            b.offsetParent !== null &&
                            /confirm/i.test(b.textContent.trim())
                        );
                        if (confirm) { confirm.click(); return true; }
                        return false;
                    }"""
                )
                if clicked:
                    log("[*] Service Agreement popup — clicked Confirm (iframe JS)")
                    popup_handled = True
        except Exception:
            pass

    if not popup_handled:
        try:
            clicked = page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll('button')];
                    const confirm = btns.find(b =>
                        b.offsetParent !== null &&
                        /confirm/i.test(b.textContent.trim())
                    );
                    if (confirm) { confirm.click(); return true; }
                    return false;
                }"""
            )
            if clicked:
                log("[*] Service Agreement popup — clicked Confirm (page JS)")
                popup_handled = True
        except Exception:
            pass

    if not popup_handled:
        log("[i] no Service Agreement popup found — proceeding")

    human_delay(2.0, 0.5, stop)


def _step2_github_login(page, account, log, stop) -> None:
    """Step 3: fill username + password + click Sign in.

    GitHub login page is a server-rendered form (not SPA), but after redirect
    from CodeBuddy OAuth it may take a few seconds to fully load. We wait
    for the login field to appear before attempting to fill.
    """
    raise_if_cancelled(stop)
    log("[*] GitHub login page detected")

    # Wait for GitHub login form to fully render (redirect just happened)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    try:
        page.wait_for_selector("#login_field, input[name='login']", timeout=15_000)
    except Exception:
        # Fallback: wait for any input
        try:
            page.wait_for_selector("input[type='text'], input[type='password']", timeout=10_000)
        except Exception:
            pass

    human_delay(1.0, 0.3, stop)

    # Fill username
    try:
        human_fill(page, LOGIN_USER_INPUTS, account.email, stop=stop)
    except Exception as exc:
        # Last resort: JS fill
        try:
            page.evaluate(
                """(email) => {
                    const el = document.querySelector('#login_field, input[name="login"]')
                        || document.querySelector('input[type="text"]');
                    if (el) { el.focus(); el.value = email; el.dispatchEvent(new Event('input', {bubbles: true})); }
                }""",
                account.email,
            )
        except Exception:
            raise SignupError(f"cannot fill GitHub login field: {exc}")
    human_delay(0.8, 0.3, stop)

    # Fill password
    try:
        human_fill(page, LOGIN_PASS_INPUTS, account.password, stop=stop)
    except Exception as exc:
        try:
            page.evaluate(
                """(pw) => {
                    const el = document.querySelector('#password, input[name="password"]')
                        || document.querySelector('input[type="password"]');
                    if (el) { el.focus(); el.value = pw; el.dispatchEvent(new Event('input', {bubbles: true})); }
                }""",
                account.password,
            )
        except Exception:
            raise SignupError(f"cannot fill GitHub password field: {exc}")
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
        # SPA: wait for JS to finish loading before looking for buttons
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        human_delay(2.0, 0.5, stop)
        _step1_agree_and_github(page, log, stop)
    except SignupError as exc:
        return CodeBuddyResult(success=False, error=str(exc), step="agree+github")

    # --- Steps 3-5: detect and handle GitHub pages ---
    for _ in range(15):  # max 15 detection loops (allow time for redirects)
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
        if state == "account_restricted":
            log("[!] CodeBuddy anti-fraud block — account access restricted")
            log("[i] This is NOT a code bug. CodeBuddy's anti-fraud system detected")
            log("[i] the GitHub account or proxy IP as suspicious. Try with a different")
            log("[i] proxy IP or a more aged GitHub account.")
            return CodeBuddyResult(
                success=False,
                error="CodeBuddy account access restricted (anti-fraud)",
                step="authorize",
            )
        if state == "page_expired":
            return CodeBuddyResult(success=False, error="CodeBuddy device code expired", step="authorize")
        if state == "region":
            break  # proceed to region selection
        if state == "codebuddy_login":
            # Still on CodeBuddy login page — GitHub redirect hasn't happened yet
            # Wait a bit for the redirect to complete
            human_delay(2.0, 0.5, stop)
            continue
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
