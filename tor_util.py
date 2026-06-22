"""Optional Tor SOCKS proxy for CanLII requests (local Tor Browser or tor daemon)."""
from __future__ import annotations

import os
import socket
import sys
import time

_SOCKS_URL: str | None = None
_ENABLED = False
_CONTROL_PORT = 9051


def enabled() -> bool:
    return _ENABLED


def socks_url() -> str | None:
    return _SOCKS_URL


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _detect_socks_port() -> int:
    env = os.environ.get("TOR_SOCKS_PORT", "").strip()
    if env.isdigit():
        return int(env)
    for port in (9050, 9150):
        if _port_open("127.0.0.1", port):
            return port
    raise RuntimeError(
        "Tor is not running. Start Tor Browser, or run: brew install tor && tor "
        "(SOCKS on 9050 or 9150)."
    )


def configure(*, use_tor: bool = False) -> None:
    """Enable routing through local Tor SOCKS proxy."""
    global _ENABLED, _SOCKS_URL, _CONTROL_PORT
    if not use_tor:
        _ENABLED = False
        _SOCKS_URL = None
        return
    port = _detect_socks_port()
    _SOCKS_URL = f"socks5h://127.0.0.1:{port}"
    _ENABLED = True
    ctrl = os.environ.get("TOR_CONTROL_PORT", "").strip()
    _CONTROL_PORT = int(ctrl) if ctrl.isdigit() else 9051


def print_status() -> None:
    if not enabled():
        print("Tor: off", flush=True)
        return
    print(f"Tor: on ({socks_url()})", flush=True)


def request_new_identity(*, wait_sec: float = 5.0) -> bool:
    """Ask Tor for a new exit IP (needs ControlPort in torrc)."""
    if not enabled():
        return False
    if not _port_open("127.0.0.1", _CONTROL_PORT, timeout=0.3):
        print("[tor] Control port not open — cannot rotate IP.", file=sys.stderr, flush=True)
        return False
    try:
        from stem import Signal
        from stem.control import Controller

        with Controller.from_port(port=_CONTROL_PORT) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        time.sleep(wait_sec)
        print("[tor] New circuit requested.\n", flush=True)
        return True
    except ImportError:
        pass
    except Exception as e:
        print(f"[tor] NEWNYM failed: {e}", file=sys.stderr, flush=True)
        return False

    # Minimal fallback without stem (cookie auth only).
    try:
        with socket.create_connection(("127.0.0.1", _CONTROL_PORT), timeout=2) as sock:
            sock.sendall(b'AUTHENTICATE ""\r\n')
            sock.recv(256)
            sock.sendall(b"SIGNAL NEWNYM\r\n")
            sock.recv(256)
        time.sleep(wait_sec)
        print("[tor] New circuit requested.\n", flush=True)
        return True
    except Exception as e:
        print(f"[tor] NEWNYM failed: {e}", file=sys.stderr, flush=True)
        return False


def curl_proxy() -> str | None:
    return socks_url() if enabled() else None


def sb_proxy_kw() -> dict:
    """SeleniumBase proxy kwarg (socks5 without the h suffix)."""
    url = socks_url()
    if not url:
        return {}
    return {"proxy": url.replace("socks5h://", "socks5://")}
