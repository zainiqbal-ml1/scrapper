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
SOLVE_ATTEMPTS = 6

LAST_UA = ""


def _mint() -> tuple[str, str]:
    from seleniumbase import SB

    cookie = ""
    ua = ""
    prompt_shown = False
    auto_attempts = 0
    tracker = browser_harvest.StablePassTracker()
    stall = browser_harvest.HarvestStallTracker()

    with SB(uc=True, headed=True, locale="en", **tor_util.sb_proxy_kw()) as sb:
        sb.activate_cdp_mode(START_URL)
        print("\n>>> DataDome slider — auto-solving (SeleniumBase + mouse)...\n", flush=True)

        def read_state() -> tuple[str, str, bool, bool, str, str, bool]:
            cdp_ok = True
            url = ""
            try:
                src = sb.cdp.get_page_source() or ""
            except Exception:
                src = ""
                cdp_ok = False
            try:
                url = sb.cdp.evaluate("location.href") or ""
            except Exception:
                cdp_ok = False
            try:
                c = sb.cdp.evaluate("document.cookie") or ""
            except Exception:
                c = ""
                cdp_ok = False
            try:
                u = sb.cdp.evaluate("navigator.userAgent") or ""
            except Exception:
                u = ""
                cdp_ok = False
            ch = browser_harvest.page_challenged_html(src)
            ok = browser_harvest.page_passed_html(src)
            return c, u, ch, ok, src, url, cdp_ok

        while True:
            cookie, ua, challenged, page_ok, src, url, cdp_ok = read_state()

            stall_reason = stall.check(
                src=src, url=url, cookie=cookie, challenged=challenged, cdp_ok=cdp_ok,
            )
            if stall_reason:
                if stall_reason == "ip_blocked":
                    print(">>> Access temporarily blocked — rotating exit.\n", flush=True)
                    raise browser_harvest.HarvestIpBlockedError(stall_reason)
                print(f">>> Harvest stalled ({stall_reason}) — trying another exit.\n", flush=True)
                raise browser_harvest.HarvestConnectivityError(stall_reason)

            if browser_harvest.page_ip_blocked_html(src):
                print(">>> Access temporarily blocked — rotating exit.\n", flush=True)
                raise browser_harvest.HarvestIpBlockedError("ip_blocked")

            if tracker.update(cookie=cookie, challenged=challenged, page_ok=page_ok):
                break

            if browser_harvest.is_datadome_slider_html(src):
                if auto_attempts < SOLVE_ATTEMPTS:
                    auto_attempts += 1
                    import slider_auto

                    slider_auto.try_solve_datadome_slider(
                        sb, quiet=False, overshoot=8.0 + auto_attempts * 5,
                    )
                if not prompt_shown:
                    prompt_shown = True
                    print(">>> Slider detected — dragging...\n", flush=True)
            elif browser_harvest.is_canlii_native_captcha_html(src):
                if not prompt_shown:
                    prompt_shown = True
                    print("\n>>> CanLII captcha — auto-solving (checkbox + OCR)...\n", flush=True)
                elif tracker.should_print_second_hint():
                    print(">>> Second captcha — trying again...\n", flush=True)
                import captcha_auto

                captcha_auto.try_solve(sb, quiet=False)

            sb.sleep(
                2.0 if tracker.captcha_seen else browser_harvest.POLL_INTERVAL
            )

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
