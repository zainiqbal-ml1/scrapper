#!/usr/bin/env python3
"""Automated CanLII cookie minter using SeleniumBase CDP (stealth) mode."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import bootstrap
import browser_harvest
import tor_util

SESSION_FILE = Path("session.py")
COOKIE_STATE = Path(".cookie_state.json")
START_URL = browser_harvest.START_URL

LAST_UA = ""


def _mint(*, juris: str = "on") -> tuple[str, str]:
    from seleniumbase import SB

    with SB(uc=True, headed=True, locale="en", **tor_util.sb_proxy_kw()) as sb:
        sb.activate_cdp_mode(START_URL)
        print("\n>>> DataDome slider — auto-solving (SeleniumBase + mouse)...\n", flush=True)
        return browser_harvest.run_harvest_loop(sb, try_auto_solve=True, quiet=False, juris=juris)


def harvest_cookie(*, juris: str = "on") -> str:
    global LAST_UA
    cookie, ua = _mint(juris=juris)
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
