"""Simple captcha automation: reCAPTCHA checkbox click + OCR text captchas."""
from __future__ import annotations

import re
import sys
import time

_LAST_ATTEMPT = 0.0
ATTEMPT_INTERVAL = 3.0

_RECAPTCHA_FRAMES = (
    'iframe[title*="reCAPTCHA"]',
    'iframe[src*="recaptcha/api2/anchor"]',
    'iframe[src*="google.com/recaptcha"]',
)
_CHECKBOX_SELECTORS = (
    "#recaptcha-anchor",
    ".recaptcha-checkbox-border",
    ".recaptcha-checkbox",
)
_IMG_SELECTORS = (
    'img[src*="captcha"]',
    "img#captcha",
    "img.captcha",
    'img[alt*="captcha" i]',
)
_INPUT_SELECTORS = (
    'input[name*="captcha" i]',
    "input#captcha",
    'input[placeholder*="captcha" i]',
)


def try_solve(sb, *, quiet: bool = False) -> bool:
    """Try checkbox click and/or OCR text captcha. Returns True if an action ran."""
    global _LAST_ATTEMPT
    now = time.monotonic()
    if now - _LAST_ATTEMPT < ATTEMPT_INTERVAL:
        return False
    _LAST_ATTEMPT = now

    if _try_recaptcha_checkbox(sb, quiet=quiet):
        return True
    if _try_ocr_text_captcha(sb, quiet=quiet):
        return True
    return False


def _switch_default(sb) -> None:
    try:
        sb.switch_to_default_content()
    except Exception:
        pass


def _try_recaptcha_checkbox(sb, *, quiet: bool = False) -> bool:
    for frame_sel in _RECAPTCHA_FRAMES:
        try:
            sb.switch_to_frame(frame_sel, timeout=2)
        except Exception:
            continue
        for box_sel in _CHECKBOX_SELECTORS:
            try:
                if not sb.is_element_present(box_sel):
                    continue
                sb.click(box_sel, timeout=2)
                _switch_default(sb)
                if not quiet:
                    print(">>> Clicked reCAPTCHA checkbox.\n", flush=True)
                time.sleep(1.5)
                return True
            except Exception:
                continue
        _switch_default(sb)
    return False


def _ocr_bytes(image_bytes: bytes) -> str:
    import ddddocr

    ocr = ddddocr.DdddOcr(show_ad=False)
    text = ocr.classification(image_bytes)
    return re.sub(r"[^A-Za-z0-9]", "", (text or "").strip())


def _try_ocr_text_captcha(sb, *, quiet: bool = False) -> bool:
    try:
        import ddddocr  # noqa: F401
    except ImportError:
        if not quiet:
            print("[captcha] Install ddddocr for image OCR: pip install ddddocr", file=sys.stderr)
        return False

    img_sel = None
    for sel in _IMG_SELECTORS:
        try:
            if sb.is_element_present(sel):
                img_sel = sel
                break
        except Exception:
            continue
    if not img_sel:
        return False

    inp_sel = None
    for sel in _INPUT_SELECTORS:
        try:
            if sb.is_element_present(sel):
                inp_sel = sel
                break
        except Exception:
            continue
    if not inp_sel:
        return False

    try:
        png = sb.find_element(img_sel).screenshot_as_png
        text = _ocr_bytes(png)
        if len(text) < 3:
            return False
        sb.clear(inp_sel)
        sb.type(inp_sel, text)
        for submit in ('button[type="submit"]', 'input[type="submit"]', "#submit", ".btn-primary"):
            try:
                if sb.is_element_present(submit):
                    sb.click(submit)
                    break
            except Exception:
                continue
        if not quiet:
            print(f">>> OCR captcha entered ({len(text)} chars).\n", flush=True)
        time.sleep(1.5)
        return True
    except Exception as e:
        if not quiet:
            print(f"[captcha] OCR failed: {e}", file=sys.stderr, flush=True)
        return False
