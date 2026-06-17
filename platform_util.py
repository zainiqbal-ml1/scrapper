"""OS detection helpers for cookie harvest / browser automation."""
from __future__ import annotations

import platform
import shutil


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


def harvest_backend() -> str:
    """Human-readable label for which harvest path will be used."""
    if is_macos() and has_osascript():
        return "macOS (AppleScript + SeleniumBase fallback)"
    return "SeleniumBase browser (Linux/Windows or no osascript)"
