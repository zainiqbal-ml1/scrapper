"""Cross-platform cookie harvest via a real Chrome window (SeleniumBase).

Shared pass/captcha detection used by AppleScript, SeleniumBase, and Linux paths.
Window closes only after all captcha steps are done.
"""
from __future__ import annotations

import sys
import time

import tor_util

START_URL = "https://www.canlii.org/en/on/"
POLL_INTERVAL = 0.25
STABLE_POLLS_NO_CAPTCHA = 2       # ~0.5s, no captcha path
SETTLE_AFTER_CAPTCHA = 2.0        # seconds to watch for a second captcha step

# Chrome / proxy failure pages (not time-based — detected from page content).
_CONNECTIVITY_MARKERS = (
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_connection_timed_out",
    "err_connection_reset",
    "err_connection_refused",
    "err_name_not_resolved",
    "err_internet_disconnected",
    "err_ssl_protocol_error",
    "this site can't be reached",
    "can't be reached",
    "proxy server is refusing",
    "network error",
    "dns_probe_finished",
    "unable to connect",
)


class HarvestConnectivityError(RuntimeError):
    """Tor exit or browser cannot load CanLII — rotate and retry."""


class HarvestIpBlockedError(RuntimeError):
    """DataDome hard IP block — stop harvest and rotate exit (no slider/captcha)."""


def page_connectivity_error(src: str, url: str = "") -> bool:
    """True when the page shows a network/proxy failure (not a captcha)."""
    low = (src or "").lower()
    u = (url or "").lower()
    if u.startswith("chrome-error://") or "chrome-error://" in low:
        return True
    return any(m in low for m in _CONNECTIVITY_MARKERS)


class HarvestStallTracker:
    """Poll-based stall detection (no wall-clock harvest timeout)."""

    BLANK_STREAK_LIMIT = 6
    CDP_FAIL_LIMIT = 3

    def __init__(self) -> None:
        self.blank_streak = 0
        self.cdp_fails = 0

    def check(
        self,
        *,
        src: str,
        url: str,
        cookie: str,
        challenged: bool,
        cdp_ok: bool,
    ) -> str | None:
        if page_ip_blocked_html(src):
            return "ip_blocked"
        if page_connectivity_error(src, url):
            return "connectivity_error"
        if not cdp_ok:
            self.cdp_fails += 1
            if self.cdp_fails >= self.CDP_FAIL_LIMIT:
                return "browser_unreachable"
        else:
            self.cdp_fails = 0

        if challenged or cookie_ready(cookie, challenged=False):
            self.blank_streak = 0
            return None

        low = (src or "").lower()
        if "canlii" not in low and len(low) < 800:
            self.blank_streak += 1
            if self.blank_streak >= self.BLANK_STREAK_LIMIT:
                return "page_never_loaded"
        else:
            self.blank_streak = 0
        return None

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
    "const ipBlock=txt.includes('temporarily blocked')||txt.includes('access is temporarily blocked')"
    "||txt.includes('you have been blocked');"
    "const challenged=ddBlock||canliiBlock;"
    "const onCanlii=document.title.includes('CanLII')||txt.includes('canlii')||location.hostname.includes('canlii');"
    "const passed=hasDD&&!challenged&&!ipBlock&&onCanlii;"
    "return c+'|||'+(passed?'1':'0')+'|||'+(challenged?'1':'0')+'|||'+(ipBlock?'1':'0');"
    "})()"
)


def parse_poll(raw: str) -> tuple[str, bool, bool, bool]:
    parts = (raw or "|||0|||0|||0").split("|||")
    cookie = parts[0].strip() if parts else ""
    passed = len(parts) > 1 and parts[1].strip() == "1"
    challenged = len(parts) > 2 and parts[2].strip() == "1"
    ip_blocked = len(parts) > 3 and parts[3].strip() == "1"
    return cookie, passed, challenged, ip_blocked


def page_challenged_html(src: str) -> bool:
    if page_ip_blocked_html(src):
        return False
    return is_datadome_slider_html(src) or is_canlii_native_captcha_html(src)


