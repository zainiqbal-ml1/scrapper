"""Create session.py from the committed template on a fresh clone."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
SESSION = _ROOT / "session.py"
TEMPLATE = _ROOT / "session.py.template"
VENV_CFG = _ROOT / ".venv" / "pyvenv.cfg"


def ensure_session_file() -> bool:
    """Copy session.py.template -> session.py if missing. Returns True if created."""
    if SESSION.exists() or not TEMPLATE.exists():
        return False
    shutil.copy(TEMPLATE, SESSION)
    print(
        "Created session.py from template (first run on this machine).\n"
        "  -> python run.py will open a browser to get a live cookie.\n"
    )
    return True


def warn_if_macos_venv_on_linux() -> None:
    """The committed .venv is macOS-built; Linux needs a local venv."""
    if sys.platform != "linux" or not VENV_CFG.exists():
        return
    try:
        text = VENV_CFG.read_text()
    except OSError:
        return
    if "/Users/" in text or "darwin" in text.lower():
        print(
            "\n*** Linux: the bundled .venv is from macOS and will not work here.\n"
            "    Run once:\n"
            "      rm -rf .venv && python3 -m venv .venv\n"
            "      .venv/bin/pip install -r requirements.txt\n"
            "    Then: python run.py\n",
            file=sys.stderr,
        )
