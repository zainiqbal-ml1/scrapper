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
import threading
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
MAC_KEEP_TIMEOUT = 600  # 10 min — keep the window open while you solve the captcha

# Set True when AppleScript opened a window but Chrome refused to run JS via
# Apple Events (so the caller knows to fall back to SeleniumBase instead).
LAST_MAC_NOJS = False

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

def _as_poll_window(win_idx: int) -> str:
    return f'''
tell application "Google Chrome"
  try
    set t to active tab of window {win_idx}
    set c to execute t javascript "document.cookie"
    set passed to execute t javascript "{PASS_JS}"
    return c & "|||" & passed
  on error
    return "|||0"
  end try
end tell
'''

AS_ACTIVATE_WINDOW = '''
tell application "Google Chrome"
  try
    activate
    set index of window %s to 1
  end try
end tell
'''

AS_CLOSE_WINDOW = '''
tell application "Google Chrome"
  try
    close window %s
  end try
end tell
'''

AS_SCAN_INCOGNITO = f'''
tell application "Google Chrome"
  repeat with w in windows
    try
      if mode of w is "incognito" then
        set c to execute active tab of w javascript "document.cookie"
        set passed to execute active tab of w javascript "{PASS_JS}"
        if c contains "datadome=" and passed is "1" then return c
      end if
    end try
  end repeat
end tell
return ""
'''

_harvest_lock = threading.Lock()


def _run_as(script: str, *, quiet: bool = False) -> str:
    if not platform_util.has_osascript():
        return ""
    proc = subprocess.run(
        ["osascript", "-e", script], text=True, capture_output=True
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err and not quiet:
            print(f"[osascript] {err}", file=sys.stderr, flush=True)
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


def poll_incognito_windows(*, quiet: bool = False) -> str:
    """Read datadome cookie from any open incognito window (no new window)."""
    if not platform_util.has_osascript():
        return ""
    cookie = _run_as(AS_SCAN_INCOGNITO).strip()
    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie read from open Chrome window.\n", flush=True)
    return cookie if "datadome=" in cookie else ""


def harvest_cookie_macos(
    *,
    quiet: bool = False,
    keep_open: bool = False,
    quick: bool = False,
    timeout_s: float | None = None,
) -> str:
    """macOS: open ONE incognito window and keep it open until you solve it.

    Sets module flag LAST_MAC_NOJS=True (and returns "") if Chrome refuses to
    run JavaScript via Apple Events, so the caller can fall back to SeleniumBase.

    quick: short ~8s probe (used for a quick background check, not for solving).
    timeout_s: how long to keep the window open while you solve (default 10 min
    unless quick).
    """
    global LAST_MAC_NOJS
    LAST_MAC_NOJS = False
    if not platform_util.has_osascript():
        return ""
    if not platform_util.chrome_macos_installed():
        if not quiet:
            print(
                "Google Chrome not found in /Applications.\n"
                "  Install Chrome or the scraper will use SeleniumBase instead.\n",
                flush=True,
            )
        LAST_MAC_NOJS = True
        return ""
    if timeout_s is None:
        timeout_s = 8.0 if quick else MAC_KEEP_TIMEOUT
    with _harvest_lock:
        if not quiet:
            print(
                "\n>>> A Chrome incognito window is opening — SOLVE THE CAPTCHA there.\n"
                f"    It stays open up to {int(timeout_s // 60)} min and closes once you pass.\n",
                flush=True,
            )
        raw_idx = _run_as(AS_OPEN, quiet=quiet).strip()
        if not raw_idx.isdigit():
            if not quiet:
                print(
                    "Could not open Chrome via AppleScript "
                    "(check Automation permission in System Settings > Privacy).\n",
                    flush=True,
                )
            LAST_MAC_NOJS = True
            return ""
        win_idx = int(raw_idx)

        # Probe: can we run JS in this window via Apple Events?
        time.sleep(1.0)
        probe = _run_as(AS_PROBE % win_idx, quiet=True).strip()
        if probe != "PONG":
            if not quiet:
                print(
                    "    Chrome won't allow JavaScript from Apple Events on this Mac.\n"
                    "    Switching to a SeleniumBase window instead...\n",
                    flush=True,
                )
            _run_as(AS_CLOSE_WINDOW % win_idx, quiet=True)
            LAST_MAC_NOJS = True
            return ""

        cookie = ""
        passed = "0"
        deadline = time.monotonic() + timeout_s
        last_note = time.monotonic()
        activated = False
        while time.monotonic() < deadline:
            raw = _run_as(_as_poll_window(win_idx), quiet=True)
            parts = (raw or "|||0").split("|||", 1)
            cookie = parts[0].strip() if parts else ""
            passed = parts[1].strip() if len(parts) > 1 else "0"
            # Only accept once the REAL CanLII page has loaded (passed == "1").
            # DataDome sets a `datadome` cookie on the unsolved challenge page
            # too, so a cookie alone is not proof the captcha was solved.
            if passed == "1" and "datadome=" in cookie:
                break
            if passed != "1" and not activated:
                # Bring the window to the front ONCE so it doesn't steal focus
                # every poll while you're solving the captcha.
                _run_as(AS_ACTIVATE_WINDOW % win_idx, quiet=True)
                activated = True
            if not quiet and time.monotonic() - last_note >= 30:
                remaining = int(deadline - time.monotonic())
                print(f"    still waiting for you to solve the captcha... "
                      f"({remaining}s left)", flush=True)
                last_note = time.monotonic()
            time.sleep(MAC_POLL_INTERVAL)
        if (passed == "1" and "datadome=" in cookie) or not keep_open:
            _run_as(AS_CLOSE_WINDOW % win_idx, quiet=True)
    if cookie and "datadome=" in cookie and passed == "1" and not quiet:
        print(">>> Cookie captured.\n", flush=True)
    return cookie.strip() if (passed == "1" and "datadome=" in cookie) else ""


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
    """Harvest a replacement cookie when the active one is burned (visible window)."""
    global LAST_UA
    LAST_UA = ""
    if platform_util.has_osascript():
        cookie = harvest_cookie_macos(keep_open=True, timeout_s=180)
        if "datadome=" in cookie:
            return cookie
        if not LAST_MAC_NOJS:
            return ""  # window worked but wasn't solved; don't open a second one
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
        try_auto_solve=False, timeout_s=180,
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
        if not LAST_MAC_NOJS:
            return ""  # window opened fine; user just didn't solve it in time
        print("[auto_refresh] AppleScript can't run JS; trying SeleniumBase browser.", file=sys.stderr)

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
