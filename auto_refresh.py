#!/usr/bin/env python3
"""Auto-refresh the CanLII session via a real Chrome window.

macOS: incognito Chrome via AppleScript (fast, auto-close when cookie captured).
Linux/Windows / Mac without Apple Events: SeleniumBase (auto-solve if permitted).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import bootstrap
import browser_harvest
import platform_util
import tor_util

SESSION_FILE = Path("session.py")
COOKIE_STATE = Path(".cookie_state.json")
AUTO_CAP_CACHE = Path(".auto_solve_capable")
START_URL = browser_harvest.START_URL
POLL_JS = browser_harvest.POLL_JS

# Set when Chrome blocks AppleScript JS (fall back to SeleniumBase).
LAST_MAC_NOJS = False

_harvest_lock = threading.Lock()
IP_COOLDOWN_SEC = 20
_ip_cooldown_until = 0.0
_ip_block_announced = False


def ip_blocked_cooldown_active() -> bool:
    return time.monotonic() < _ip_cooldown_until


def mark_ip_blocked(*, quiet: bool = False) -> None:
    """DataDome IP hard block — log once; harvest retries immediately (no sleep)."""
    global _ip_cooldown_until, _ip_block_announced
    _ip_cooldown_until = max(_ip_cooldown_until, time.monotonic() + IP_COOLDOWN_SEC)
    if not quiet and not _ip_block_announced:
        _ip_block_announced = True
        print(
            "\n>>> Access temporarily blocked — harvesting a fresh cookie...\n",
            flush=True,
        )


def wait_ip_cooldown(*, quiet: bool = False) -> None:
    """No-op (cooldown waits removed)."""
    global _ip_block_announced
    _ip_block_announced = False


def _handle_ip_block(*, quiet: bool = False) -> None:
    mark_ip_blocked(quiet=quiet)
    if tor_util.enabled():
        tor_util.request_new_identity(quiet=quiet)


def _run_as(script: str, *, quiet: bool = False) -> str:
    if not platform_util.has_osascript():
        return ""
    proc = subprocess.run(["osascript", "-e", script], text=True, capture_output=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err and not quiet:
            print(f"[osascript] {err}", file=sys.stderr, flush=True)
        return ""
    return proc.stdout.strip()


AS_OPEN = f'''
tell application "Google Chrome"
  set w to make new window with properties {{mode:"incognito"}}
  set URL of active tab of w to "{START_URL}"
  return index of w as text
end tell
'''

AS_PROBE = '''
tell application "Google Chrome"
  try
    set r to execute active tab of window %s javascript "'PONG'"
    return r
  on error
    return "NOJS"
  end try
end tell
'''


def _as_poll(win_idx: int) -> str:
    js = POLL_JS.replace('"', '\\"')
    return f'''
tell application "Google Chrome"
  try
    set t to active tab of window {win_idx}
    return execute t javascript "{js}"
  on error
    return "|||0|||0"
  end try
end tell
'''


AS_ACTIVATE = '''
tell application "Google Chrome"
  try
    activate
    set index of window %s to 1
  end try
end tell
'''

AS_CLOSE = '''
tell application "Google Chrome"
  try
    close window %s
  end try
end tell
'''

AS_CLOSE_INCOGNITO = '''
tell application "Google Chrome"
  repeat with w in (every window)
    try
      if mode of w is "incognito" then close w
    end try
  end repeat
end tell
'''


def _close_stale_harvest_windows() -> None:
    """Close leftover incognito CanLII windows so only one harvest runs at a time."""
    if platform_util.has_osascript():
        _run_as(AS_CLOSE_INCOGNITO, quiet=True)


def harvest_cookie_macos(*, quiet: bool = False, timeout_s: float | None = None) -> str:
    """macOS: one incognito window; close only when no captcha or captcha solved."""
    global LAST_MAC_NOJS
    LAST_MAC_NOJS = False
    if not platform_util.has_osascript() or not platform_util.chrome_macos_installed():
        LAST_MAC_NOJS = True
        return ""

    with _harvest_lock:
        _close_stale_harvest_windows()
        if not quiet:
            print("\n>>> Opening Chrome incognito...", flush=True)
        raw_idx = _run_as(AS_OPEN, quiet=quiet).strip()
        if not raw_idx.isdigit():
            LAST_MAC_NOJS = True
            if not quiet:
                print("Could not control Chrome (check Automation in System Settings).\n", flush=True)
            return ""
        win_idx = int(raw_idx)
        time.sleep(1.0)
        if _run_as(AS_PROBE % win_idx, quiet=True).strip() != "PONG":
            _run_as(AS_CLOSE % win_idx, quiet=True)
            LAST_MAC_NOJS = True
            platform_util.set_apple_events_works(False)
            if not quiet:
                print("Chrome blocks Apple Events — will use SeleniumBase instead.\n", flush=True)
            return ""
        platform_util.set_apple_events_works(True)

        cookie = ""
        passed = False
        activated = False
        prompt_shown = False
        tracker = browser_harvest.StablePassTracker()
        fast_deadline = time.monotonic() + browser_harvest.FAST_EXIT_NO_CAPTCHA
        no_captcha_deadline = (
            time.monotonic() + (timeout_s or browser_harvest.FAST_EXIT_NO_CAPTCHA)
            if timeout_s
            else fast_deadline
        )

        while True:
            raw = _run_as(_as_poll(win_idx), quiet=True)
            cookie, poll_passed, challenged, ip_blocked = browser_harvest.parse_poll(raw)
            if ip_blocked:
                _run_as(AS_CLOSE % win_idx, quiet=True)
                _handle_ip_block(quiet=quiet)
                break
            page_ok = poll_passed or browser_harvest.cookie_ready(cookie, challenged=False)

            if tracker.update(cookie=cookie, challenged=challenged, page_ok=page_ok):
                passed = True
                break

            if challenged:
                if not activated:
                    _run_as(AS_ACTIVATE % win_idx, quiet=True)
                    activated = True
                if not prompt_shown:
                    prompt_shown = True
                    print(
                        ">>> Captcha — solve it in Chrome (including a second step if shown).\n",
                        flush=True,
                    )
                elif tracker.should_print_second_hint():
                    print(">>> Second captcha — please solve it too.\n", flush=True)
            elif not tracker.captcha_seen and time.monotonic() > no_captcha_deadline:
                break
            time.sleep(browser_harvest.POLL_INTERVAL)

        if passed or not tracker.captcha_seen:
            _run_as(AS_CLOSE % win_idx, quiet=True)

    if passed and cookie and "datadome=" in cookie:
        if not quiet:
            print(">>> Cookie captured — window closed.\n", flush=True)
        return cookie.strip()
    return ""


def harvest_cookie_browser(*, try_auto: bool = False, quiet: bool = False) -> str:
    global LAST_UA
    _close_stale_harvest_windows()
    cookie, ua = browser_harvest.harvest_cookie_interactive(
        try_auto_solve=try_auto, quiet=quiet,
    )
    if "datadome=" in cookie:
        LAST_UA = ua
        return cookie
    return ""


LAST_UA = ""


def auto_solve_capable(*, force_recheck: bool = False) -> bool:
    """True when PyAutoGUI can screenshot + move the mouse (auto slider)."""
    if platform_util.is_linux() and not _has_display():
        return False
    # Only trust a cached True; re-test if missing or False (permissions may have been fixed).
    if not force_recheck and AUTO_CAP_CACHE.exists():
        try:
            if json.loads(AUTO_CAP_CACHE.read_text()) is True:
                return True
        except Exception:
            pass
    ok = False
    err = ""
    try:
        import pyautogui

        pyautogui.FAILSAFE = False
        pyautogui.screenshot()
        start = pyautogui.position()
        pyautogui.moveTo(start[0] + 6, start[1] + 6, duration=0.05)
        time.sleep(0.05)
        end = pyautogui.position()
        pyautogui.moveTo(start[0], start[1], duration=0.05)
        ok = abs(end[0] - (start[0] + 6)) < 4 and abs(end[1] - (start[1] + 6)) < 4
        if not ok:
            err = "mouse did not move — enable Accessibility for the app running Python"
    except Exception as e:
        err = str(e)
        low = err.lower()
        if platform_util.is_macos() and ("screenshot" in low or "screen" in low):
            err = "screenshot failed — enable Screen Recording for the app running Python"
        ok = False
    try:
        if ok:
            AUTO_CAP_CACHE.write_text(json.dumps(True))
        elif AUTO_CAP_CACHE.exists():
            AUTO_CAP_CACHE.unlink()
    except Exception:
        pass
    if not ok and err and platform_util.is_macos() and not force_recheck:
        print(f"[auto_refresh] Auto slider unavailable: {err}", file=sys.stderr, flush=True)
    return ok


def print_harvest_capabilities(*, force_recheck: bool = True) -> bool:
    """Print whether auto slider is available; return capability flag."""
    import os

    term = os.environ.get("TERM_PROGRAM") or os.environ.get("TERM_PROGRAM_VERSION") or "terminal"
    capable = auto_solve_capable(force_recheck=force_recheck)
    if capable:
        print(f"Auto slider: ON (PyAutoGUI OK via {term})", flush=True)
    else:
        print(f"Auto slider: OFF — manual captcha in Chrome ({term})", flush=True)
        if platform_util.is_macos():
            print(
                "  macOS: System Settings → Privacy & Security → Screen Recording + Accessibility\n"
                "  Enable the app you run python from (Terminal.app OR Cursor — not both unless both used).\n"
                "  Quit and reopen that app after granting. Optional: rm -f .auto_solve_capable",
                flush=True,
            )
    return capable


def _try_sb_mint(*, quiet: bool = False) -> str:
    """Auto-solve DataDome slider via SeleniumBase + PyAutoGUI."""
    global LAST_UA
    try:
        import sb_mint

        if not quiet:
            print("\n>>> Auto-solving DataDome slider...\n", flush=True)
        cookie = sb_mint.harvest_cookie()
        if "datadome=" in cookie:
            LAST_UA = sb_mint.LAST_UA
            return cookie.strip()
    except Exception as e:
        if not quiet:
            print(f"[auto_refresh] Auto-solve failed ({e}).", file=sys.stderr, flush=True)
    return ""


def _has_display() -> bool:
    import os
    if platform_util.is_windows():
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _try_auto_slider_first(*, quiet: bool = False) -> str:
    """SeleniumBase + PyAutoGUI before AppleScript (AppleScript cannot drag the slider)."""
    if platform_util.is_macos():
        return _try_sb_mint(quiet=quiet)
    if auto_solve_capable(force_recheck=True):
        return _try_sb_mint(quiet=quiet)
    return ""


def harvest_cookie() -> str:
    """Harvest a validated cookie — auto slider first when permitted."""
    global LAST_UA
    LAST_UA = ""

    if tor_util.enabled():
        tor_util.rotate_for_new_cookie()
        print("Tor on — routing cookie harvest through Tor (SeleniumBase).\n", flush=True)
        cookie = _try_sb_mint(quiet=False)
        if "datadome=" in cookie:
            return cookie
        cookie = harvest_cookie_browser(try_auto=True, quiet=False)
        return cookie if "datadome=" in cookie else ""

    cookie = _try_auto_slider_first(quiet=False)
    if "datadome=" in cookie:
        return cookie
    if platform_util.is_macos():
        print("Auto slider did not finish — opening manual harvest.\n", flush=True)
    elif not auto_solve_capable(force_recheck=True):
        print("Auto slider skipped (permissions not detected for this terminal).\n", flush=True)

    if platform_util.has_osascript() and platform_util.chrome_macos_installed():
        ae_cache = platform_util.APPLE_EVENTS_CACHE
        try_applescript = not ae_cache.exists() or ae_cache.read_text().strip() != "0"
        if try_applescript:
            cookie = harvest_cookie_macos()
            if "datadome=" in cookie:
                return cookie
            if not LAST_MAC_NOJS:
                return ""

    if platform_util.is_linux():
        try:
            from linux_chrome_harvest import harvest_linux_fast

            cookie, ua = harvest_linux_fast(quiet=False)
            if "datadome=" in cookie:
                LAST_UA = ua
                return cookie
        except Exception:
            pass

    cookie = harvest_cookie_browser(
        try_auto=platform_util.is_macos() or auto_solve_capable(force_recheck=True),
    )
    if "datadome=" in cookie:
        return cookie

    return ""


def update_session_cookie(cookie: str, ua: str = "") -> None:
    bootstrap.ensure_session_file()
    src = SESSION_FILE.read_text()
    src = re.sub(r'COOKIE = \(\s*"[^"]*"\s*\)', f'COOKIE = (\n    "{cookie}"\n)', src, count=1, flags=re.S)
    if ua:
        src = re.sub(r'USER_AGENT = \(\s*"[^"]*"\s*\)', f'USER_AGENT = (\n    "{ua}"\n)', src, count=1, flags=re.S)
    SESSION_FILE.write_text(src)
    if COOKIE_STATE.exists():
        COOKIE_STATE.unlink()
    if tor_util.enabled():
        tor_util.mark_cookie_ip()


def main() -> int:
    print(f"Harvest backend: {platform_util.harvest_backend()}")
    cookie = harvest_cookie()
    if "datadome=" not in cookie:
        print("Did not capture a validated cookie.", file=sys.stderr)
        return 1
    dd = re.search(r"datadome=([^;]+)", cookie)
    update_session_cookie(cookie.strip(), LAST_UA)
    print(f"Captured fresh session. datadome={dd.group(1)[:24] if dd else '?'}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
