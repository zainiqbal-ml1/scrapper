#!/usr/bin/env python3
"""Auto-refresh the CanLII session via a real incognito Chrome window.

Flow (no DevTools / no Copy-as-cURL needed):
  1. Opens a NEW incognito Chrome window at the CanLII Ontario page.
  2. You solve the DataDome slider in that window.
  3. This polls the tab; once it passes (real page loads), it reads the
     validated cookies via `document.cookie`, writes them into session.py,
     clears the stale cookie cache, and exits 0.
  4. If you don't solve it within the timeout, exits 1.

Requirements (one-time):
  - Chrome menu: View > Developer > "Allow JavaScript from Apple Events" (checked)
  - Approve the macOS automation prompt for controlling Chrome the first time.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

SESSION_FILE = Path("session.py")
COOKIE_STATE = Path(".cookie_state.json")
AUTO_CAP_CACHE = Path(".auto_solve_capable")
START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 3      # seconds between checks
POLL_TRIES = 80        # 80 * 3s = up to 4 minutes to solve the slider

# We consider the page "passed" only when the REAL Ontario page is showing
# (it lists "Court of Appeal for Ontario"). This means ALL captchas - the
# DataDome slider AND CanLII's native captcha - have been solved.
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
    proc = subprocess.run(
        ["osascript", "-"], input=APPLESCRIPT, text=True, capture_output=True
    )
    if proc.returncode != 0:
        print(f"AppleScript error: {proc.stderr.strip()}", file=sys.stderr)
        return ""
    return proc.stdout.strip()


# Set to the User-Agent that came with the most recently harvested cookie
# (only the automated SeleniumBase path can report it). Kept in sync so
# curl_cffi presents the same fingerprint the cookie was minted under.
LAST_UA = ""


def auto_solve_capable() -> bool:
    """True only if PyAutoGUI can actually screenshot AND move the mouse.

    The DataDome slider auto-solver needs macOS Screen Recording (to find the
    slider) and Accessibility (to drag it). If either is missing, the drag
    silently no-ops, so we skip the slow auto attempt and go straight to the
    manual window. Result is cached in .auto_solve_capable (delete it to
    re-test after granting permissions).
    """
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


def harvest_cookie() -> str:
    """Return a validated `document.cookie` string, or '' on failure.

    If this machine can drive the mouse (Screen Recording + Accessibility
    granted), tries the AUTOMATED slider solver first. Otherwise - or if it
    fails - opens an incognito window for you to solve the slider yourself.
    """
    global LAST_UA
    LAST_UA = ""
    if auto_solve_capable():
        try:
            import sb_mint

            cookie = sb_mint.harvest_cookie()
            if "datadome=" in cookie:
                LAST_UA = sb_mint.LAST_UA
                return cookie.strip()
            print("[auto_refresh] Auto-solve didn't pass; opening a window for you to solve.", file=sys.stderr)
        except Exception as e:
            print(f"[auto_refresh] Auto-solve unavailable ({e}); opening a window.", file=sys.stderr)
    else:
        print("[auto_refresh] Auto-solve needs Screen Recording + Accessibility "
              "(mouse control is blocked here) - opening a window for you to solve.", file=sys.stderr)

    cookie = run_applescript()
    return cookie.strip() if "datadome=" in cookie else ""


def update_session_cookie(cookie: str, ua: str = "") -> None:
    src = SESSION_FILE.read_text()
    src = re.sub(r'COOKIE = \(\s*"[^"]*"\s*\)', f'COOKIE = (\n    "{cookie}"\n)', src, count=1, flags=re.S)
    if ua:
        src = re.sub(r'USER_AGENT = \(\s*"[^"]*"\s*\)', f'USER_AGENT = (\n    "{ua}"\n)', src, count=1, flags=re.S)
    SESSION_FILE.write_text(src)
    if COOKIE_STATE.exists():
        COOKIE_STATE.unlink()


def main() -> int:
    print("Opening an incognito Chrome window... solve the slider there.")
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