def is_datadome_slider_html(src: str) -> bool:
    """DataDome slider interstitial (auto-solvable with PyAutoGUI)."""
    if page_ip_blocked_html(src):
        return False
    low = (src or "").lower()
    if "captcha-delivery" in low or "geo.captcha-delivery.com" in low:
        return True
    if "please enable js" in low and "datadome" in low and "canlii" not in low[:2000]:
        return True
    return False


def is_canlii_native_captcha_html(src: str) -> bool:
    """CanLII reCAPTCHA / image captcha after DataDome."""
    low = (src or "").lower()
    return "proceed with our captcha" in low or "calls upon users accessing" in low


def page_ip_blocked_html(src: str) -> bool:
    """DataDome hard block — new cookies/windows will not help for ~1–2 minutes."""
    low = (src or "").lower()
    return (
        "temporarily blocked" in low
        or "access is temporarily blocked" in low
        or "you have been blocked" in low
        or ("blocked" in low and "datadome" in low and "canlii" not in low[:1500])
    )


def page_passed_html(src: str) -> bool:
    if page_ip_blocked_html(src):
        return False
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
    stall = HarvestStallTracker()
    if not quiet:
        print("\n>>> Opening Chrome...", flush=True)

    with SB(uc=True, headed=True, locale="en", **tor_util.sb_proxy_kw()) as sb:
        sb.activate_cdp_mode(START_URL)
        while True:
            cdp_ok = True
            url = ""
            try:
                src = sb.cdp.get_page_source() or ""
            except Exception:
                src = ""
                cdp_ok = False
            try:
                url = sb.cdp.evaluate("location.href") or ""
            except Exception:
                cdp_ok = False
            challenged = page_challenged_html(src)
            try:
                cookie = sb.cdp.evaluate("document.cookie") or ""
                ua = sb.cdp.evaluate("navigator.userAgent") or ""
            except Exception:
                cookie = ua = ""
                cdp_ok = False

            if page_ip_blocked_html(src):
                if not quiet:
                    print(">>> Access temporarily blocked — rotating exit.\n", flush=True)
                raise HarvestIpBlockedError("ip_blocked")

            stall_reason = stall.check(
                src=src, url=url, cookie=cookie, challenged=challenged, cdp_ok=cdp_ok,
            )
            if stall_reason:
                if stall_reason == "ip_blocked":
                    if not quiet:
                        print(">>> Access temporarily blocked — rotating exit.\n", flush=True)
                    raise HarvestIpBlockedError(stall_reason)
                if not quiet:
                    print(f">>> Harvest stalled ({stall_reason}) — trying another exit.\n", flush=True)
                raise HarvestConnectivityError(stall_reason)

            if tracker.update(
                cookie=cookie, challenged=challenged, page_ok=page_passed_html(src),
            ):
                break

            if challenged:
                slider = is_datadome_slider_html(src)
                native = is_canlii_native_captcha_html(src)
                if not prompt_shown:
                    prompt_shown = True
                    if slider and try_auto_solve:
                        print("\n>>> DataDome slider — auto-solving...\n", flush=True)
                    elif native:
                        print("\n>>> CanLII captcha — auto-solving (checkbox + OCR)...\n", flush=True)
                    else:
                        print(
                            "\n>>> Captcha — solve it in Chrome (including a second step if shown).\n",
                            flush=True,
                        )
                elif tracker.should_print_second_hint() and not quiet:
                    print(">>> Second captcha — please solve it too.\n", flush=True)
                if try_auto_solve and slider:
                    import slider_auto

                    slider_auto.try_solve_datadome_slider(sb, quiet=quiet)
                elif native:
                    import captcha_auto

                    captcha_auto.try_solve(sb, quiet=quiet)

            time.sleep(POLL_INTERVAL)

    if cookie and "datadome=" in cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    return cookie.strip(), ua.strip()


def _handle_ip_block_from_harvest(*, quiet: bool = False) -> None:
    """Delegate to auto_refresh IP cooldown (avoid import cycle at module load)."""
    import auto_refresh

    auto_refresh._handle_ip_block(quiet=quiet)
