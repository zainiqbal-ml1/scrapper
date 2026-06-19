"""Create session.py from the committed template on a fresh clone."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
SESSION = _ROOT / "session.py"
TEMPLATE = _ROOT / "session.py.template"
_ENV = _ROOT / ".env"
_env_loaded = False


def load_env_file() -> None:
    """Load KEY=value pairs from .env if present (does not override existing env)."""
    global _env_loaded
    if _env_loaded or not _ENV.exists():
        _env_loaded = True
        return
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    _env_loaded = True


def ensure_session_file() -> bool:
    load_env_file()
    """Copy session.py.template -> session.py if missing. Returns True if created."""
    if SESSION.exists() or not TEMPLATE.exists():
        return False
    shutil.copy(TEMPLATE, SESSION)
    print(
        "Created session.py from template (first run on this machine).\n"
        "  -> python run.py will open a browser to get a live cookie.\n"
    )
    return True
