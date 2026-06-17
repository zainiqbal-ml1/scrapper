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
