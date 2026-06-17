"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Shared pass/captcha detection used by AppleScript and SeleniumBase paths.
Window closes only when CanLII loads with no captcha, or after captcha is solved.
If a captcha is shown, the window stays open until the user passes it.
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 0.4
FAST_EXIT_NO_CAPTCHA = 15   # seconds: give up only when no captcha ever appeared

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


def _captcha_prompt_once(*, quiet: bool, shown: list[bool]) -> None:
    if quiet or shown[0]:
        return
    shown[0] = True
    print(
        "\n>>> Captcha detected — solve the slider in the Chrome window.\n"
        "    (Window stays open until you pass; closes automatically after.)\n",
        flush=True,
    )


def harvest_cookie_interactive(
    *,
    try_auto_solve: bool = False,
    quiet: bool = False,
) -> tuple[str, str]:
    """Open Chrome; close only when no captcha or captcha solved."""
    from seleniumbase import SB

    cookie = ""
    ua = ""
    captcha_seen = False
    prompt_shown = [False]
    if not quiet:
        print("\n>>> Opening Chrome...", flush=True)

    fast_deadline = time.monotonic() + FAST_EXIT_NO_CAPTCHA

    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(1.5)
        while True:
            try:
                src = sb.cdp.get_page_source() or ""
            except Exception:
                src = ""

            if page_passed_html(src):
                try:
                    cookie = sb.cdp.evaluate("document.cookie") or ""
                    ua = sb.cdp.evaluate("navigator.userAgent") or ""
                except Exception:
                    cookie = ua = ""
                if "datadome=" in cookie:
                    break

            challenged = page_challenged_html(src)
            if challenged:
                captcha_seen = True
                _captcha_prompt_once(quiet=quiet, shown=prompt_shown)
                if try_auto_solve:
                    try:
                        sb.cdp.solve_captcha()
                    except Exception as e:
                        if not quiet:
                            print(f"[browser] auto-solve: {e}", file=sys.stderr)
            elif not captcha_seen and time.monotonic() > fast_deadline:
                if not quiet:
                    print(">>> Page did not load in time (no captcha seen).", flush=True)
                break

            time.sleep(POLL_INTERVAL)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    return cookie.strip(), ua.strip()
