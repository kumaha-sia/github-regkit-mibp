#!/usr/bin/env python3
"""Camoufox session recorder.

Records manual steps (clicks, fills, navigation) performed in a REAL Camoufox
browser — same launch options as production (persistent profile with DataDome
trust, geoip, os fingerprint, humanize). Output:

  recorded_steps.json  — full event data (action, selector, value, url, ts)
  recorded_steps.py    — human-readable Python preview (codegen-style)

Usage:
    .venv/bin/python record_camoufox.py
    .venv/bin/python record_camoufox.py --url https://github.com/login
    .venv/bin/python record_camoufox.py --out my_flow

Stop by closing the browser window or Ctrl+C. Passwords are masked ('***')
inside the page itself — they never leave the browser in plain text.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from github_register.config import load_config  # noqa: E402
from github_register.runner import _browser_ctx_options  # noqa: E402

RECORDER_JS = r"""
(() => {
  if (window.__recorder_installed) return;
  window.__recorder_installed = true;

  const describe = (el) => ({
    tag: el.tagName ? el.tagName.toLowerCase() : '',
    text: (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .trim().slice(0, 40),
  });

  const selectorFor = (el) => {
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (el.id) return '#' + CSS.escape(el.id);
    if (tag === 'form') {
      const a = el.getAttribute('action');
      if (a) return `form[action="${a}"]`;
    }
    const name = el.getAttribute('name');
    if (name) return `${tag}[name="${name}"]`;
    const testid = el.getAttribute('data-testid');
    if (testid) return `[data-testid="${testid}"]`;
    const aria = el.getAttribute('aria-label');
    if (aria) return `[aria-label="${aria}"]`;
    const ph = el.getAttribute('placeholder');
    if (ph) return `[placeholder="${ph}"]`;
    // structural fallback: shortest nth-of-type chain
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) part += `:nth-of-type(${sibs.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };

  const send = (action, el, value) => {
    try {
      const { tag, text } = describe(el);
      window.__record(JSON.stringify({
        action,
        selector: selectorFor(el),
        tag,
        text: text || null,
        value: value === undefined ? null : String(value),
        url: location.href,
        frame: window === window.top ? null : (location.href || '(iframe)'),
        ts: Date.now(),
      }));
    } catch (e) { /* never break the page */ }
  };

  // clicks — capture phase so we see them before page handlers
  document.addEventListener('click', (e) => {
    const el = e.target.closest(
      'a, button, input[type="submit"], input[type="button"], [role="button"], input, select, label, summary'
    ) || e.target;
    send('click', el);
  }, true);

  // text input (debounced) + password masking + checkbox/radio
  let t = null;
  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!el.matches('input, textarea')) return;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'checkbox' || type === 'radio') return; // handled on change
    if (type === 'password') { send('fill', el, '***'); return; }
    clearTimeout(t);
    t = setTimeout(() => send('fill', el, el.value), 600);
  }, true);

  document.addEventListener('change', (e) => {
    const el = e.target;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'checkbox' || type === 'radio') send('check', el, el.checked);
    else if (el.matches('select')) send('select', el, el.value);
  }, true);

  // explicit form submits (Enter key in a text field fires no click)
  document.addEventListener('submit', (e) => send('submit', e.target), true);
})();
"""


def _dedupe(events: list[dict]) -> list[dict]:
    """Collapse consecutive fills of the same field into the last value."""
    out: list[dict] = []
    for ev in events:
        if (
            ev.get("action") == "fill"
            and out
            and out[-1].get("action") == "fill"
            and out[-1].get("selector") == ev.get("selector")
        ):
            out[-1] = ev
        else:
            out.append(ev)
    return out


def _dump(events: list[dict], base: str) -> None:
    events = _dedupe(events)
    json_path = (ROOT / base).with_suffix(".json")
    py_path = (ROOT / base).with_suffix(".py")

    json_path.write_text(json.dumps(events, indent=2), encoding="utf-8")

    lines = [
        "# Recorded steps (preview) — generated by record_camoufox.py",
        "# Values marked '***' are masked passwords. Translate into runner.py stages.",
        "from playwright.sync_api import sync_playwright",
        "",
    ]
    for ev in events:
        act, sel, val = ev.get("action"), ev.get("selector"), ev.get("value")
        if act == "goto":
            lines.append(f"page.goto({ev['url']!r})")
        elif act == "fill":
            lines.append(f"page.locator({sel!r}).fill({val!r})")
        elif act == "click":
            lines.append(f"page.locator({sel!r}).click()")
        elif act == "check":
            lines.append(f"page.locator({sel!r}).set_checked({str(val).lower() if val in ('True','False',True,False) else 'True'})")
        elif act == "select":
            lines.append(f"page.locator({sel!r}).select_option({val!r})")
        elif act == "submit":
            lines.append(f"# form submit: {sel}")
    py_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for ev in events:
        counts[ev.get("action", "?")] = counts.get(ev.get("action", "?"), 0) + 1
    summary = ", ".join(f"{v}x {k}" for k, v in counts.items())
    print(f"\n[*] saved {len(events)} steps ({summary})")
    print(f"    → {json_path.name}")
    print(f"    → {py_path.name}")


HARD_BLOCK_MARKERS = (
    # English
    "access is temporarily restricted",
    "we detected unusual activity",
    "you have been temporarily blocked",
    # Indonesian (DataDome localizes)
    "akses dibatasi untuk sementara",
    "kami mendeteksi aktivitas yang tidak biasa",
    "ada robot di jaringan",
)


def _is_hard_block(page) -> bool:
    try:
        text = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        return False
    return any(m in text for m in HARD_BLOCK_MARKERS)


def _warn_if_warp_ip(log=print) -> None:
    """Warn early when the exit IP belongs to Cloudflare WARP (DataDome hates it)."""
    try:
        import urllib.request

        with urllib.request.urlopen("http://ip-api.com/json/?fields=query,isp", timeout=8) as r:
            data = json.loads(r.read().decode())
        ip, isp = str(data.get("query") or "?"), str(data.get("isp") or "?")
        log(f"[*] exit IP: {ip} ({isp})")
        if "cloudflare" in isp.lower() or ip.startswith("104.28."):
            log("[!] IP kamu Cloudflare WARP — range ini sering diblokir DataDome.")
            log("    Disarankan matikan WARP (menu bar Cloudflare) untuk rekaman stabil.")
    except Exception:
        pass  # never block the recorder on network diagnostics


def _open_with_warmup(page, url: str, log=print) -> None:
    """Homepage warm-up first (earns DataDome trust), then the target URL.

    On a hard block, does NOT exit: warns, keeps the window open, and lets the
    user solve it manually (or wait it out) — recording continues either way.
    """
    log("[*] warm-up: github.com homepage")
    page.goto("https://github.com/", wait_until="domcontentloaded", timeout=60_000)
    time.sleep(4)  # let DataDome tags.js execute and set the trust cookie
    if _is_hard_block(page):
        log("[!] hard block di homepage — IP ditandai DataDome (WARP/VPN?).")
        log("    Window tetap terbuka: kamu bisa tunggu, reload manual (Cmd+R),")
        log("    atau matikan WARP lalu jalankan ulang recorder ini.")
    else:
        log(f"[*] navigating ke target: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(3)
        if _is_hard_block(page):
            log("[!] hard block di halaman target — window tetap terbuka.")
            log("    Tunggu sebentar lalu reload manual (Cmd+R), atau matikan WARP.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="https://github.com/signup", help="start URL")
    ap.add_argument("--out", default="recorded_steps", help="output base name")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.json")
    opts = _browser_ctx_options(cfg)  # persistent profile + geoip + os + humanize
    opts["headless"] = False  # recording is a manual, visible session

    _warn_if_warp_ip()  # tampil sebelum browser dibuka

    from camoufox.sync_api import Camoufox

    events: list[dict] = []
    live_path = (ROOT / args.out).with_suffix(".json")

    def _flush_live() -> None:
        """Write events to disk on EVERY new event — crash-proof recording."""
        try:
            live_path.write_text(json.dumps(_dedupe(events), indent=2), encoding="utf-8")
        except Exception:
            pass  # never let disk problems kill the recording session

    try:
        with Camoufox(**opts) as browser:
            context = browser  # persistent context IS the browser object

            def on_record(source, payload: str) -> None:
                try:
                    ev = json.loads(payload)
                except Exception:
                    return
                events.append(ev)
                label = ev.get("text") or ""
                val = ev.get("value")
                extra = f" «{label}»" if label else ""
                extra += f" = {val}" if val not in (None, "") else ""
                frame = " [iframe]" if ev.get("frame") else ""
                print(f"[REC] {ev['action']:<6} {ev['selector'][:58]:<58}{extra}{frame}", flush=True)
                _flush_live()  # persist immediately

            context.expose_binding("__record", on_record)
            context.add_init_script(RECORDER_JS)

            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(20_000)
            _open_with_warmup(page, args.url)

            print(f"[*] recorder armed: {args.url}")
            print(f"[*] live-saving tiap event → {live_path.name}")
            print("[*] lakukan step manual — tutup window browser atau Ctrl+C untuk selesai\n")

            last_url = page.url
            while True:
                time.sleep(1)
                try:
                    url = page.url
                except Exception:
                    break  # browser window closed by user
                if url != last_url:
                    events.append({"action": "goto", "url": url, "ts": int(time.time() * 1000)})
                    print(f"[REC] goto   {url[:76]}", flush=True)
                    last_url = url
                    _flush_live()
    except KeyboardInterrupt:
        print("\n[*] Ctrl+C — stopping recorder", flush=True)
    except Exception as exc:
        # browser already closed by the user (TargetClosedError etc.) — that's
        # a NORMAL exit path, not a failure. Recording stays intact.
        if "TargetClosedError" in type(exc).__name__ or "target" in str(exc).lower() or "closed" in str(exc).lower():
            print(f"\n[*] browser ditutup — rekaman tersimpan", flush=True)
        else:
            print(f"\n[!] recorder error: {exc}", flush=True)
    finally:
        if events:
            _dump(events, args.out)
        else:
            print("[!] tidak ada event terekam")
    return 0


if __name__ == "__main__":
    sys.exit(main())
