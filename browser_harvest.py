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
STABLE_POLLS_CLEAR = 4          # ~1s with no captcha on the path
STABLE_POLLS_AFTER_CAPTCHA = 12  # ~3s stable pass after slider / captcha steps
POST_SLIDER_GUARD_POLLS = 6      # ~1.5s watch for follow-up captcha after slider

# Live DOM check — catches 2nd captcha before it appears in page_source snapshot.
FOLLOWUP_JS = (
    "(()=>{"
    "const t=(document.body&&document.body.innerText||'').toLowerCase();"
    "const h=(document.documentElement&&document.documentElement.innerHTML||'').toLowerCase();"
    "const u=location.href.toLowerCase();"
    "if(u.includes('captcha-delivery')||u.includes('geo.captcha'))return 'delivery';"
    "if(t.includes('proceed with our captcha')||t.includes('calls upon users accessing'))return 'canlii';"
    "if(document.querySelector('iframe[src*=\"recaptcha\"],iframe[src*=\"captcha-delivery\"],iframe[title*=\"reCAPTCHA\"]'))return 'iframe';"
    "if(h.includes('g-recaptcha')||h.includes('google.com/recaptcha'))return 'recaptcha';"
    "return '';"
    "})()"
)

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
        hold: bool = False,
    ) -> str | None:
        if page_connectivity_error(src, url):
            return "connectivity_error"
        if hold:
            self.blank_streak = 0
            self.cdp_fails = 0
            return None
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
    return (
        is_datadome_slider_html(src)
        or is_canlii_native_captcha_html(src)
        or is_recaptcha_html(src)
    )


def is_recaptcha_html(src: str) -> bool:
    low = (src or "").lower()
    return "recaptcha" in low and (
        "g-recaptcha" in low or "google.com/recaptcha" in low or "iframe" in low
    )


def harvest_complete(cookie: str, src: str) -> bool:
    """True only when CanLII is fully loaded and the session cookie is usable."""
    if page_ip_blocked_html(src):
        return False
    if page_challenged_html(src):
        return False
    if not cookie_ready(cookie, challenged=False):
        return False
    low = (src or "").lower()
    if "captcha-delivery" in low or "geo.captcha-delivery.com" in low:
        return False
    if "please enable js" in low and "datadome" in low:
        return False
    return "canlii" in low and (
        "canlii.org" in low or 'href="/' in low or "database" in low or "jurisdiction" in low
    )


def harvest_complete_verified(
    cookie: str, src: str, url: str = "", *, followup: str = "",
) -> bool:
    """Stricter pass check — rejects partial pages and pending follow-up captchas."""
    if followup:
        return False
    if not harvest_complete(cookie, src):
        return False
    u = (url or "").lower()
    if "captcha-delivery" in u or "geo.captcha" in u:
        return False
    low = (src or "").lower()
    if "proceed with our captcha" in low or "calls upon users accessing" in low:
        return False
    if "g-recaptcha" in low or "google.com/recaptcha" in low:
        return False
    return True


def finalize_harvest(cookie: str, src: str, url: str = "") -> str:
    """Return cookie only when the session is fully verified."""
    return cookie.strip() if harvest_complete_verified(cookie, src, url) else ""


def is_datadome_slider_html(src: str) -> bool:
    """DataDome slider interstitial (auto-solvable with PyAutoGUI)."""
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
    """Require consecutive verified polls before closing (longer after captcha)."""

    def __init__(self) -> None:
        self.captcha_seen = False
        self.streak = 0
        self._second_hint = False
        self._on_slider = False
        self._post_slider_guard = 0

    def holding_stall(self) -> bool:
        """True while waiting for a possible follow-up captcha after the slider."""
        return self._post_slider_guard > 0

    def update(
        self,
        *,
        cookie: str,
        challenged: bool,
        src: str,
        url: str = "",
        followup: str = "",
    ) -> bool:
        on_slider = is_datadome_slider_html(src)
        followup_active = bool(followup) or is_canlii_native_captcha_html(src) or is_recaptcha_html(src)

        if on_slider:
            self._on_slider = True
        elif self._on_slider and not followup_active and not challenged:
            if self._post_slider_guard == 0:
                self._post_slider_guard = POST_SLIDER_GUARD_POLLS
            self._on_slider = False

        if challenged or followup_active:
            if self.captcha_seen and not on_slider:
                self._second_hint = True
            self.captcha_seen = True
            self._post_slider_guard = 0
            self.streak = 0
            if on_slider:
                self._on_slider = True
            return False

        if self._post_slider_guard > 0:
            self._post_slider_guard -= 1
            self.streak = 0
            return False

        if not harvest_complete_verified(cookie, src, url):
            self.streak = 0
            return False

        self.streak += 1
        need = STABLE_POLLS_AFTER_CAPTCHA if self.captcha_seen else STABLE_POLLS_CLEAR
        return self.streak >= need

    def should_print_second_hint(self) -> bool:
        if self._second_hint:
            self._second_hint = False
            return True
        return False

    def awaiting_followup(self, *, cookie: str, src: str) -> bool:
        """After slider/captcha cleared but page not fully ready yet."""
        return self.captcha_seen and not harvest_complete(cookie, src)


