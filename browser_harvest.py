"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Shared pass/captcha detection used by AppleScript and SeleniumBase paths.
Window closes only after captcha flow is fully done (including a second step).
"""
from __future__ import annotations

import sys
import time

START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 0.4
FAST_EXIT_NO_CAPTCHA = 20   # seconds: only when no captcha ever appeared
STABLE_POLLS_NO_CAPTCHA = 4       # ~1.6s with no captcha ever shown
STABLE_POLLS_AFTER_CAPTCHA = 15   # ~6s after any captcha — catches a second step

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
    """True when document.cookie has datadome and no active challenge."""
    return bool(cookie) and "datadome=" in cookie and not challenged


class StablePassTracker:
    """Require several consecutive OK polls before accepting pass.

    CanLII often shows a second captcha a moment after the DataDome slider;
    a single OK poll was causing an early quit before step two appeared.
    """

    def __init__(self) -> None:
        self.captcha_seen = False
        self.streak = 0
        self._wait_hint = False
        self._second_hint = False

    def update(self, *, cookie: str, challenged: bool, page_ok: bool) -> bool:
        if challenged:
            if self.captcha_seen and self.streak > 0 and not self._second_hint:
                self._second_hint = True
            self.captcha_seen = True
            self.streak = 0
            self._wait_hint = False
            return False

        ok = cookie_ready(cookie, challenged=False) and page_ok
        if not ok:
            self.streak = 0
            return False

        if self.captcha_seen and self.streak == 0 and not self._wait_hint:
            self._wait_hint = True

        need = STABLE_POLLS_AFTER_CAPTCHA if self.captcha_seen else STABLE_POLLS_NO_CAPTCHA
        self.streak += 1
        return self.streak >= need

    def should_print_wait_hint(self) -> bool:
        if self._wait_hint and self.streak == 1 and self.captcha_seen:
            self._wait_hint = False
            return True
        return False

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

            page_ok = page_passed_html(src)
            if tracker.update(cookie=cookie, challenged=challenged, page_ok=page_ok):
                break

            if challenged:
                if not prompt_shown:
                    prompt_shown = True
                    print(
                        "\n>>> Captcha detected — solve it in the Chrome window.\n"
                        "    (If a second captcha appears, solve that too.)\n",
                        flush=True,
                    )
                elif tracker.should_print_second_hint() and not quiet:
                    print(
                        ">>> Another captcha step appeared — please solve it too.\n",
                        flush=True,
                    )
                if try_auto_solve:
                    try:
                        sb.cdp.solve_captcha()
                    except Exception as e:
                        if not quiet:
                            print(f"[browser] auto-solve: {e}", file=sys.stderr)
            elif tracker.should_print_wait_hint() and not quiet:
                print(
                    ">>> First captcha cleared — waiting a few seconds in case another appears...\n",
                    flush=True,
                )
            elif not tracker.captcha_seen and time.monotonic() > fast_deadline:
                if not quiet:
                    print(">>> Page did not load in time (no captcha seen).", flush=True)
                break

            time.sleep(POLL_INTERVAL)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    return cookie.strip(), ua.strip()
