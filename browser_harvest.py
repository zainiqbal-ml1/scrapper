"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Shared pass/captcha detection used by AppleScript, SeleniumBase, and Linux paths.
Window closes only after all captcha steps are done.
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 0.25
FAST_EXIT_NO_CAPTCHA = 15   # seconds when no captcha ever appeared
STABLE_POLLS_NO_CAPTCHA = 2       # ~0.5s, no captcha path
SETTLE_AFTER_CAPTCHA = 2.0        # seconds to watch for a second captcha step

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
    if page_challenged_html(src):
        return False
    return "canlii" in (src or "").lower()


def cookie_ready(cookie: str, *, challenged: bool) -> bool:
    return bool(cookie) and "datadome=" in cookie and not challenged


class StablePassTracker:
    """Fast pass when clear; brief settle window after captcha for a possible second step."""

    def __init__(self) -> None:
        self.captcha_seen = False
        self.streak = 0
        self._settle_start: float | None = None
        self._second_hint = False

    def update(self, *, cookie: str, challenged: bool, page_ok: bool) -> bool:
        if challenged:
            if self.captcha_seen and self._settle_start is not None:
                self._second_hint = True
            self.captcha_seen = True
            self.streak = 0
            self._settle_start = None
            return False

        ok = cookie_ready(cookie, challenged=False) and page_ok
        if not ok:
            self.streak = 0
            self._settle_start = None
            return False

        if not self.captcha_seen:
            self.streak += 1
            return self.streak >= STABLE_POLLS_NO_CAPTCHA

        now = time.monotonic()
        if self._settle_start is None:
            self._settle_start = now
            return False
        return (now - self._settle_start) >= SETTLE_AFTER_CAPTCHA

    def should_print_second_hint(self) -> bool:
        if self._second_hint:
            self._second_hint = False
            return True
        return False


def harvest_cookie_interactive(
    *,
    try_auto_solve: bool = False,
    quiet: bool = False,
) -> tuple[str, str]:
    """Open Chrome; close only when captcha flow fully complete."""
    from seleniumbase import SB

    cookie = ""
    ua = ""
    prompt_shown = False
    tracker = StablePassTracker()
    if not quiet:
        print("\n>>> Opening Chrome...", flush=True)

    fast_deadline = time.monotonic() + FAST_EXIT_NO_CAPTCHA

    with SB(uc=True, headed=True, locale="en") as sb:
        sb.activate_cdp_mode(START_URL)
        sb.sleep(1.0)
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

            if tracker.update(
                cookie=cookie, challenged=challenged, page_ok=page_passed_html(src),
            ):
                break

            if challenged:
                if not prompt_shown:
                    prompt_shown = True
                    print(
                        "\n>>> Captcha — solve it in Chrome (including a second step if shown).\n",
                        flush=True,
                    )
                elif tracker.should_print_second_hint() and not quiet:
                    print(">>> Second captcha — please solve it too.\n", flush=True)
                if try_auto_solve:
                    try:
                        sb.cdp.solve_captcha()
                    except Exception as e:
                        if not quiet:
                            print(f"[browser] auto-solve: {e}", file=sys.stderr)
            elif not tracker.captcha_seen and time.monotonic() > fast_deadline:
                if not quiet:
                    print(">>> Page did not load in time.", flush=True)
                break

            time.sleep(POLL_INTERVAL)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    return cookie.strip(), ua.strip()
