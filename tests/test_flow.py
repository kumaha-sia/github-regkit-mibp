"""Tests for flow.session and flow.verify. Run: python -m tests.test_flow"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.config import Config
from github_register.flow.session import (
    browser_ctx_options,
    clean_github_session_cookies,
    context_and_page,
    logged_in,
)
from github_register.flow.verify import (
    post_submit_state,
    verify_input_visible,
    verify_page_markers,
)
from github_register.net.proxy import ProxyManager


class FakeCookieContext:
    def __init__(self, cookies=None):
        self._cookies = cookies or []
        self.added = []

    def cookies(self):
        return list(self._cookies)

    def clear_cookies(self):
        self._cookies = []

    def add_cookies(self, cs):
        for c in cs:
            self.added.append(c)
            self._cookies.append(c)


class FakePage:
    def __init__(self, text="", url="", locators_visible=None):
        self._text = text
        self.url = url
        self._vis = locators_visible or {}

    def locator(self, sel):
        return _Locator(self._vis.get(sel, False))

    def content(self):
        return self._text


class _Locator:
    """Playwright locator double with .first / .is_visible() / .count()."""

    def __init__(self, visible: bool):
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible

    def count(self):
        return 1 if self._visible else 0


def test_logged_in_cookie():
    ctx = FakeCookieContext(cookies=[{"name": "logged_in", "value": "yes"}])
    assert logged_in(ctx) is True
    ctx2 = FakeCookieContext(cookies=[{"name": "logged_in", "value": "no"}])
    assert logged_in(ctx2) is False
    assert logged_in(FakeCookieContext()) is False


def test_clean_github_session_cookies():
    cookies = [
        {"name": "logged_in", "value": "yes", "domain": ".github.com", "path": "/"},
        {"name": "user_session", "value": "x", "domain": ".github.com", "path": "/"},
        {"name": "datadome", "value": "trust", "domain": ".github.com", "path": "/"},
    ]
    ctx = FakeCookieContext(cookies=cookies)
    logs = []
    clean_github_session_cookies(ctx, logs.append)
    names = [c["name"] for c in ctx.cookies()]
    assert "logged_in" not in names
    assert "user_session" not in names
    assert "datadome" in names
    assert any("session cookies cleared" in m for m in logs)


def test_browser_ctx_options_no_proxy():
    cfg = Config(proxy="", headless=True)
    pm = ProxyManager()
    opts = browser_ctx_options(cfg, pm)
    assert opts["headless"] is True
    assert opts["humanize"] is True
    assert "proxy" not in opts


def test_browser_ctx_options_fresh_profile():
    cfg = Config(proxy="", headless=False, fresh_profile=True)
    pm = ProxyManager()
    opts = browser_ctx_options(cfg, pm)
    assert "persistent_context" not in opts
    assert "user_data_dir" not in opts


def test_browser_ctx_options_persistent_profile():
    cfg = Config(proxy="", headless=False, fresh_profile=False, browser_profile_dir=".test-profile")
    pm = ProxyManager()
    opts = browser_ctx_options(cfg, pm)
    assert opts.get("persistent_context") is True
    assert "user_data_dir" in opts


def test_context_and_page_browser_context():
    class FakeBrowserContext:
        def cookies(self):
            return []

        pages = [object()]  # already has a page

    ctx, page = context_and_page(FakeBrowserContext())
    assert ctx is not None
    assert page is not None


def test_context_and_page_browser():
    class FakeBrowser:
        def new_context(self, locale=None):
            class Ctx:
                def new_page(self):
                    return "page"
            return Ctx()

    ctx, page = context_and_page(FakeBrowser())
    assert page == "page"


def test_verify_input_visible():
    page = FakePage(locators_visible={"#otp": True})
    assert verify_input_visible(page) is True
    page2 = FakePage(locators_visible={})
    assert verify_input_visible(page2) is False


def test_post_submit_state_verify():
    page = FakePage(
        locators_visible={"#launch-code-0": True},
        url="https://github.com/signup",
    )
    ctx = FakeCookieContext()
    assert post_submit_state(page, ctx) == "verify"


def test_post_submit_state_done():
    page = FakePage(url="https://github.com/dashboard")
    ctx = FakeCookieContext(cookies=[{"name": "logged_in", "value": "yes"}])
    assert post_submit_state(page, ctx) == "done"


def test_post_submit_state_pending():
    page = FakePage(url="https://github.com/signup")
    ctx = FakeCookieContext()
    assert post_submit_state(page, ctx) == "pending"


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all flow tests passed")
