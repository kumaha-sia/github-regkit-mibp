"""GitHub sign-up automation driven by Camoufox (Firefox anti-detect) + Litensi mail."""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from camoufox.sync_api import Camoufox

from .config import Config
from .litensi import LitensiClient, LitensiError
from .profiles import generate_password, generate_username, username_from_email

ROOT = Path(__file__).resolve().parent.parent

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


class SignupError(RuntimeError):
    pass


class SignupBlocked(SignupError):
    pass


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


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_proxy(url: str) -> Optional[dict]:
    """'http://user:pass@host:port' -> Camoufox proxy dict, or None."""
    url = (url or "").strip()
    if not url:
        return None
    p = urlsplit(url)
    if not p.hostname:
        raise SignupError(f"invalid proxy url: {url}")
    port = p.port or (443 if p.scheme == "https" else 80)
    proxy = {"server": f"{p.scheme}://{p.hostname}:{port}"}
    if p.username:
        proxy["username"] = p.username
        proxy["password"] = p.password or ""
    return proxy


def _page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _first(page, selectors: list[str], visible: bool = False):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            if not visible or loc.is_visible():
                return loc
        except Exception:
            continue
    raise SignupError(f"no visible element matching {selectors}")


def _wait_step(page, selectors: list[str], label: str, timeout: int = 30) -> None:
    try:
        page.wait_for_selector(", ".join(selectors), state="visible", timeout=timeout * 1000)
    except Exception:
        raise SignupError(f"{label} did not appear; body={_page_text(page)[:300]!r}")


def _fill(page, selectors: list[str], value: str) -> None:
    _first(page, selectors, visible=True).fill(value)


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


def _cancel_order(lit: LitensiClient, order_id: str, log) -> None:
    try:
        lit.set_status(order_id, "CANCELED")
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


def _open_signup(page, log, attempts: int = 3) -> None:
    """Open github.com/signup and fight through DataDome retries/challenge.

    Strategy: direct goto first; on DataDome, try the human path
    (homepage -> click 'Sign up' link) which carries a warm session,
    then retry direct loads. Manual solve window is given at the end.
    """
    sel = ", ".join(_EMAIL_INPUTS)
    last_hint = ""
    goto_ok = True
    for attempt in range(1, attempts + 1):
        try:
            if goto_ok:
                page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log(f"[!] goto failed ({exc}); retry {attempt}/{attempts}")
            goto_ok = False
        # wait up to 25s for the email form (JS render) or a stable challenge page
        deadline = time.time() + 25
        while time.time() < deadline:
            if _is_hard_block(page):
                raise SignupBlocked(
                    "DataDome HARD BLOCK: 'Access is temporarily restricted' — IP ini diblokir "
                    "sementara oleh GitHub. Solusi: ganti IP (matikan VPN/WARP, ganti jaringan, "
                    "atau isi config proxy residential) lalu ulangi."
                )
            if _form_ready(page):
                log("[*] github.com/signup email form is ready")
                return
            hint = _challenge_hint(page)
            if hint:
                last_hint = hint
                _try_click_datadome(page, log)
            time.sleep(2)
        if last_hint and attempt == 1:
            # human-like navigation: homepage -> Sign up link (warmer session)
            try:
                log("[*] DataDome hit — trying homepage -> 'Sign up' navigation")
                page.goto("https://github.com/", wait_until="domcontentloaded", timeout=60_000)
                if _is_hard_block(page):
                    raise SignupBlocked(
                        "DataDome HARD BLOCK: 'Access is temporarily restricted' — IP ini diblokir "
                        "sementara oleh GitHub. Solusi: ganti IP (matikan VPN/WARP, ganti jaringan, "
                        "atau isi config proxy residential) lalu ulangi."
                    )
                time.sleep(2)
                link = page.get_by_role("link", name="Sign up").first
                if link.count():
                    link.click(timeout=10_000)
                else:
                    page.goto("https://github.com/signup", wait_until="domcontentloaded", timeout=60_000)
                deadline = time.time() + 30
                while time.time() < deadline:
                    if _form_ready(page):
                        log("[*] email form ready via homepage navigation")
                        return
                    _try_click_datadome(page, log)
                    time.sleep(2)
            except Exception as exc:
                log(f"[!] homepage navigation failed: {exc}")
        if attempt < attempts:
            log(f"[!] {last_hint or 'form not ready'} — reload attempt {attempt + 1}/{attempts}")
    if last_hint:
        # final long wait: challenge may need a manual click in the visible window
        log(f"[!] {last_hint} — waiting up to 120s; solve the check in the browser window "
            f"if visible, or configure a residential proxy")
        _try_click_datadome(page, log)
        deadline = time.time() + 120
        while time.time() < deadline:
            if _form_ready(page):
                log("[*] challenge passed, email form is ready")
                return
            time.sleep(2)
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


