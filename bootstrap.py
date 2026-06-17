"""Create session.py from the committed template on a fresh clone."""
from __future__ import annotations

import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
SESSION = _ROOT / "session.py"
TEMPLATE = _ROOT / "session.py.template"


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
