"""Stage: set profile status and complete public profile fields."""
from __future__ import annotations

import time

import requests

from ..browser.human import page_text
from ..config import Config
from ..errors import SignupError
from ..profiles import parse_public_profile


def fetch_public_profile() -> dict[str, str]:
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


def _set_status(page, status: str, log) -> None:
    """Open the status popup, type the status, and confirm it saved."""
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
    if not launcher_opened:
        return

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


def _edit_profile_fields(page, username: str, profile: dict, log) -> None:
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
        if profile["name"] not in page_text(page) and name_input.input_value() != profile["name"]:
            raise SignupError("profile save was not confirmed")
    except SignupError:
        raise
    except Exception:
        pass
    log(f"[*] profile completed: {profile['name']} | {profile['location']}")


def complete_profile(page, username: str, cfg: Config, log) -> None:
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
        profile = fetch_public_profile() if not all(custom.values()) else {}
        profile = {key: custom[key] or profile[key] for key in custom}
    try:
        from ..browser.human import first, human_delay, human_mouse_to_element
        avatar = first(page, [
            "button[aria-label='Open user account menu']", 
            "summary[aria-label='View profile and more']",
            "img.avatar-user"
        ], visible=True)
        human_mouse_to_element(page, avatar)
        human_delay(0.5, 0.2)
        avatar.click()
        human_delay(1.5, 0.5)
        
        profile_link = first(page, [
            f"a[href='/{username}']", 
            "a:has-text('Your profile')"
        ], visible=True)
        human_mouse_to_element(page, profile_link)
        human_delay(0.5, 0.2)
        profile_link.click(timeout=15_000)
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception as exc:
        log(f"[i] UI navigation to profile failed ({exc}), falling back to direct URL")
        page.goto(f"https://github.com/{username}", wait_until="domcontentloaded", timeout=60_000)

    if cfg.set_profile_status:
        status = cfg.profile_status.strip() or "On vacation"
        _set_status(page, status, log)

    if not profile:
        return
    _edit_profile_fields(page, username, profile, log)
