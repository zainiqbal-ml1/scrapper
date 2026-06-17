#!/usr/bin/env python3
"""Auto-refresh the CanLII session via a real Chrome window.

macOS: opens incognito Chrome via AppleScript (or SeleniumBase fallback).
Linux/Windows: opens Chrome via SeleniumBase (no osascript).

You solve any captcha in the window; cookies are read automatically.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import bootstrap
import browser_harvest
import platform_util

SESSION_FILE = Path("session.py")
COOKIE_STATE = Path(".cookie_state.json")
AUTO_CAP_CACHE = Path(".auto_solve_capable")
START_URL = browser_harvest.START_URL
POLL_INTERVAL = browser_harvest.POLL_INTERVAL
POLL_TRIES = browser_harvest.POLL_TRIES
PASS_JS = "document.body && document.body.innerText.indexOf('Court of Appeal for Ontario')>-1 ? '1':'0'"

APPLESCRIPT = '''
tell application "Google Chrome"
  activate
  set w to make new window with properties {mode:"incognito"}
  set URL of active tab of w to "%s"
end tell
set grabbed to ""
repeat %d times
  delay %d
  tell application "Google Chrome"
    try
      set passed to execute active tab of w javascript "%s"
    on error
      set passed to "0"
    end try
    if passed is "1" then
      try
        set grabbed to execute active tab of w javascript "document.cookie"
      end try
      if grabbed contains "datadome=" then exit repeat
    end if
  end tell
end repeat
tell application "Google Chrome"
  try
    close w
  end try
end tell
return grabbed
''' % (START_URL, POLL_TRIES, POLL_INTERVAL, PASS_JS)


def run_applescript() -> str:
    if not platform_util.has_osascript():
        return ""
    proc = subprocess.run(
        ["osascript", "-"], input=APPLESCRIPT, text=True, capture_output=True
    )
    if proc.returncode != 0:
        print(f"AppleScript error: {proc.stderr.strip()}", file=sys.stderr)
        return ""
    return proc.stdout.strip()


def harvest_cookie_macos() -> str:
    """macOS-only: incognito via AppleScript (one window per call)."""
    if not platform_util.has_osascript():
        return ""
    print("[auto_refresh] macOS: opening incognito Chrome window...", flush=True)
    cookie = run_applescript()
    return cookie.strip() if "datadome=" in cookie else ""


def harvest_cookie_browser(try_auto: bool = False) -> str:
    """Linux/Windows (and macOS fallback): SeleniumBase headed Chrome."""
    global LAST_UA
    cookie, ua = browser_harvest.harvest_cookie_interactive(try_auto_solve=try_auto)
    if "datadome=" in cookie:
        LAST_UA = ua
        return cookie
    return ""


# Set to the User-Agent that came with the most recently harvested cookie
# (only the automated SeleniumBase path can report it). Kept in sync so
# curl_cffi presents the same fingerprint the cookie was minted under.
LAST_UA = ""


def auto_solve_capable() -> bool:
    """True if PyAutoGUI can screenshot and move the mouse (slider auto-solve).

    On macOS this needs Screen Recording + Accessibility. On Linux/Windows
    needs a graphical display (DISPLAY / Wayland). Result cached in
    .auto_solve_capable (delete to re-test).
    """
    if platform_util.is_linux() and not _has_display():
        return False
    if AUTO_CAP_CACHE.exists():
        try:
            return bool(json.loads(AUTO_CAP_CACHE.read_text()))
        except Exception:
            pass
    ok = False
    try:
        import pyautogui

        pyautogui.FAILSAFE = False
        pyautogui.screenshot()  # raises / blank without Screen Recording
        start = pyautogui.position()
        pyautogui.moveTo(start[0] + 6, start[1] + 6, duration=0.05)
        time.sleep(0.05)
        end = pyautogui.position()
        pyautogui.moveTo(start[0], start[1], duration=0.05)  # put it back
        ok = abs(end[0] - (start[0] + 6)) < 4 and abs(end[1] - (start[1] + 6)) < 4
    except Exception:
        ok = False
    try:
        AUTO_CAP_CACHE.write_text(json.dumps(ok))
    except Exception:
        pass
    return ok


def _has_display() -> bool:
    import os
    if platform_util.is_windows():
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def harvest_cookie_pool() -> str:
    """Fast harvest for the background cookie pool (skip slow sb_mint).

    Opens an incognito/browser window only. Safe to run several in parallel
    (each call opens its own window).
    """
    global LAST_UA
    LAST_UA = ""
    if platform_util.has_osascript():
        cookie = harvest_cookie_macos()
        if "datadome=" in cookie:
            return cookie
    cookie, ua = browser_harvest.harvest_cookie_interactive(try_auto_solve=False, quiet=True)
    if "datadome=" in cookie:
        LAST_UA = ua
        return cookie
    return ""


def harvest_cookie() -> str:
    """Return a validated `document.cookie` string, or '' on failure."""
    global LAST_UA
    LAST_UA = ""

    can_auto = auto_solve_capable()
    if can_auto:
        try:
            import sb_mint

            cookie = sb_mint.harvest_cookie()
            if "datadome=" in cookie:
                LAST_UA = sb_mint.LAST_UA
                return cookie.strip()
            print("[auto_refresh] Auto-solve didn't pass; opening browser for you.", file=sys.stderr)
        except Exception as e:
            print(f"[auto_refresh] Auto-solve unavailable ({e}); opening browser.", file=sys.stderr)

    # macOS: fast path via AppleScript (incognito, no SeleniumBase startup).
    if platform_util.has_osascript():
        cookie = harvest_cookie_macos()
        if "datadome=" in cookie:
            return cookie
        print("[auto_refresh] AppleScript didn't pass; trying SeleniumBase browser.", file=sys.stderr)

    # Linux / Windows / macOS fallback: headed Chrome via SeleniumBase.
    if platform_util.is_linux():
        print(f"[auto_refresh] Linux detected — using SeleniumBase ({platform_util.harvest_backend()}).", flush=True)
    return harvest_cookie_browser(try_auto=can_auto)


def update_session_cookie(cookie: str, ua: str = "") -> None:
    bootstrap.ensure_session_file()
    src = SESSION_FILE.read_text()
    src = re.sub(r'COOKIE = \(\s*"[^"]*"\s*\)', f'COOKIE = (\n    "{cookie}"\n)', src, count=1, flags=re.S)
    if ua:
        src = re.sub(r'USER_AGENT = \(\s*"[^"]*"\s*\)', f'USER_AGENT = (\n    "{ua}"\n)', src, count=1, flags=re.S)
    SESSION_FILE.write_text(src)
    if COOKIE_STATE.exists():
        COOKIE_STATE.unlink()


def main() -> int:
    print(f"Harvest backend: {platform_util.harvest_backend()}")
    print("Opening a browser window... solve any captcha there.")
    print("(Waiting up to ~4 min; it auto-detects when you pass.)")
    cookie = harvest_cookie()
    if "datadome=" not in cookie:
        print("Did not capture a validated cookie (timeout or not solved).", file=sys.stderr)
        return 1
    dd = re.search(r"datadome=([^;]+)", cookie)
    update_session_cookie(cookie.strip(), LAST_UA)
    print(f"Captured fresh session. datadome={dd.group(1)[:24] if dd else '?'}...")
    if LAST_UA:
        print(f"User-Agent synced: {LAST_UA[:60]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
