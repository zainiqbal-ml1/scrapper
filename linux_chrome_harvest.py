"""Fast Linux cookie harvest via system Chrome + Selenium attach (no SeleniumBase UC)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time

from browser_harvest import PASS_TEXT, START_URL

POLL_INTERVAL = 0.5
POLL_TRIES = 120  # ~60s


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


def _page_passed(driver) -> bool:
    try:
        return PASS_TEXT in (driver.page_source or "")
    except Exception:
        return False


def harvest_linux_fast(*, quiet: bool = True) -> tuple[str, str]:
    """Launch system Chrome incognito, attach Selenium, read cookie when page passes."""
    chrome = find_chrome()
    if not chrome:
        return "", ""

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    profile = tempfile.mkdtemp(prefix="canlii-harvest-")
    port = 19222 + (int(time.time()) % 1000)
    if not quiet:
        print("\n>>> Linux: opening system Chrome (fast harvest)...\n", flush=True)

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
    try:
        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        for _ in range(40):
            try:
                driver = webdriver.Chrome(options=opts)
                break
            except Exception:
                time.sleep(0.25)
        if not driver:
            return "", ""

        for _ in range(POLL_TRIES):
            if _page_passed(driver):
                try:
                    cookie = driver.execute_script("return document.cookie") or ""
                    ua = driver.execute_script("return navigator.userAgent") or ""
                except Exception:
                    cookie = ua = ""
                if "datadome=" in cookie:
                    break
            time.sleep(POLL_INTERVAL)
    finally:
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
