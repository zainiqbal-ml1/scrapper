"""OS detection helpers for cookie harvest / browser automation."""
from __future__ import annotations

import platform
import shutil
from pathlib import Path


def system() -> str:
    return platform.system()  # Darwin, Linux, Windows


def is_macos() -> bool:
    return system() == "Darwin"


def is_linux() -> bool:
    return system() == "Linux"


def is_windows() -> bool:
    return system() == "Windows"


def has_osascript() -> bool:
    return is_macos() and shutil.which("osascript") is not None


def chrome_macos_installed() -> bool:
    """True when Google Chrome.app is present (AppleScript harvest needs this)."""
    return Path("/Applications/Google Chrome.app").exists()


def harvest_backend() -> str:
    """Human-readable label for which harvest path will be used."""
    if is_macos() and has_osascript():
        return "macOS (AppleScript + SeleniumBase fallback)"
    if is_linux():
        return "Linux (system Chrome fast harvest + SeleniumBase fallback)"
    return "SeleniumBase browser (Windows or no osascript)"


def default_workers() -> int:
    """Sensible parallel worker count for this OS."""
    return 4 if is_linux() else 1


def default_rate() -> float:
    """Sensible requests/sec cap for this OS."""
    return 4.0 if is_linux() else 2.0


APPLE_EVENTS_CACHE = Path(".mac_apple_events_ok")


def set_apple_events_works(ok: bool) -> None:
    """Remember whether Chrome allows JS from Apple Events on this Mac."""
    if is_macos():
        try:
            APPLE_EVENTS_CACHE.write_text("1" if ok else "0")
        except Exception:
            pass


def apple_events_works() -> bool:
    """Cached result of Chrome > Developer > Allow JavaScript from Apple Events."""
    if not is_macos():
        return False
    if APPLE_EVENTS_CACHE.exists():
        return APPLE_EVENTS_CACHE.read_text().strip() == "1"
    return False
