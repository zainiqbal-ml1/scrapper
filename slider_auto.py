"""DataDome slider auto-drag with correct iframe mapping and full travel."""
from __future__ import annotations

import time

import browser_harvest

IFRAME_SEL = 'body > iframe[src*="/geo.captcha-delivery.com/captcha/"]'
SLIDER_SEL = "div.slider"
TARGET_SEL = "div.sliderTarget"


def try_solve_datadome_slider(sb, *, quiet: bool = False, overshoot: float = 10.0) -> bool:
    """Drag the DataDome slider to the end of the track. Returns True if drag ran."""
    cdp = sb.cdp if hasattr(sb, "cdp") else sb
    src = cdp.get_page_source() or ""
    if not browser_harvest.is_datadome_slider_html(src):
        return False
    if not cdp.is_element_visible(IFRAME_SEL):
        return False

    points = _measure_slider_points(cdp)
    if not points:
        if not quiet:
            print("[slider] Could not measure slider — retrying.\n", flush=True)
        return False

    x1, y1, x2, y2 = points
    x2 += overshoot

    if not quiet:
        dist = int(x2 - x1)
        print(f">>> Slider drag ({dist}px)...\n", flush=True)

    cdp.bring_active_window_to_front()
    time.sleep(0.08)
    cdp.gui_drag_drop_points(x1, y1, x2, y2, timeframe=1.15)
    time.sleep(0.35)
    return True


def _measure_slider_points(cdp) -> tuple[float, float, float, float] | None:
    """Map slider handle → track end in screen coordinates (iframe-aware)."""
    iframe_gui = cdp.get_gui_element_rect(IFRAME_SEL, timeout=2)
    captcha_url = cdp.get_attribute(IFRAME_SEL, "src")
    if not captcha_url:
        return None

    tab = cdp.get_active_tab()
    try:
        cdp.open_new_tab(url=captcha_url)
        time.sleep(0.45)
        cdp.loop.run_until_complete(cdp.page.wait(0.15))

        for _ in range(24):
            if cdp.is_element_present(SLIDER_SEL) and cdp.is_element_present(TARGET_SEL):
                break
            time.sleep(0.12)
        if not cdp.is_element_present(SLIDER_SEL) or not cdp.is_element_present(TARGET_SEL):
            return None

        slider = cdp.get_element_rect(SLIDER_SEL, timeout=2)
        target = cdp.get_element_rect(TARGET_SEL, timeout=2)
        captcha_w = float(cdp.evaluate("document.documentElement.clientWidth") or 0)
        captcha_h = float(cdp.evaluate("document.documentElement.clientHeight") or 0)
        if captcha_w < 50 or captcha_h < 50:
            return None

        rel_x1 = slider["x"] + slider["width"] / 2.0
        rel_y = slider["y"] + slider["height"] / 2.0
        rel_x2 = target["x"] + target["width"] - 2.0

        scale_x = iframe_gui["width"] / captcha_w
        scale_y = iframe_gui["height"] / captcha_h

        x1 = iframe_gui["x"] + rel_x1 * scale_x
        x2 = iframe_gui["x"] + rel_x2 * scale_x
        y1 = iframe_gui["y"] + rel_y * scale_y
        y2 = y1
    finally:
        try:
            cdp.close_active_tab()
        except Exception:
            pass
        try:
            cdp.switch_to_tab(tab)
        except Exception:
            pass

    if x2 <= x1 + 20:
        return None
    return (x1, y1, x2, y2)
