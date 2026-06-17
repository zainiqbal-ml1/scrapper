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
CHALLENGE_JS = (
    "(()=>{"
    "const txt=(document.body&&document.body.innerText||'').toLowerCase();"
    "const html=(document.documentElement&&document.documentElement.innerHTML||'').toLowerCase();"
    "return (txt.includes('captcha')||txt.includes('proceed with our captcha')||html.includes('captcha-delivery'))?'1':'0';"
    "})()"
)
# Shorter poll when driven from Python (see harvest_cookie_macos).
MAC_POLL_INTERVAL = 0.5
MAC_POLL_TRIES = 120  # ~60s

AS_OPEN = f'''
tell application "Google Chrome"
  set w to make new window with properties {{mode:"incognito"}}
  set URL of active tab of w to "{START_URL}"
end tell
'''

AS_POLL = f'''
tell application "Google Chrome"
  try
    set t to active tab of front window
    set c to execute t javascript "document.cookie"
    set challenged to execute t javascript "{CHALLENGE_JS}"
    return c & "|||" & challenged
  on error
    return "|||1"
  end try
end tell
'''

AS_ACTIVATE = '''
tell application "Google Chrome"
  activate
  set index of front window to 1
end tell
'''

AS_CLOSE = '''
tell application "Google Chrome"
  try
    close front window
  end try
end tell
'''


def _run_as(script: str) -> str:
    if not platform_util.has_osascript():
        return ""
    proc = subprocess.run(
        ["osascript", "-e", script], text=True, capture_output=True
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


# Legacy single-shot script (kept for reference; harvest_cookie_macos uses AS_* above).
APPLESCRIPT = '''
tell application "Google Chrome"
  set w to make new window with properties {mode:"incognito"}
  set URL of active tab of w to "%s"
end tell
set grabbed to ""
set focused to false
repeat %d times
  delay %d
  tell application "Google Chrome"
    try
      set passed to execute active tab of w javascript "%s"
    on error
      set passed to "0"
    end try
    try
      set challenged to execute active tab of w javascript "%s"
    on error
      set challenged to "0"
    end try
    if challenged is "1" and focused is false then
      activate
      set index of w to 1
      set focused to true
    end if
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
''' % (START_URL, POLL_TRIES, POLL_INTERVAL, PASS_JS, CHALLENGE_JS)


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


def harvest_cookie_macos(*, quiet: bool = False) -> str:
    """macOS: incognito via AppleScript, polled from Python for faster detection."""
    if not platform_util.has_osascript():
        return ""
    if not quiet:
        print(
            "\n>>> Opening Chrome incognito — solve the captcha if shown.\n"
            "    (If this never finishes: Chrome menu View > Developer >\n"
            "     Allow JavaScript from Apple Events)\n",
            flush=True,
        )
    _run_as(AS_OPEN)
    js_hint_shown = False
    cookie = ""
    for i in range(MAC_POLL_TRIES):
        raw = _run_as(AS_POLL)
        parts = (raw or "|||1").split("|||", 1)
        cookie = parts[0].strip() if parts else ""
        challenged = parts[1].strip() if len(parts) > 1 else "1"
        if "datadome=" in cookie and challenged != "1":
            break
        if challenged == "1":
            _run_as(AS_ACTIVATE)
        elif i > 20 and not cookie and not js_hint_shown and not quiet:
            print(
                "    Tip: enable Chrome > View > Developer > "
                "Allow JavaScript from Apple Events",
                flush=True,
            )
            js_hint_shown = True
        if not quiet and i > 0 and i % 20 == 0:
            print(f"    still waiting... ({i * MAC_POLL_INTERVAL:.0f}s)", flush=True)
        time.sleep(MAC_POLL_INTERVAL)
    _run_as(AS_CLOSE)
    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured.\n", flush=True)
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
    """Fast harvest for the background cookie pool (skip slow sb_mint)."""
    global LAST_UA
    LAST_UA = ""
    if platform_util.has_osascript():
        cookie = harvest_cookie_macos(quiet=True)
        if "datadome=" in cookie:
            return cookie
    if platform_util.is_linux():
        try:
            from linux_chrome_harvest import harvest_linux_fast

            cookie, ua = harvest_linux_fast(quiet=True)
            if "datadome=" in cookie:
                LAST_UA = ua
                return cookie
        except Exception as e:
            print(f"[auto_refresh] Linux fast harvest failed ({e}); using SeleniumBase.", file=sys.stderr)
    cookie, ua = browser_harvest.harvest_cookie_interactive(
        try_auto_solve=False, quiet=True, fast_poll=True,
    )
    if "datadome=" in cookie:
        LAST_UA = ua
        return cookie
    return ""


def harvest_cookie(*, skip_auto_solve: bool = False) -> str:
    """Return a validated `document.cookie` string, or '' on failure.

    skip_auto_solve: go straight to incognito/browser (used by run.py so we
    don't block ~30s on sb_mint before opening the window you need to solve).
    """
    global LAST_UA
    LAST_UA = ""

    can_auto = False if skip_auto_solve else auto_solve_capable()
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
