"""Human-input simulation primitives for Playwright pages.

Every helper mimics a natural user: variable delays, curved mouse paths,
scroll overshoot, typing rhythm. Anti-bot systems (DataDome, Octocaptcha)
flag synthetic regularity, so jitter is load-bearing here — do not
"simplify" these functions.
"""
from __future__ import annotations

import random
import time

from ..errors import RegistrationCancelled


def raise_if_cancelled(stop) -> None:
    if stop and stop():
        raise RegistrationCancelled("stop requested")


def sleep_with_cancel(seconds: float, stop=None) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        raise_if_cancelled(stop)
        time.sleep(min(0.25, deadline - time.time()))


def human_delay(base: float, jitter: float = 0.4, stop=None) -> None:
    """Sleep base ± jitter seconds (Gaussian). Mimics natural human pauses."""
    delay = max(0.3, random.gauss(base, jitter))
    sleep_with_cancel(delay, stop)


def human_random_pause(stop=None) -> None:
    """Occasional random pause (10% chance, 1-3s) — simulates distraction/thinking."""
    if random.random() < 0.10:
        sleep_with_cancel(random.uniform(1.0, 3.0), stop)


def human_scroll(page, direction: str = "down", distance: int = 0) -> None:
    """Scroll like a human: variable speed, slight overshoot, settle back."""
    if not distance:
        distance = random.randint(150, 500)
    if direction == "up":
        distance = -distance
    try:
        # scroll in 2-3 small steps (not one big jump)
        steps = random.randint(2, 3)
        for i in range(steps):
            chunk = distance // steps + random.randint(-20, 20)
            page.evaluate(f"window.scrollBy(0, {chunk})")
            time.sleep(random.uniform(0.08, 0.25))
        # small settle-back (overshoot correction)
        if random.random() < 0.4:
            time.sleep(random.uniform(0.1, 0.3))
            page.evaluate(f"window.scrollBy(0, {-random.randint(10, 40)})")
    except Exception:
        pass


def human_mouse_move(page, target_x: int = 0, target_y: int = 0) -> None:
    """Move mouse like a human: curved path, variable speed, slight wobble.

    If target is (0,0), picks a random position in the viewport.
    """
    try:
        vw = page.evaluate("window.innerWidth") or 1280
        vh = page.evaluate("window.innerHeight") or 720
        if not target_x and not target_y:
            target_x = random.randint(100, vw - 100)
            target_y = random.randint(100, vh - 100)
        # move in 3-5 steps with slight random offsets (bezier-like curve)
        steps = random.randint(3, 5)
        for i in range(1, steps + 1):
            ratio = i / steps
            x = int(target_x * ratio + random.randint(-15, 15))
            y = int(target_y * ratio + random.randint(-15, 15))
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.02, 0.08))
        # final move to exact target
        page.mouse.move(target_x, target_y)
    except Exception:
        pass


def human_mouse_to_element(page, locator) -> tuple[int, int]:
    """Move mouse to an element's bounding box center with human-like path.

    Returns (x, y) of the element center for subsequent click.
    """
    try:
        box = locator.bounding_box(timeout=3000)
        if box:
            # land slightly off-center (humans don't hit exact center)
            x = int(box["x"] + box["width"] * random.uniform(0.3, 0.7))
            y = int(box["y"] + box["height"] * random.uniform(0.3, 0.7))
            human_mouse_move(page, x, y)
            time.sleep(random.uniform(0.05, 0.15))  # brief hover before click
            return x, y
    except Exception:
        pass
    return 0, 0


def human_click(page, locator, timeout: int = 10000) -> None:
    """Click an element with human-like mouse movement + hover + variable delay."""
    human_mouse_to_element(page, locator)
    locator.click(timeout=timeout)


def first(page, selectors: list[str], visible: bool = False):
    """First locator matching any of the selectors (optionally visible) IMMEDIATELY."""
    from ..errors import SignupError

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

def wait_for_first(page, selectors: list[str], visible: bool = False, timeout: int = 15000):
    """Wait and poll until one of the selectors matches (and optionally is visible)."""
    from ..errors import SignupError
    import time
    
    deadline = time.time() + (timeout / 1000.0)
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and (not visible or loc.is_visible()):
                    return loc
            except Exception:
                pass
        time.sleep(0.5)
    raise SignupError(f"timed out waiting for any of {selectors}")

def first_in_frame(frame_locator, selectors: list[str], visible: bool = False):
    """First locator matching any selector **inside a Playwright FrameLocator**.

    Used for CodeBuddy's Keycloak login iframe — all form elements
    (checkbox, OAuth buttons) live inside an <iframe>, not the main page.
    """
    from ..errors import SignupError

    for sel in selectors:
        loc = frame_locator.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            if not visible or loc.is_visible():
                return loc
        except Exception:
            continue
    raise SignupError(f"no visible element matching {selectors} in iframe")


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def wait_step(page, selectors: list[str], label: str, timeout: int = 30) -> None:
    from ..errors import SignupError

    try:
        page.wait_for_selector(", ".join(selectors), state="visible", timeout=timeout * 1000)
    except Exception:
        raise SignupError(f"{label} did not appear; body={page_text(page)[:300]!r}")


def fill(page, selectors: list[str], value: str) -> None:
    first(page, selectors, visible=True).fill(value)


def human_fill(page, selectors: list[str], value: str, stop=None) -> None:
    """Type a signup value progressively so GitHub's async validators run.

    `locator.fill()` injects a whole value in one DOM task. GitHub's signup
    form validates email/password/username and obtains an Octocaptcha token
    asynchronously; instant fills often leave Create account disabled. Typing
    at a modest, consistent pace plus blur matches the normal UI path.
    """
    field = first(page, selectors, visible=True)
    raise_if_cancelled(stop)
    # Do not pointer-click inputs here. GitHub's Octocaptcha can briefly place
    # an invisible overlay above a perfectly valid field, causing click() to
    # time out after "performing click action". DOM focus has the same input
    # semantics without needing a pointer target.
    human_mouse_to_element(page, field)
    try:
        field.focus(timeout=5_000)
    except Exception:
        field.evaluate("el => el.focus()")
    field.fill("")
    # Variable typing speed: base 45-75ms per char, occasional longer pauses
    # simulating natural rhythm (fast bursts + brief thinking pauses)
    for ch in value:
        raise_if_cancelled(stop)
        field.press_sequentially(ch, delay=0)
        # base delay with jitter
        base_ms = random.randint(40, 80)
        # 12% chance of a longer pause (150-350ms) — "thinking about next char"
        if random.random() < 0.12:
            base_ms = random.randint(150, 350)
        time.sleep(base_ms / 1000)
    raise_if_cancelled(stop)
    # brief pause before blur (human reaction time)
    time.sleep(random.uniform(0.15, 0.4))
    try:
        field.evaluate("el => el.blur()")
    except Exception:
        pass
