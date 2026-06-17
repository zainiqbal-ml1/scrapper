"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Shared pass/captcha detection used by AppleScript and SeleniumBase paths.
Window closes only when CanLII loads with no captcha, or after captcha is solved.
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 0.4
FAST_EXIT_NO_CAPTCHA = 20   # seconds: only when no captcha ever appeared

# AppleScript + CDP poll: "cookie|||passed|||challenged"
POLL_JS = (
    "(()=>{"
    "const txt=(document.body&&document.body.innerText||'').toLowerCase();"
    "const html=(document.documentElement&&document.documentElement.innerHTML||'').toLowerCase();"
    "const c=document.cookie||'';"
    "const hasDD=c.includes('datadome=');"
    "const ddBlock=html.includes('captcha-delivery')||html.includes('geo.captcha-delivery.com')"
    "||(txt.includes('please enable js')&&html.includes('datadome')&&!txt.includes('canlii'));"
    "const canliiBlock=txt.includes('proceed with our captcha')||txt.includes('calls upon users accessing');"
    "const challenged=ddBlock||canliiBlock;"
    "const onCanlii=document.title.includes('CanLII')||txt.includes('canlii')||location.hostname.includes('canlii');"
    "const passed=hasDD&&!challenged&&onCanlii;"
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


def page_challenged_html(src: str) -> bool:
    low = (src or "").lower()
    if "captcha-delivery" in low or "geo.captcha-delivery.com" in low:
        return True
    if "proceed with our captcha" in low or "calls upon users accessing" in low:
        return True
    if "please enable js" in low and "datadome" in low and "canlii" not in low[:2000]:
        return True
    return False


def page_passed_html(src: str) -> bool:
    low = (src or "").lower()
    if page_challenged_html(src):
        return False
    return "canlii" in low


def cookie_ready(cookie: str, *, challenged: bool) -> bool:
    """True when document.cookie has a validated datadome and no active challenge."""
    return bool(cookie) and "datadome=" in cookie and not challenged


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
    prompt_shown = False
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
            challenged = page_challenged_html(src)

            try:
                cookie = sb.cdp.evaluate("document.cookie") or ""
                ua = sb.cdp.evaluate("navigator.userAgent") or ""
            except Exception:
                cookie = ua = ""

            if cookie_ready(cookie, challenged=challenged) and page_passed_html(src):
                break

            if challenged:
                captcha_seen = True
                if not prompt_shown:
                    prompt_shown = True
                    print(
                        "\n>>> Captcha detected — solve it in the Chrome window.\n"
                        "    (Solve any sliders/checkboxes shown; window closes when done.)\n",
                        flush=True,
                    )
                if try_auto_solve:
                    try:
                        sb.cdp.solve_captcha()
                    except Exception as e:
                        if not quiet:
                            print(f"[browser] auto-solve: {e}", file=sys.stderr)
            elif cookie_ready(cookie, challenged=False):
                break
            elif not captcha_seen and time.monotonic() > fast_deadline:
                if not quiet:
                    print(">>> Page did not load in time (no captcha seen).", flush=True)
                break

            time.sleep(POLL_INTERVAL)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    return cookie.strip(), ua.strip()
