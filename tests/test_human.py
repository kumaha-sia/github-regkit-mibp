"""Tests for browser.human primitives. Run: python -m tests.test_human"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_register.browser.human import (
    human_delay,
    human_random_pause,
    page_text,
    raise_if_cancelled,
    sleep_with_cancel,
)
from github_register.errors import RegistrationCancelled, SignupError


class FakePage:
    """Minimal page double: evaluate() and locator() calls recorded."""

    def __init__(self):
        self.calls = []

    def evaluate(self, expr):
        self.calls.append(("evaluate", expr))
        return 1280 if "innerWidth" in expr else 720

    def locator(self, sel):
        self.calls.append(("locator", sel))
        raise RuntimeError("not needed in this test")


def test_raise_if_cancelled():
    try:
        raise_if_cancelled(lambda: True)
    except RegistrationCancelled:
        pass
    else:
        raise AssertionError("must raise when stop() is True")
    # None and False are no-ops
    raise_if_cancelled(None)
    raise_if_cancelled(lambda: False)


def test_sleep_with_cancel_interrupts():
    start = time.time()
    try:
        # stop flips to True after 0.2s
        state = {"stop": False}

        def stop():
            return state["stop"]

        import threading

        threading.Timer(0.2, lambda: state.update(stop=True)).start()
        sleep_with_cancel(10.0, stop)
    except RegistrationCancelled:
        elapsed = time.time() - start
        assert elapsed < 2.0, f"cancel took too long: {elapsed:.2f}s"
    else:
        raise AssertionError("must raise during long sleep when stop flips")


def test_human_delay_bounds():
    for _ in range(20):
        start = time.time()
        human_delay(0.05, 0.01, stop=None)
        elapsed = time.time() - start
        # gaussian floor is 0.3s in the implementation
        assert 0.25 <= elapsed < 2.0, elapsed


def test_human_random_pause_mostly_noop():
    # 10% pause chance: over 50 runs almost all should return instantly
    start = time.time()
    for _ in range(50):
        human_random_pause(stop=None)
    elapsed = time.time() - start
    # even a handful of 1-3s pauses stay well under 50 * 1s
    assert elapsed < 30.0, elapsed


def test_page_text_swallows_errors():
    assert page_text(FakePage()) == ""


def test_exceptions_hierarchy():
    assert issubclass(RegistrationCancelled, SignupError)
    assert issubclass(SignupError, RuntimeError)


if __name__ == "__main__":
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_")):
        fn()
        print(f"[OK] {name}")
    print("[*] all human tests passed")