def _click_create_account(page, log, wait_enabled: int = 30) -> None:
    """Click 'Create account': prefer a NATIVE click on an ENABLED button.

    GitHub gates the button on the octocaptcha token; clicking a disabled
    button via JS is silently ignored by the page. So:
    1. wait up to `wait_enabled`s for the button to become enabled
    2. native-click it (Playwright) — real pointer event, human-like
    3. only if the button never enables, retry the JS click as a fallback
    """
    btn = page.locator("form[action*='signup'] button[type='submit']").first
    deadline = time.time() + wait_enabled
    enabled = False
    while time.time() < deadline:
        try:
            if btn.count() and btn.is_visible() and btn.is_enabled():
                enabled = True
                break
        except Exception:
            pass
        time.sleep(0.8)
    if enabled:
        btn.click(timeout=10_000)
        log("[*] 'Create account' clicked (button enabled)")
        return
    # fallback: JS click even while disabled, then re-check for movement
    page.evaluate(
        """() => {
            const form = document.querySelector("form[action*='signup']");
            const b = form && form.querySelector("button[type='submit']");
            if (b) b.click();
        }"""
    )
    log("[!] 'Create account' still disabled — JS click fallback used")


def _fill_and_create_account(page, base_username: str, tries: int, log) -> str:
    """Fill username, wait 3s, CLICK 'Create account', verify the page reacts.

    If GitHub answers with a username error, append one digit and retry
    (name -> name2 -> name3 ...). Returns the accepted username once the
    page actually moves past the signup form.
    """
    inp = _first(page, _USERNAME_INPUTS, visible=True)
    name = base_username
    for attempt in range(1, tries + 1):
        inp.fill(name)
        time.sleep(3)  # debounce + async availability check (user-requested pause)
        _click_create_account(page, log)

        # wait for reaction: error under username field OR page moving forward
        deadline = time.time() + 15
        reacted = False
        while time.time() < deadline:
            time.sleep(1)
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
        time.sleep(5)
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


def _wait_post_submit(page, context, timeout: int = 120, log=None) -> str:
    """Wait after submit until the state is stable (not 'pending').

    Anti-race: require the state to hold for 2 consecutive checks (≥4s) before
    deciding, so a mid-transition page can't be misread as 'done'.
    """
    stable_state = ""
    stable_hits = 0
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
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
        time.sleep(2)
    raise SignupError(
        f"post-submit state never stabilized; url={page.url} "
        f"body={_page_text(page)[:200]!r}"
    )


def _browser_ctx_options(cfg: Config) -> dict:
    """Launch options tuned for DataDome (see 2026 field guides):

    - persistent profile: keeps the `datadome` cookie (accumulated trust)
    - geoip=True: timezone/locale aligned with the (proxy) exit IP
    - os=host OS: Picasso canvas hash matches the REAL device class we run on
    - headful by default: headless rendering is a Picasso tell
    """
    import platform

    opts = {"headless": cfg.headless, "humanize": True, "geoip": True}
    if platform.system() == "Darwin":
        opts["os"] = "macos"  # canvas/GPU class must match the real machine
    proxy = _parse_proxy(cfg.proxy)
    if proxy:
        opts["proxy"] = proxy
    if cfg.browser_profile_dir:
        opts["persistent_context"] = True
        opts["user_data_dir"] = str((ROOT / cfg.browser_profile_dir).resolve())
    return opts


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
    name = base_name or "hello"
    page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("#repository-name-input", state="visible", timeout=30_000)
    except Exception:
        raise SignupError(f"repo form not found; url={page.url} body={_page_text(page)[:200]!r}")
    inp = page.locator("#repository-name-input").first
    inp.fill(name)
    time.sleep(1.5)  # let GitHub validate + enable the submit button
    # submit: the 'Create repository' button inside the react form
    btn = page.get_by_role("button", name="Create repository").first
    try:
        if not btn.is_visible():
            btn = page.locator("main > react-app button[class*='Button--primary']").first
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if btn.is_enabled():
                    break
            except Exception:
                pass
            time.sleep(0.5)
        btn.click(timeout=10_000)
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
            page.get_by_role("button", name="Create repository").first.click(timeout=10_000)
        time.sleep(1)
    raise SignupError(f"repository creation not confirmed; url={page.url}")


