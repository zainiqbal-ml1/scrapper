"""DataDome slider — drag on the captcha tab (one open, solve, close)."""
from __future__ import annotations

import time

import browser_harvest

IFRAME_SEL = 'body > iframe[src*="/geo.captcha-delivery.com/captcha/"]'
SLIDER_SEL = "div.slider"
TARGET_SEL = "div.sliderTarget"
SOLVE_COOLDOWN_SEC = 5.0

_last_solve_at = 0.0


def try_solve_datadome_slider(sb, *, quiet: bool = False, overshoot: float = 12.0) -> bool:
    """Open geo captcha once, drag slider there, close tab. Returns True if drag ran."""
    global _last_solve_at
    now = time.monotonic()
    if now - _last_solve_at < SOLVE_COOLDOWN_SEC:
        return False

    cdp = sb.cdp if hasattr(sb, "cdp") else sb
    src = cdp.get_page_source() or ""
    if browser_harvest.page_ip_blocked_html(src):
        return False
    if not browser_harvest.is_datadome_slider_html(src):
        return False
    if not cdp.is_element_visible(IFRAME_SEL):
        return False

    captcha_url = cdp.get_attribute(IFRAME_SEL, "src")
    if not captcha_url:
        return False

    parent_tab = cdp.get_active_tab()
    try:
        cdp.open_new_tab(url=captcha_url)
        time.sleep(0.55)
        cdp.loop.run_until_complete(cdp.page.wait(0.2))

        for _ in range(40):
            tab_src = cdp.get_page_source() or ""
            if browser_harvest.page_ip_blocked_html(tab_src):
                return False
            if cdp.is_element_present(SLIDER_SEL) and cdp.is_element_present(TARGET_SEL):
                break
            time.sleep(0.12)
        else:
            if not quiet:
                print("[slider] Slider not ready on captcha page.\n", flush=True)
            return False

        cdp.bring_active_window_to_front()
        time.sleep(0.15)

        x1, y1 = cdp.get_gui_element_center(SLIDER_SEL)
        target = cdp.get_gui_element_rect(TARGET_SEL, timeout=2)
        x2 = target["x"] + target["width"] - 2 + overshoot
        y2 = y1

        if x2 <= x1 + 20:
            if not quiet:
                print("[slider] Track too short to drag.\n", flush=True)
            return False

        if not quiet:
            print(f">>> Slider drag on captcha tab ({int(x2 - x1)}px)...\n", flush=True)

        cdp.gui_drag_drop_points(x1, y1, x2, y2, timeframe=1.2)
        time.sleep(1.0)
        _last_solve_at = time.monotonic()
        return True
    finally:
        try:
            cdp.close_active_tab()
        except Exception:
            pass
        try:
            cdp.switch_to_tab(parent_tab)
        except Exception:
            pass
        time.sleep(0.25)
