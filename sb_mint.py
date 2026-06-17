#!/usr/bin/env python3
"""Automated CanLII cookie minter using SeleniumBase CDP (stealth) mode.

Unlike auto_refresh.py (which needs you to solve the slider by hand), this
solves the DataDome slider AUTOMATICALLY via SeleniumBase's built-in
`solve_captcha()` (it drives a real mouse drag through PyAutoGUI).

IMPORTANT - macOS permissions (one-time, needs admin):
  System Settings > Privacy & Security >
    - Screen Recording   -> enable for your Terminal / IDE
    - Accessibility      -> enable for your Terminal / IDE
  Without BOTH, the slider drag silently fails (PyAutoGUI can't move the mouse
  or read the screen). On a managed Mac without admin, use auto_refresh.py
  instead and solve the slider yourself.

Usage:
    python sb_mint.py            # mint once, write session.py, exit 0/1
    from sb_mint import harvest_cookie   # returns validated cookie string
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import bootstrap
import browser_harvest

SESSION_FILE = Path("session.py")
COOKIE_STATE = Path(".cookie_state.json")
START_URL = browser_harvest.START_URL
SOLVE_ATTEMPTS = 6      # each attempt: solve_captcha() + wait
WAIT_PER_ATTEMPT = 4    # seconds

# Module-level scratch so callers can read the UA that came with the cookie.
LAST_UA = ""


def _mint() -> tuple[str, str]:
    """Launch stealth Chrome, auto-solve the slider, return (cookie, user_agent)."""
    from seleniumbase import SB

    cookie = ""
    ua = ""
    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(2)

        def page_src() -> str:
            try:
                return sb.cdp.get_page_source() or ""
            except Exception:
                return ""

        captcha_seen = False
        fast_deadline = time.monotonic() + browser_harvest.FAST_EXIT_NO_CAPTCHA

        for i in range(SOLVE_ATTEMPTS):
            src = page_src()
            if browser_harvest.page_passed_html(src):
                break
            if browser_harvest.page_challenged_html(src):
                captcha_seen = True
                try:
                    sb.cdp.solve_captcha()
                except Exception as e:
                    print(f"[sb_mint] solve attempt {i + 1} error: {e}", file=sys.stderr)
            elif not captcha_seen and time.monotonic() > fast_deadline:
                break
            sb.sleep(WAIT_PER_ATTEMPT)

        if browser_harvest.page_passed_html(page_src()):
            try:
                cookie = sb.cdp.evaluate("document.cookie") or ""
            except Exception:
                cookie = ""
            try:
                ua = sb.cdp.evaluate("navigator.userAgent") or ""
            except Exception:
                ua = ""
    return cookie.strip(), ua.strip()


def harvest_cookie() -> str:
    """Mint a fresh validated cookie automatically.

    Returns the full document.cookie string (containing a validated
    `datadome`), or '' on failure. Sets module-level LAST_UA on success so
    callers can keep the User-Agent in sync with the cookie's fingerprint.
    """
    global LAST_UA
    cookie, ua = _mint()
    if "datadome=" in cookie:
        LAST_UA = ua
        return cookie
    return ""


def update_session(cookie: str, ua: str = "") -> None:
    """Write the cookie (and matching UA, if captured) into session.py and
    clear the stale rotated-cookie cache so curl_cffi starts fresh."""
    bootstrap.ensure_session_file()
    src = SESSION_FILE.read_text()
    src = re.sub(
        r'COOKIE = \(\s*"[^"]*"\s*\)',
        f'COOKIE = (\n    "{cookie}"\n)',
        src,
        count=1,
        flags=re.S,
    )
    if ua:
        src = re.sub(
            r'USER_AGENT = \(\s*"[^"]*"\s*\)',
            f'USER_AGENT = (\n    "{ua}"\n)',
            src,
            count=1,
            flags=re.S,
        )
    SESSION_FILE.write_text(src)
    if COOKIE_STATE.exists():
        COOKIE_STATE.unlink()


def main() -> int:
    print("[sb_mint] Launching stealth Chrome and auto-solving the slider...")
    cookie, ua = _mint()
    if "datadome=" not in cookie:
        print(
            "[sb_mint] Failed to mint a validated cookie. "
            "Check Screen Recording + Accessibility permissions, "
            "or fall back to: python auto_refresh.py",
            file=sys.stderr,
        )
        return 1
    update_session(cookie, ua)
    dd = re.search(r"datadome=([^;]+)", cookie)
    print(f"[sb_mint] Minted fresh session. datadome={dd.group(1)[:24] if dd else '?'}...")
    if ua:
        print(f"[sb_mint] User-Agent synced: {ua[:60]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
