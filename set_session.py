#!/usr/bin/env python3
"""Refresh session.py from a fresh Chrome 'Copy as cURL'.

Whenever the scraper stops with a DataDome challenge:
  1. In Chrome (where CanLII works), open any CanLII page, DevTools > Network,
     reload, right-click the top request > Copy > Copy as cURL.
  2. Paste it into a file called `curl.txt` in this folder (overwrite it).
  3. Run:  python set_session.py
  4. Re-run the scraper with the same args (downloaded files are skipped).

This rewrites the COOKIE and USER_AGENT lines in session.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import bootstrap

CURL_FILE = Path("curl.txt")
SESSION_FILE = Path("session.py")


def extract(curl: str) -> tuple[str, str]:
    # cookies: -b '...' or --cookie '...', or a -H 'cookie: ...' header
    cookie = None
    m = re.search(r"(?:-b|--cookie)\s+'([^']*)'", curl)
    if m:
        cookie = m.group(1)
    if not cookie:
        m = re.search(r"-H\s+'cookie:\s*([^']*)'", curl, re.I)
        if m:
            cookie = m.group(1)

    ua = None
    m = re.search(r"-H\s+'user-agent:\s*([^']*)'", curl, re.I)
    if m:
        ua = m.group(1)

    if not cookie:
        sys.exit("ERROR: couldn't find cookies in curl.txt (expected -b '...' or a cookie header)")
    if not ua:
        sys.exit("ERROR: couldn't find user-agent header in curl.txt")
    return cookie.strip(), ua.strip()


def main() -> int:
    bootstrap.ensure_session_file()
    if not CURL_FILE.exists():
        sys.exit(f"ERROR: paste your 'Copy as cURL' into {CURL_FILE.resolve()} first.")
    curl = CURL_FILE.read_text()
    cookie, ua = extract(curl)

    src = SESSION_FILE.read_text()
    src = re.sub(r'COOKIE = \(\s*"[^"]*"\s*\)', f'COOKIE = (\n    "{cookie}"\n)', src, count=1, flags=re.S)
    src = re.sub(r'USER_AGENT = \(\s*"[^"]*"\s*\)', f'USER_AGENT = (\n    "{ua}"\n)', src, count=1, flags=re.S)
    SESSION_FILE.write_text(src)

    # drop any stale rotated-cookie cache so the fresh session takes effect
    state = Path(".cookie_state.json")
    if state.exists():
        state.unlink()

    dd = re.search(r"datadome=([^;]+)", cookie)
    print("Updated session.py")
    print(f"  user-agent: {ua[:60]}...")
    print(f"  datadome:   {dd.group(1)[:30] if dd else '(not found!)'}...")
    print("Now re-run your scraper command.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