def _enable_2fa(page, log) -> str:
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

    # wait for the setup wizard (QR code page shows the secret in a hidden dialog)
    try:
        page.wait_for_selector(
            "div[data-target='two-factor-setup-verification.mashedSecret']",
            state="attached",  # present in DOM even while the dialog is closed
            timeout=45_000,
        )
    except Exception:
        raise SignupError(f"2FA setup wizard did not load; url={page.url}")

    # reveal the setup key via the 'setup key' button (mirrors the recording),
    # then read the secret from the dialog's data-target div.
    try:
        page.locator("#dialog-show-two-factor-setup-verification-mashed-secret").first.click(
            timeout=10_000
        )
        time.sleep(0.8)
    except Exception:
        pass  # dialog content is in the DOM even when closed — read anyway

    secret = ""
    try:
        secret = (
            page.locator(
                "div[data-target='two-factor-setup-verification.mashedSecret']"
            ).first.inner_text(timeout=5000)
            or ""
        ).strip()
    except Exception:
        pass
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
    return secret


def _run_signup(
    cfg: Config,
    email: str,
    password: str,
    lit: LitensiClient,
    order_id: str,
    log,
    stop,
) -> tuple[str, str]:
    """Run the whole sign-up in one Camoufox browser; returns (username, totp).

    GitHub's signup is now a SINGLE page: Email* / Password* / Username* in one
    form (action=/signup?social=false), submit = "Create account" button.
    OAuth (Google/Apple) buttons live in separate <form> tags — never click them.

    A persistent browser profile (config browser_profile_dir) keeps DataDome
    trust cookies between accounts/runs — fresh-profile browsers get 403'ed.

    Post-login stages (user recording): create repo (stage 4) and enable TOTP
    2FA capturing the secret (stage 5).
    """
    with Camoufox(**_browser_ctx_options(cfg)) as browser:
        context = browser if hasattr(browser, "cookies") else browser.contexts[0]
        # between accounts on a persistent profile: wipe login state, keep trust
        _clean_github_session_cookies(context, log)
        # REUSE the existing page — persistent contexts open one window already;
        # calling new_page() here caused a SECOND window to appear.
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20_000)
        _open_signup(page, log)
        _reject_blocked(page)

        # fill the single-page form: email -> password -> username (from email)
        _fill(page, _EMAIL_INPUTS, email)
        _fill(page, _PASSWORD_INPUTS, password)
        # 3s pause after username -> CLICK Create account -> on username error
        # append one digit and retry (name -> name2 -> name3 ...)
        username = _fill_and_create_account(
            page, username_from_email(email), cfg.max_username_tries, log
        )
        log(f"[*] form submitted: email + password + username={username}")

        # after submit GitHub either shows the email verification (launch code)
        # page, or (high-trust sessions) logs straight in.
        state = _wait_post_submit(page, context, timeout=120, log=log)
        if state == "verify":
            log(f"[*] verification page: {page.url}")
            code = lit.wait_for_code(
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
                delivered = lit.last_order_id or order_id
                lit.mark_success(delivered)
                log(f"[*] litensi order {delivered} confirmed SUCCESS")
            except Exception as exc:
                log(f"[i] litensi confirm SUCCESS failed: {exc}")
            # after OTP: must reach a logged-in state
            state2 = _wait_post_submit(page, context, timeout=90, log=log)
            if state2 == "verify":
                raise SignupError("verification code rejected (still on verify page)")
        # state 'done' required — no more accepting bare redirects
        totp_secret = ""
        deadline = time.time() + 60
        while time.time() < deadline:
            if _logged_in(context):
                log("[*] logged_in cookie confirmed — account is active")
                # ---- stage 4: create first repository ----
                if cfg.create_repo:
                    try:
                        _create_repository(page, username, cfg.repo_name, log)
                    except Exception as exc:
                        log(f"[i] create repo stage skipped: {exc}")
                # ---- stage 5: enable TOTP 2FA ----
                if cfg.enable_2fa:
                    try:
                        totp_secret = _enable_2fa(page, log)
                    except Exception as exc:
                        log(f"[i] 2FA stage failed (account still saved): {exc}")
                return username, totp_secret
            # GitHub sends fresh signups to /login: sign in with the new creds
            if "/login" in (page.url or ""):
                if _try_login(page, email, password, context, log):
                    log("[*] logged_in cookie confirmed after auto-login")
                    # ---- stage 4: create first repository ----
                    if cfg.create_repo:
                        try:
                            _create_repository(page, username, cfg.repo_name, log)
                        except Exception as exc:
                            log(f"[i] create repo stage skipped: {exc}")
                    # ---- stage 5: enable TOTP 2FA ----
                    if cfg.enable_2fa:
                        try:
                            totp_secret = _enable_2fa(page, log)
                        except Exception as exc:
                            log(f"[i] 2FA stage failed (account still saved): {exc}")
                    return username, totp_secret
                raise SignupError("auto-login after signup failed")
            if _post_submit_state(page, context) == "pending":
                time.sleep(2)
                continue
            if _wait_post_submit(page, context, timeout=20, log=log) == "done":
                continue  # loop will hit the _logged_in check above
            time.sleep(2)
        raise SignupError(
            f"account not confirmed logged-in after flow; url={page.url} "
            f"body={_page_text(page)[:200]!r}"
        )
    raise SignupError("browser closed")  # pragma: no cover


def register_one(
    cfg: Config, log: Callable[[str], None], cancel_cb: Optional[Callable[[], bool]] = None
) -> Optional[str]:
    """Register one account. Returns 'email----password----username----totp' or None."""
    stop = cancel_cb or (lambda: False)
    lit = LitensiClient(cfg.litensi_api_id, cfg.litensi_api_key, cfg.litensi_site, cfg.litensi_zone)
    email, order_id = lit.create_mailbox()
    log(f"[*] mailbox: {email} (order {order_id})")
    try:
        password = generate_password()
        username, totp_secret = _run_signup(cfg, email, password, lit, order_id, log, stop)
        return f"{email}----{password}----{username}----{totp_secret}"
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log(f"[-] account failed: {exc}")
        return None
    finally:
        # order already confirmed SUCCESS -> nothing to cancel; else free the mailbox
        if lit.last_order_id:
            log("[*] mailbox already confirmed (SUCCESS) — no cancel needed")
        else:
            _cancel_order(lit, order_id, log)


def run_job(
    cfg: Config,
    cancel_cb: Optional[Callable[[], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> tuple[int, int, Path]:
    """Register `register_count` accounts; returns (ok, fail, output_file)."""
    if log is None:
        log = lambda msg: print(f"[{_now()}] {msg}")  # noqa: E731
    stop = cancel_cb or (lambda: False)
    out = ROOT / f"github_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    ok = fail = 0
    log(f"[*] github-regkit | engine=Camoufox (Firefox anti-detect) | site={cfg.litensi_site} "
        f"| headless={cfg.headless} | target={cfg.register_count} | output={out.name}")
    try:
        for i in range(1, cfg.register_count + 1):
            if stop():
                break
            log(f"--- account {i}/{cfg.register_count} ---")
            line = None
            try:
                line = register_one(cfg, log, stop)
            except KeyboardInterrupt:
                raise
            except LitensiError as exc:  # provider-level error: abort job, not just this account
                log(f"[!] litensi error, aborting: {exc}")
                break
            if line:
                with out.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                ok += 1
                log(f"[+] {line.split('----')[0]} saved to {out.name}")
            else:
                fail += 1
            log(f"[*] stats: OK {ok} | FAIL {fail}")
            if i < cfg.register_count and not stop():
                time.sleep(cfg.delay_sec)
    finally:
        log(f"[*] done: OK {ok} | FAIL {fail}")
    return ok, fail, out
