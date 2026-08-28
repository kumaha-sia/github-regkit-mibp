"""Stage: the signup form itself — warm-up, navigation, fill, submit.

Everything between "browser opened" and "Create account accepted":
- homepage warm-up to earn the DataDome trust cookie
- /signup navigation with challenge retry ladder
- username fill + availability retry (name -> name2 -> name3 ...)
- the create-account click ladder (native -> DOM) that never force-submits
  a disabled form
"""
from __future__ import annotations

import random
import time

from ..browser.human import (
    human_delay,
    human_fill,
    human_mouse_to_element,
    human_random_pause,
    human_scroll,
    page_text,
    raise_if_cancelled,
    sleep_with_cancel,
)
from ..browser.selectors import (
    EMAIL_INPUTS,
    OTP_INPUTS,
    PASSWORD_INPUTS,
    USERNAME_INPUTS,
)
from ..detection.datadome import (
    challenge_hint,
    is_hard_block,
    log_block_ip,
    raise_if_rate_limited,
    try_click_datadome,
)
from ..errors import SignupBlocked, SignupError


def _form_validation_hint(page) -> str:
    """Return a concise visible validation error when Create account is disabled."""
    try:
        alerts = page.locator(
            "[role='alert'], .is-error, .error, .flash-error"
        ).all()
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


def click_submit(page) -> None:
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
    from ..browser.human import first

    first(page, [
        "form[action*='signup'] button[type='submit']",
        "#submit",
        "button[type='submit']",
    ], visible=True).click()


def form_ready(page) -> bool:
    sel = ", ".join(EMAIL_INPUTS)
    try:
        return page.locator(sel).first.is_visible()
    except Exception:
        return False


def homepage_warmup(page, log, stop=None, dwell: int = 12, exit_ip: str = "") -> bool:
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
                sleep_with_cancel(3, stop)
                continue
            log(f"[!] homepage goto failed: {exc}")
            return False
    if not page_loaded:
        return False
    if is_hard_block(page):
        log_block_ip(page, log, exit_ip=exit_ip)
        return False

    # simulate human browsing: mouse movement, scrolling, reading pauses
    scroll_done = False
    for i in range(dwell):
        raise_if_cancelled(stop)
        # random mouse movement (40% chance each second)
        if random.random() < 0.40:
            from ..browser.human import human_mouse_move

            human_mouse_move(page)
        # scroll pattern: down at ~3s, more at ~7s, back up at ~10s
        if i == random.randint(2, 4) and not scroll_done:
            human_scroll(page, "down", random.randint(200, 500))
            scroll_done = True
        elif i == random.randint(6, 8):
            human_scroll(page, "down", random.randint(100, 300))
        elif i == random.randint(9, 11):
            if random.random() < 0.5:
                human_scroll(page, "up", random.randint(100, 300))
        # variable sleep (not fixed 1s)
        human_delay(1.0, 0.3, stop)
        if is_hard_block(page):
            log_block_ip(page, log, exit_ip=exit_ip)
            return False
    log("[*] homepage warm-up complete")
    return True


def open_signup(page, log, attempts: int = 3, stop=None, exit_ip: str = "") -> None:
    """Open github.com/signup with homepage warm-up first.

    Strategy:
      1. Homepage warm-up (12s) — earn DataDome trust cookie before /signup
      2. Navigate to /signup via 'Sign up' link (human path) or direct goto
      3. On DataDome challenge: longer warm-up (20s) + retry
      4. Final: 120s manual solve window
    """
    last_hint = ""

    # --- Phase 1: homepage warm-up before first /signup attempt ---
    if not homepage_warmup(page, log, stop=stop, dwell=12, exit_ip=exit_ip):
        log_block_ip(page, log, exit_ip=exit_ip)
        raise SignupBlocked(
            "DataDome HARD BLOCK on homepage warm-up — this IP is blocked. "
            "Change IP, disable VPN/WARP, or configure a residential proxy."
        )

    for attempt in range(1, attempts + 1):
        raise_if_cancelled(stop)
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
            raise_if_cancelled(stop)
            raise_if_rate_limited(page)
            if is_hard_block(page):
                log_block_ip(page, log, exit_ip=exit_ip)
                # Phase 2: longer warm-up retry on hard block
                if attempt < attempts:
                    log("[!] hard block on /signup — trying longer homepage warm-up (20s)")
                    if homepage_warmup(page, log, stop=stop, dwell=20, exit_ip=exit_ip):
                        break  # warm-up OK, retry /signup in next attempt
                    else:
                        log_block_ip(page, log, exit_ip=exit_ip)
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
            if form_ready(page):
                log("[*] github.com/signup email form is ready")
                return
            hint = challenge_hint(page)
            if hint:
                last_hint = hint
                try_click_datadome(page, log)
            sleep_with_cancel(2, stop)

        if attempt < attempts:
            log(f"[!] {last_hint or 'form not ready'} — reload attempt {attempt + 1}/{attempts}")

    if last_hint:
        # final long wait: challenge may need a manual click in the visible window
        log(f"[!] {last_hint} — waiting up to 120s; solve the check in the browser window "
            f"if visible, or configure a residential proxy")
        try_click_datadome(page, log)
        deadline = time.time() + 120
        while time.time() < deadline:
            raise_if_cancelled(stop)
            raise_if_rate_limited(page)
            if form_ready(page):
                log("[*] challenge passed, email form is ready")
                return
            sleep_with_cancel(2, stop)
    raise SignupError(f"email form did not appear ({last_hint or 'no challenge marker'}); "
                      f"IP is blocked by DataDome — use a residential proxy in config")


