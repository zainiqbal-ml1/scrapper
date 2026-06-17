"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Shared pass/captcha detection used by AppleScript and SeleniumBase paths.
Window closes automatically once the cookie is captured (or on timeout).
Only prompts for manual captcha solve when a captcha is actually shown.
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 0.4
FAST_EXIT_NO_CAPTCHA = 12   # seconds: if no captcha ever shown, give up waiting
CAPTCHA_TIMEOUT = 180       # seconds: max wait once captcha is shown
DEFAULT_TIMEOUT = 180

# One JS snippet for AppleScript + CDP: "cookie|||passed|||challenged"
POLL_JS = (
    "(()=>{"
    "const txt=(document.body&&document.body.innerText||'').toLowerCase();"
    "const html=(document.documentElement&&document.documentElement.innerHTML||'').toLowerCase();"
    "const challenged=txt.includes('captcha')||txt.includes('proceed with our captcha')"
    "||html.includes('captcha-delivery');"
    "const c=document.cookie||'';"
    "const passed=!challenged&&c.includes('datadome=')"
    "&&(document.title.includes('CanLII')||txt.includes('canlii')||txt.includes('court of appeal'));"
    "return c+'|||'+(passed?'1':'0')+'|||'+(challenged?'1':'0');"
    "})()"
)


def parse_poll(raw: str) -> tuple[str, bool, bool]:
    """Parse POLL_JS output -> (cookie, passed, challenged)."""
    parts = (raw or "|||0|||0").split("|||")
    cookie = parts[0].strip() if parts else ""
    passed = len(parts) > 1 and parts[1].strip() == "1"
    challenged = len(parts) > 2 and parts[2].strip() == "1"
    return cookie, passed, challenged


def page_passed_html(src: str) -> bool:
    """True when HTML looks like a validated CanLII page (no captcha)."""
    low = (src or "").lower()
    if "captcha-delivery" in low or "proceed with our captcha" in low:
        return False
    if "please enable js" in low and "datadome" in low:
        return False
    return "canlii" in low and ("court of appeal" in low or "canlii.org" in low)


def page_challenged_html(src: str) -> bool:
    low = (src or "").lower()
    return (
        "captcha-delivery" in low
        or "proceed with our captcha" in low
        or ("captcha" in low and "datadome" in low)
    )


def harvest_cookie_interactive(
    *,
    try_auto_solve: bool = False,
    quiet: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> tuple[str, str]:
    """Open Chrome, capture cookie when CanLII loads, close automatically."""
    from seleniumbase import SB

    cookie = ""
    ua = ""
    captcha_seen = False
    if not quiet:
        print("\n>>> Opening Chrome...", flush=True)

    deadline = time.monotonic() + timeout_s
    fast_deadline = time.monotonic() + FAST_EXIT_NO_CAPTCHA

    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(1.5)
        while time.monotonic() < deadline:
            try:
                src = sb.cdp.get_page_source() or ""
            except Exception:
                src = ""
            challenged = page_challenged_html(src)
            if page_passed_html(src):
                try:
                    cookie = sb.cdp.evaluate("document.cookie") or ""
                    ua = sb.cdp.evaluate("navigator.userAgent") or ""
                except Exception:
                    cookie = ua = ""
                if "datadome=" in cookie:
                    break
            if challenged:
                captcha_seen = True
                if not quiet:
                    print(
                        "\n>>> Captcha detected — solve the slider in the Chrome window.\n"
                        "    (Closes automatically once you pass.)\n",
                        flush=True,
                    )
                if try_auto_solve:
                    try:
                        sb.cdp.solve_captcha()
                    except Exception as e:
                        if not quiet:
                            print(f"[browser] auto-solve: {e}", file=sys.stderr)
            elif not captcha_seen and time.monotonic() > fast_deadline:
                if not quiet:
                    print(">>> Page did not load in time.", flush=True)
                break
            time.sleep(POLL_INTERVAL)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    return cookie.strip(), ua.strip()
