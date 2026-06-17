"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Used on Linux/Windows (and as macOS fallback when AppleScript is unavailable).
Opens a headed browser, waits for you to solve any captcha, then reads cookies.
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
PASS_TEXT = "Court of Appeal for Ontario"
POLL_INTERVAL = 3
POLL_TRIES = 80  # ~4 minutes


def harvest_cookie_interactive(*, try_auto_solve: bool = False, quiet: bool = False) -> tuple[str, str]:
    """Open Chrome, wait until CanLII loads, return (document.cookie, user_agent)."""
    from seleniumbase import SB

    cookie = ""
    ua = ""
    if not quiet:
        print(
            "\n>>> Opening Chrome — solve any captcha in the browser window.\n"
            "    (Waiting up to ~4 min; closes automatically when done.)\n",
            flush=True,
        )
    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(3)
        for i in range(POLL_TRIES):
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
                    if i == 0:
                        print(f"[browser] auto-solve attempt: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL)
    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured from browser.\n", flush=True)
    return cookie.strip(), ua.strip()
