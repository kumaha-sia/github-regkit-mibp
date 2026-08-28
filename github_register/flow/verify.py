"""Post-submit verification: launch code, auto-login, page-state classifier.

Everything that happens AFTER the signup form is submitted:
- classify the page GitHub shows (verify / done / pending)
- fill the 8-box launch-code page (or single OTP fallback)
- auto-login when GitHub redirects to /login
"""
from __future__ import annotations

import time
from typing import Optional

from ..browser.human import first, page_text, raise_if_cancelled, sleep_with_cancel
from ..browser.selectors import LOGIN_PASS_INPUTS, OTP_INPUTS
from ..detection.datadome import raise_if_rate_limited
from ..errors import SignupError
from ..flow.signup import click_submit
from ..flow.session import logged_in

_LOGIN_INPUTS = ["#login_field", "input[name='login']", "input#login"]

_VERIFY_PAGE_MARKERS = (
    "launch code", "verify your email", "check your email",
    "enter the code", "we sent a code", "verification code",
)

_DONE_MARKERS = (
    "welcome to github", "let's get started", "get started",
    "what do you want to do", "your github journey",
    "your account was created successfully",
)


def verify_input_visible(page) -> bool:
    """Is any e-mail verification code input visible? (launch-code page)"""
    for sel in OTP_INPUTS:
        try:
            if page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


def verify_page_markers(page) -> bool:
    """Text markers of the email-verification ('launch code') page."""
    try:
        text = page_text(page)[:2000].lower()
    except Exception:
        return False
    return any(m in text for m in _VERIFY_PAGE_MARKERS)


def post_submit_state(page, context) -> str:
    """Classify what GitHub shows after 'Create account'.

    Returns one of:
      'verify' — email verification (launch code) page: code input visible or markers
      'done'   — logged in (cookie logged_in=yes) or a welcome/onboarding page
      'pending'— still transitioning
    """
    if verify_input_visible(page):
        return "verify"
    if logged_in(context):
        return "done"
    url = page.url or ""
    text = ""
    try:
        text = page_text(page)[:2000].lower()
    except Exception:
        pass
    if verify_page_markers(page):
        return "verify"
    if "signup" in url:
        return "pending"
    # off /signup without verify markers and without login cookie — ambiguous,
    # treat onboarding/welcome/created-successfully text as done, else pending
    if any(m in text for m in _DONE_MARKERS):
        return "done"
    return "pending"


def wait_post_submit(page, context, timeout: int = 120, log=None, stop=None) -> str:
    """Wait after submit until the state is stable (not 'pending').

    Anti-race: require the state to hold for 2 consecutive checks (>=4s) before
    deciding, so a mid-transition page can't be misread as 'done'.
    """
    stable_state = ""
    stable_hits = 0
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        raise_if_cancelled(stop)
        raise_if_rate_limited(page)
        state = post_submit_state(page, context)
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
        sleep_with_cancel(2, stop)
    raise SignupError(
        f"post-submit state never stabilized; url={page.url} "
        f"body={page_text(page)[:200]!r}"
    )


def fill_launch_code(page, code: str, log) -> None:
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
    otp = first(page, OTP_INPUTS, visible=True)
    if not otp.input_value():
        otp.fill(code)
    click_submit(page)
    log("[*] OTP submitted")


def try_login(page, username: str, password: str, context, log) -> bool:
    """GitHub sends fresh signups to /login: sign in to obtain logged_in=yes.

    Returns True when the login cookie is present afterwards.
    """
    try:
        user = page.locator(", ".join(_LOGIN_INPUTS)).first
        if not user.is_visible():
            return logged_in(context)
        user.fill(username, timeout=5000)
        page.locator(", ".join(LOGIN_PASS_INPUTS)).first.fill(password, timeout=5000)
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
        if logged_in(context):
            return True
        time.sleep(1.5)
    return False
