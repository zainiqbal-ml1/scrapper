"""Fast Linux cookie harvest via system Chrome + Selenium attach (no SeleniumBase UC)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time

from browser_harvest import (
    POLL_INTERVAL,
    POLL_JS,
    START_URL,
    HarvestConnectivityError,
    HarvestStallTracker,
    StablePassTracker,
    cookie_ready,
    page_challenged_html,
    page_ip_blocked_html,
    page_passed_html,
    parse_poll,
    _handle_ip_block_from_harvest,
)


def find_chrome() -> str | None:
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def _read_state(driver) -> tuple[str, str, bool, bool]:
    try:
        src = driver.page_source or ""
    except Exception:
        src = ""
    challenged = page_challenged_html(src)
    page_ok = page_passed_html(src)
    try:
        raw = driver.execute_script(f"return {POLL_JS}") or ""
        cookie, poll_passed, poll_challenged, ip_blocked = parse_poll(str(raw))
        if ip_blocked:
            challenged = True
        if poll_challenged:
            challenged = True
        if poll_passed:
            page_ok = True
    except Exception:
        cookie = ""
        try:
            cookie = driver.execute_script("return document.cookie") or ""
        except Exception:
            cookie = ""
    try:
        ua = driver.execute_script("return navigator.userAgent") or ""
    except Exception:
        ua = ""
    return cookie, ua, challenged, page_ok


def harvest_linux_fast(*, quiet: bool = True) -> tuple[str, str]:
    """Launch system Chrome, wait through captcha steps, capture cookie when stable."""
    chrome = find_chrome()
    if not chrome:
        return "", ""

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    profile = tempfile.mkdtemp(prefix="canlii-harvest-")
    port = 19222 + (int(time.time()) % 1000)
    if not quiet:
        print("\n>>> Linux: opening Chrome...\n", flush=True)

    proc = subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            "--incognito",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            START_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    driver = None
    cookie = ""
    ua = ""
    passed = False
    prompt_shown = False
    tracker = StablePassTracker()
    stall = HarvestStallTracker()

    try:
        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        for _ in range(40):
            try:
                driver = webdriver.Chrome(options=opts)
                break
            except Exception:
                time.sleep(0.2)
        if not driver:
            return "", ""

        while True:
            cookie, ua, challenged, page_ok = _read_state(driver)
            try:
                src = driver.page_source or ""
            except Exception:
                src = ""
            if page_ip_blocked_html(src):
                _handle_ip_block_from_harvest(quiet=quiet)
                break

            try:
                url = driver.current_url or ""
            except Exception:
                url = ""
            stall_reason = stall.check(
                src=src, url=url, cookie=cookie, challenged=challenged, cdp_ok=True,
            )
            if stall_reason:
                if not quiet:
                    print(f">>> Harvest stalled ({stall_reason}).\n", flush=True)
                raise HarvestConnectivityError(stall_reason)

            page_ok = page_ok or cookie_ready(cookie, challenged=False)

            if tracker.update(cookie=cookie, challenged=challenged, page_ok=page_ok):
                passed = True
                break

            if challenged:
                if not prompt_shown:
                    prompt_shown = True
                    print(
                        ">>> Captcha — solve it in Chrome (including a second step if shown).\n",
                        flush=True,
                    )
                elif tracker.should_print_second_hint():
                    print(">>> Second captcha — please solve it too.\n", flush=True)

            time.sleep(POLL_INTERVAL)
    finally:
        if passed or not tracker.captcha_seen:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
            shutil.rmtree(profile, ignore_errors=True)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Linux cookie captured.\n", flush=True)
    return cookie.strip(), ua.strip()
