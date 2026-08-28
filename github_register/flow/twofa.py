"""Stage: enable TOTP 2FA and capture the secret + recovery codes."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime

from ..browser.human import page_text
from ..errors import SignupError

_WIZARD_SELECTORS = [
    "div[data-target='two-factor-setup-verification.mashedSecret']",
    "[data-target*='mashedSecret']",
    "[data-target*='two-factor']",
    "div[role='dialog']",
    "#two-factor-setup",
]

_REVEAL_SELECTORS = [
    "#dialog-show-two-factor-setup-verification-mashed-secret",
    "button:has-text('setup key')",
    "button:has-text('Setup key')",
    "button:has-text('text code')",
    "details summary:has-text('setup key')",
]

_SECRET_SELECTORS = [
    "div[data-target='two-factor-setup-verification.mashedSecret']",
    "[data-target*='mashedSecret']",
    "code[data-target*='secret']",
    "samp",
    "code",
]

_BASE32_RE = re.compile(r"\b([A-Z2-7]{16,32})\b")
_CODE_RE = re.compile(r"\b[a-z0-9]{5,6}-[a-z0-9]{5,6}\b", re.I)


def _click_active_wizard_button(page, label: str) -> bool:
    """Click the VISIBLE enabled wizard button by its label.

    The wizard keeps all steps' buttons in the DOM; Playwright's is_visible()
    is unreliable there, so use the browser's own visibility semantics
    (offsetParent !== null) to find the ACTIVE step's button.
    """
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


def _click_visible_next_button(page) -> None:
    """Fallback: click any visible enabled next button (its label may be icon-only)."""
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


def _extract_codes(txt: str) -> list[str]:
    return list(dict.fromkeys(_CODE_RE.findall(txt)))


def enable_2fa(page, log) -> tuple[str, str]:
    """Enable TOTP 2FA and return the secret.

    Flow (from the user recording):
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
    wizard_loaded = False
    for sel in _WIZARD_SELECTORS:
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
    for sel in _REVEAL_SELECTORS:
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
    for sel in _SECRET_SELECTORS:
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
        try:
            body = page.content()
        except Exception:
            body = ""
        m = _BASE32_RE.search(body or "")
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

    if not _click_active_wizard_button(page, "Continue"):
        _click_visible_next_button(page)
    log("[*] TOTP code submitted → Continue")
    time.sleep(3)

    # ---- recovery codes step ----
    recovery = ""
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            # prefer the dedicated element, else scan the page text
            try:
                rc_el = page.locator(
                    "two-factor-setup-recovery-codes, "
                    "[data-target='two-factor-setup-recovery-codes']"
                )
                if rc_el.count():
                    txt = rc_el.first.inner_text(timeout=3000) or ""
                else:
                    txt = page_text(page)
            except Exception:
                txt = page_text(page)

            codes = _extract_codes(txt)
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
                    codes = _extract_codes(dl_text)
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
        _click_visible_next_button(page)
        log("[*] recovery codes confirmed (fallback)")
    time.sleep(2)
    if _click_active_wizard_button(page, "Done"):
        log("[*] 2FA wizard finished")
    else:
        _click_visible_next_button(page)
    time.sleep(2)
    return secret, recovery