def username_error(page) -> str:
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
        text = page_text(page)[:1200].lower()
        if "username is not available" in text or "username is already taken" in text:
            return "taken"
    except Exception:
        pass
    return ""


def dom_click_create_account(page) -> bool:
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


def click_create_account(page, log, wait_enabled: int = 30, stop=None) -> None:
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
        raise_if_cancelled(stop)
        raise_if_rate_limited(page)
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                enabled = True
                break
        except Exception:
            pass
        sleep_with_cancel(0.8, stop)
    if enabled:
        # an invisible/visible Octocaptcha overlay is often what eats the
        # pointer click — poke the captcha frame first so it can finish
        try_click_datadome(page, log)
        # human-like: move mouse to button, hover briefly, then click
        human_mouse_to_element(page, btn)
        human_delay(0.3, 0.15, stop)  # brief hover before click
        try:
            btn.click(timeout=10_000)
            log("[*] 'Create account' clicked (button enabled)")
            return
        except Exception as exc:
            log(f"[i] native click intercepted ({exc}); trying DOM click on enabled button")
            if dom_click_create_account(page):
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


def fill_and_create_account(page, base_username: str, tries: int, log, stop=None) -> str:
    """Fill username, wait 3s, CLICK 'Create account', verify the page reacts.

    If GitHub answers with a username error, append one digit and retry
    (name -> name2 -> name3 ...). Returns the accepted username once the
    page actually moves past the signup form.
    """
    name = base_username
    for attempt in range(1, tries + 1):
        raise_if_cancelled(stop)
        human_fill(page, USERNAME_INPUTS, name, stop=stop)
        # GitHub debounces username availability; wait for the server result.
        # Variable delay: 3-5s (not fixed 3.5s)
        human_delay(3.5, 0.8, stop)
        human_random_pause(stop)
        # scroll down to see the submit button (human behavior)
        human_scroll(page, "down", random.randint(50, 150))
        click_create_account(page, log, stop=stop)

        # wait for reaction: error under username field OR page moving forward
        deadline = time.time() + 15
        reacted = False
        while time.time() < deadline:
            raise_if_cancelled(stop)
            raise_if_rate_limited(page)
            sleep_with_cancel(1, stop)
            err = username_error(page)
            if err == "taken":
                log(f"[*] username {name} taken, retry with +1 digit ({attempt}/{tries})")
                name = f"{base_username}{attempt + 1}"  # name2, name3, ...
                reacted = True
                break
            if err == "invalid":
                raise SignupError(f"username {name} rejected as invalid")
            # page moved on from the signup form -> submit accepted
            if not form_ready(page):
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
        sleep_with_cancel(5, stop)
        if not form_ready(page) or "signup" not in page.url:
            return name
        raise SignupError(
            f"'Create account' did nothing after two clicks (username={name}); "
            f"octocaptcha/DataDome gate never lifted — retry the run or change IP"
        )
    raise SignupError(f"username still taken after {tries} tries (base={base_username})")


def fill_signup_form(page, cfg, email, password, log, stop) -> str:
    """Fill the single-page signup form (email -> password -> username).

    Returns the accepted username. Raises SignupError with a clear reason when
    the form cannot be completed (validation error, overlay, rate limit).
    """
    from ..profiles import username_from_email

    # human-like: scroll down to see the form, random pause to "read" it
    human_scroll(page, "down", random.randint(100, 250))
    human_delay(1.0, 0.4, stop)  # "look at the form"
    human_random_pause(stop)

    # Fill in the same order as a person: email -> wait -> password ->
    # wait -> username. Each blur gives GitHub's async form validators and
    # Octocaptcha time to settle before Create account is considered.
    human_fill(page, EMAIL_INPUTS, email, stop=stop)
    human_delay(1.2, 0.5, stop)  # variable pause after email
    raise_if_rate_limited(page)
    human_random_pause(stop)

    human_fill(page, PASSWORD_INPUTS, password, stop=stop)
    human_delay(1.0, 0.4, stop)  # variable pause after password
    raise_if_rate_limited(page)
    human_random_pause(stop)

    # 3s pause after username -> CLICK Create account -> on username error
    # append one digit and retry (name -> name2 -> name3 ...)
    return fill_and_create_account(
        page, username_from_email(email), cfg.max_username_tries, log, stop=stop
    )
