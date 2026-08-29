"""Tests for CodeBuddy browser flow. Run: python -m tests.test_codebuddy_flow"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.codebuddy.flow import detect_page, CodeBuddyResult
from github_register.codebuddy.selectors import LOGIN_MARKERS, TWOFA_MARKERS, AUTHORIZE_MARKERS, REGION_MARKERS
from github_register.storage.models import Account


class FakeLocator:
    def __init__(self, visible=True, count_val=1):
        self._visible = visible
        self._count = count_val

    @property
    def first(self):
        return self

    def is_visible(self):
        return self._visible

    def count(self):
        return self._count

    def fill(self, value, timeout=None):
        pass

    def click(self, timeout=None):
        pass

    def inner_text(self, timeout=None):
        return ""

    def bounding_box(self, timeout=None):
        return {"x": 100, "y": 100, "width": 50, "height": 30}


class FakeBodyLocator:
    """Locator for 'body' selector — returns the page text."""

    def __init__(self, text=""):
        self._text = text

    @property
    def first(self):
        return self

    def is_visible(self):
        return True

    def count(self):
        return 1

    def inner_text(self, timeout=None):
        return self._text

    def fill(self, value, timeout=None):
        pass

    def click(self, timeout=None):
        pass

    def bounding_box(self, timeout=None):
        return {"x": 100, "y": 100, "width": 50, "height": 30}


class FakePage:
    def __init__(self, text="", url="https://www.codebuddy.ai"):
        self._text = text
        self.url = url
        self.frames = []
        self._eval_result = True

    def locator(self, sel):
        if sel == "body":
            return FakeBodyLocator(self._text)
        return FakeLocator()

    def content(self):
        return self._text

    def evaluate(self, js, *args):
        return self._eval_result

    def goto(self, url, **kw):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def get_by_role(self, role, **kw):
        return FakeLocator()


class FakeContext:
    def cookies(self):
        return []


def test_detect_page_login():
    page = FakePage(text="Sign in to GitHub to continue to Tencent Buddy Agent")
    assert detect_page(page) == "login"


def test_detect_page_2fa():
    page = FakePage(text="Two-factor authentication Enter the code from your app")
    assert detect_page(page) == "2fa"


def test_detect_page_authorize():
    page = FakePage(text="Tencent Buddy Agent wants access to your GitHub account")
    assert detect_page(page) == "authorize"


def test_detect_page_region():
    page = FakePage(text="Select Registration Region to Get Started")
    assert detect_page(page) == "region"


def test_detect_page_already_authorized():
    page = FakePage(text="You have already authorized this application")
    assert detect_page(page) == "already_authorized"


def test_detect_page_app_suspended():
    page = FakePage(text="The OAuth application has been suspended")
    assert detect_page(page) == "app_suspended"


def test_detect_page_unknown():
    page = FakePage(text="Welcome to CodeBuddy dashboard!")
    assert detect_page(page) == "unknown"


def test_codebuddy_result_dataclass():
    r = CodeBuddyResult(success=True, connection_id=42, region="Singapore")
    assert r.success and r.connection_id == 42 and r.region == "Singapore"
    r2 = CodeBuddyResult(success=False, error="timeout", step="re-poll")
    assert not r2.success and r2.error == "timeout"


def test_codebuddy_register_no_router_config():
    """Must return failure when router_url/password not configured."""
    from github_register.codebuddy.flow import codebuddy_register
    from github_register.config import Config

    page = FakePage()
    context = FakeContext()
    account = Account(email="x@y.com", username="x", password="p", totp_secret="T" * 16)
    cfg = Config(router_url="", router_password="")
    result = codebuddy_register(page, context, account, cfg, log=lambda m: None)
    assert result.success is False
    assert "router" in result.error.lower()
    assert result.step == "api"


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all codebuddy flow tests passed")
