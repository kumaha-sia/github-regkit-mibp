"""Tests for detection.datadome and browser.selectors. Run: python -m tests.test_detection"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.browser.selectors import (
    EMAIL_INPUTS,
    OTP_INPUTS,
    SUBMIT_SELECTORS,
)
from github_register.detection import datadome
from github_register.errors import GitHubRateLimited, SignupBlocked


class FakePage:
    """Page double with controllable body text/url/content/frames."""

    def __init__(self, text="", url="", html="", frames=None):
        self._text = text
        self.url = url
        self._html = html
        self.frames = frames or []

    def locator(self, sel):
        raise AssertionError("detection must not query the DOM via locator")

    def content(self):
        return self._html


def _page_with_text(monkey_text):
    """datadome reads via browser.human.page_text -> page.locator('body').inner_text."""

    class BodyLoc:
        def inner_text(self, timeout=None):
            return monkey_text

    class P:
        url = "https://github.com/signup"

        def locator(self, sel):
            assert sel == "body"
            return BodyLoc()

    return P()


def test_hard_block_markers_en():
    page = _page_with_text("Your access is temporarily restricted. We detected unusual activity.")
    assert datadome.is_hard_block(page) is True


def test_hard_block_markers_id():
    page = _page_with_text("Akses dibatasi untuk sementara waktu.")
    assert datadome.is_hard_block(page) is True


def test_no_hard_block_on_normal_page():
    page = _page_with_text("Create your account Email address Password Username")
    assert datadome.is_hard_block(page) is False


def test_rate_limit_detection():
    page = _page_with_text("You have exceeded a secondary rate limit. Please wait a few minutes.")
    try:
        datadome.raise_if_rate_limited(page)
    except GitHubRateLimited:
        pass
    else:
        raise AssertionError("must raise on rate limit text")
    normal = _page_with_text("Welcome to GitHub")
    datadome.raise_if_rate_limited(normal)  # no-op


def test_reject_blocked_risk_check():
    page = _page_with_text("Please Login to continue using GitHub")
    try:
        datadome.reject_blocked(page)
    except SignupBlocked as exc:
        assert "login to continue" in str(exc)
    else:
        raise AssertionError("must raise on risk check")


def test_challenge_hint():
    assert "DataDome" in datadome.challenge_hint(FakePage(url="https://geo.captcha-delivery.com/x"))
    assert "DataDome" in datadome.challenge_hint(FakePage(html='<div id="cmsg">'))
    assert "Cloudflare" in datadome.challenge_hint(FakePage(html="<script>cf-chl</script>"))
    assert datadome.challenge_hint(FakePage()) == ""


def test_blocked_ip_extraction():
    page = _page_with_text("Access restricted. IP: 203.0.113.42 has been blocked")
    assert datadome.blocked_ip(page) == "203.0.113.42"
    assert datadome.blocked_ip(_page_with_text("no ip here")) == ""


def test_log_block_ip_diagnoses_leak():
    logs = []
    page = _page_with_text("blocked IP: 198.51.100.7")
    # exit ip matches blocked -> proxy active but flagged
    datadome.log_block_ip(page, logs.append, exit_ip="198.51.100.7")
    assert any("matches proxy exit" in m for m in logs)
    # exit ip differs -> leak warning
    logs.clear()
    datadome.log_block_ip(page, logs.append, exit_ip="203.0.113.99")
    assert any("leaking" in m for m in logs)
    # no exit ip -> falls to the unknown/leak branch (no crash, both lines logged)
    logs.clear()
    datadome.log_block_ip(page, logs.append, exit_ip="")
    assert len(logs) == 2


def test_selectors_shape():
    # every selector list is non-empty and every entry is a css string
    for group in (EMAIL_INPUTS, OTP_INPUTS, SUBMIT_SELECTORS):
        assert group and all(isinstance(s, str) and s for s in group)
    # submit selectors must scope to the signup form first (never OAuth)
    assert SUBMIT_SELECTORS[0].startswith("form[action*='signup']")
    # otp fallback includes the single-digit launch-code boxes
    assert "#launch-code-0" in OTP_INPUTS


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all detection tests passed")