def _auto_solve_step(sb, *, src: str, try_auto_solve: bool, quiet: bool) -> None:
    """Run the right auto-solver for the current challenge (or follow-up captcha)."""
    if not try_auto_solve:
        return
    if is_datadome_slider_html(src):
        try:
            sb.cdp.solve_captcha()
        except Exception as e:
            if not quiet:
                print(f"[harvest] slider: {e}", file=sys.stderr)
    elif is_canlii_native_captcha_html(src) or is_recaptcha_html(src):
        import captcha_auto

        captcha_auto.try_solve(sb, quiet=quiet)


def run_harvest_loop(sb, *, try_auto_solve: bool = False, quiet: bool = False) -> tuple[str, str]:
    """Poll Chrome until harvest is complete, or raise on connectivity failure."""
    cookie = ""
    ua = ""
    prompt_shown = False
    tracker = StablePassTracker()
    stall = HarvestStallTracker()

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
        try:
            cookie = sb.cdp.evaluate("document.cookie") or ""
            ua = sb.cdp.evaluate("navigator.userAgent") or ""
        except Exception:
            cookie = ua = ""
            cdp_ok = False

        followup = ""
        try:
            followup = sb.cdp.evaluate(FOLLOWUP_JS) or ""
        except Exception:
            pass

        challenged = page_challenged_html(src) or bool(followup)

        if page_ip_blocked_html(src):
            _handle_ip_block_from_harvest(quiet=quiet)
            break

        stall_reason = stall.check(
            src=src, url=url, cookie=cookie, challenged=challenged, cdp_ok=cdp_ok,
            hold=challenged or tracker.holding_stall(),
        )
        if stall_reason:
            if not quiet:
                print(f">>> Harvest stalled ({stall_reason}) — trying another exit.\n", flush=True)
            raise HarvestConnectivityError(stall_reason)

        if tracker.update(
            cookie=cookie, challenged=challenged, src=src, url=url, followup=followup,
        ):
            if harvest_complete_verified(cookie, src, url):
                break
            tracker.streak = 0

        if challenged or tracker.awaiting_followup(cookie=cookie, src=src) or tracker.holding_stall():
            if not prompt_shown:
                prompt_shown = True
                if is_datadome_slider_html(src):
                    if not quiet:
                        print("\n>>> DataDome slider — auto-solving...\n", flush=True)
                elif is_canlii_native_captcha_html(src) or is_recaptcha_html(src):
                    if not quiet:
                        print("\n>>> CanLII captcha — auto-solving...\n", flush=True)
                elif not quiet:
                    print("\n>>> Finishing captcha — waiting for CanLII to load...\n", flush=True)
            elif tracker.should_print_second_hint() and not quiet:
                print(">>> Second captcha step — solving...\n", flush=True)
            _auto_solve_step(sb, src=src, try_auto_solve=try_auto_solve, quiet=quiet)

        time.sleep(POLL_INTERVAL)

    cookie = finalize_harvest(cookie, src, url)
    if cookie and not quiet:
        print(">>> Cookie captured — window closed.\n", flush=True)
    elif not cookie and not quiet:
        print(">>> Harvest ended without a valid session.\n", flush=True)
    return cookie, ua.strip()


def harvest_cookie_interactive(
    *,
    try_auto_solve: bool = False,
    quiet: bool = False,
) -> tuple[str, str]:
    """Open Chrome; close only when captcha flow fully complete."""
    from seleniumbase import SB

    if not quiet:
        print("\n>>> Opening Chrome...", flush=True)
    with SB(uc=True, headed=True, locale="en", **tor_util.sb_proxy_kw()) as sb:
        sb.activate_cdp_mode(START_URL)
        return run_harvest_loop(sb, try_auto_solve=try_auto_solve, quiet=quiet)


def _handle_ip_block_from_harvest(*, quiet: bool = False) -> None:
    """Delegate to auto_refresh IP cooldown (avoid import cycle at module load)."""
    import auto_refresh

    auto_refresh._handle_ip_block(quiet=quiet)
