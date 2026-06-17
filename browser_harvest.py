"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Used on Linux/Windows (and as macOS fallback when AppleScript is unavailable).
Opens a headed browser, waits for you to solve any captcha, then reads cookies.
The window stays open until you solve it (or the timeout), so it never closes
out from under you mid-captcha.
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
PASS_TEXT = "Court of Appeal for Ontario"
POLL_INTERVAL = 3
POLL_INTERVAL_FAST = 0.4
POLL_TRIES = 80  # legacy; kept for callers that still import it
DEFAULT_TIMEOUT = 600  # 10 minutes — plenty of time to solve a captcha by hand


def harvest_cookie_interactive(
    *,
    try_auto_solve: bool = False,
    quiet: bool = False,
    fast_poll: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> tuple[str, str]:
    """Open Chrome, keep it open until CanLII loads, return (document.cookie, ua).

    The browser window stays open for up to ``timeout_s`` so you have time to
    solve the captcha; it only closes once a validated cookie is captured or the
    timeout elapses.
    """
    from seleniumbase import SB

    cookie = ""
    ua = ""
    if not quiet:
        mins = int(timeout_s // 60)
        print(
            "\n>>> A Chrome window is opening — SOLVE THE CAPTCHA there.\n"
            f"    It stays open up to {mins} min and closes itself once you pass.\n"
            "    Leave this terminal running.\n",
            flush=True,
        )
    poll = POLL_INTERVAL_FAST if fast_poll else POLL_INTERVAL
    deadline = time.monotonic() + timeout_s
    last_note = time.monotonic()
    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(1 if fast_poll else 3)
        while time.monotonic() < deadline:
            try:
                src = sb.cdp.get_page_source() or ""
            except Exception:
                src = ""
            if PASS_TEXT in src:
                try:
                    cookie = sb.cdp.evaluate("document.cookie") or ""
                    ua = sb.cdp.evaluate("navigator.userAgent") or ""
                except Exception:
                    cookie = ua = ""
                if "datadome=" in cookie:
                    break
            if try_auto_solve:
                try:
                    sb.cdp.solve_captcha()
                except Exception as e:
                    if not quiet:
                        print(f"[browser] auto-solve attempt: {e}", file=sys.stderr)
            if not quiet and time.monotonic() - last_note >= 30:
                remaining = int(deadline - time.monotonic())
                print(f"    still waiting for you to solve the captcha... "
                      f"({remaining}s left)", flush=True)
                last_note = time.monotonic()
            time.sleep(poll)
    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured from browser.\n", flush=True)
    return cookie.strip(), ua.strip()
