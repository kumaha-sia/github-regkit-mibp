"""Stage: create the first repository on github.com/new."""
from __future__ import annotations

import time

from ..browser.human import page_text
from ..errors import SignupError

_REPO_NAME_SELECTORS = [
    "#repository-name-input",
    "input[name='repository[name]']",
    "input[aria-label='Repository name']",
    "input[placeholder*='repository' i]",
    "input[placeholder*='repo' i]",
    "input[data-testid='repository-name-input']",
]


def _submit(page, log) -> None:
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


def create_repository(page, username: str, base_name: str, log) -> str:
    """Create the first repository on /new.

    The name field auto-generates a suggestion; we type our own name and submit.
    Returns the repository name created.
    """
    name = base_name or "hello"
    page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
    # try multiple selectors — GitHub may have changed the repo name input
    inp = None
    for sel in _REPO_NAME_SELECTORS:
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
            raise SignupError(f"repo form not found; url={page.url} body={page_text(page)[:300]!r}")
    inp.fill(name)
    time.sleep(1.5)  # let GitHub validate + enable the submit button
    try:
        _submit(page, log)
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
            err = page_text(page)[:600].lower()
        except Exception:
            pass
        if "already exists" in err and "/new" in url:
            log(f"[*] repo {name} exists, retry with suffix")
            name = f"{base_name}{int(time.time()) % 10000}"
            page.goto("https://github.com/new", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("#repository-name-input", state="visible", timeout=20_000)
            page.locator("#repository-name-input").first.fill(name)
            time.sleep(1.5)
            _submit(page, log)
        time.sleep(1)
    raise SignupError(f"repository creation not confirmed; url={page.url}")
