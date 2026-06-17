#!/usr/bin/env python3
"""Automated CanLII cookie minter using SeleniumBase CDP (stealth) mode.

Tries auto-solve when Screen Recording + Accessibility are granted.
Otherwise (or if auto-solve fails) keeps Chrome open until you solve the
slider manually — window closes only after pass or when no captcha appears.
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
SOLVE_ATTEMPTS = 6
WAIT_PER_ATTEMPT = 4

LAST_UA = ""


def _mint() -> tuple[str, str]:
    """Launch stealth Chrome; close only when no captcha or captcha solved."""
    from seleniumbase import SB

    cookie = ""
    ua = ""
    captcha_seen = False
    prompt_shown = False
    auto_attempts = 0
    fast_deadline = time.monotonic() + browser_harvest.FAST_EXIT_NO_CAPTCHA

    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(2)

        def page_src() -> str:
            try:
                return sb.cdp.get_page_source() or ""
            except Exception:
                return ""

        while True:
            src = page_src()
            if browser_harvest.page_passed_html(src):
                try:
                    cookie = sb.cdp.evaluate("document.cookie") or ""
                except Exception:
                    cookie = ""
                try:
                    ua = sb.cdp.evaluate("navigator.userAgent") or ""
                except Exception:
                    ua = ""
                if "datadome=" in cookie:
                    break

            if browser_harvest.page_challenged_html(src):
                captcha_seen = True
                if auto_attempts < SOLVE_ATTEMPTS:
                    auto_attempts += 1
                    try:
                        sb.cdp.solve_captcha()
                    except Exception as e:
                        print(f"[sb_mint] auto-solve attempt {auto_attempts}: {e}", file=sys.stderr)
                if not prompt_shown:
                    prompt_shown = True
                    print(
                        "\n>>> Captcha detected — solve the slider in the Chrome window.\n"
                        "    (Window stays open until you pass.)\n",
                        flush=True,
                    )
            elif not captcha_seen and time.monotonic() > fast_deadline:
                break

            sb.sleep(WAIT_PER_ATTEMPT if captcha_seen else browser_harvest.POLL_INTERVAL)

    return cookie.strip(), ua.strip()


def harvest_cookie() -> str:
    global LAST_UA
    cookie, ua = _mint()
    if "datadome=" in cookie:
        LAST_UA = ua
        return cookie
    return ""


def update_session(cookie: str, ua: str = "") -> None:
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
    print("[sb_mint] Launching Chrome for CanLII session...")
    cookie, ua = _mint()
    if "datadome=" not in cookie:
        print(
            "[sb_mint] Did not capture a validated cookie. "
            "If a captcha was shown, solve it in Chrome and run again.",
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
