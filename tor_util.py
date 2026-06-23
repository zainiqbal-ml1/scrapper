"""Optional Tor SOCKS proxy for CanLII requests (local Tor Browser or tor daemon)."""
from __future__ import annotations

import os
import socket
import sys
import time

_SOCKS_URL: str | None = None
_ENABLED = False
_CONTROL_PORT = 9051
_ip: str | None = None
_last_cookie_ip: str | None = None
_MAX_ROTATE_ATTEMPTS = 6
_NEWNYM_WAIT_SEC = 11.0  # Tor rate-limits NEWNYM to ~1 per 10s


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


def _default_control_port(socks_port: int) -> int:
    """Tor Browser uses 9150/9151; tor daemon uses 9050/9051."""
    return {9050: 9051, 9150: 9151}.get(socks_port, 9051)


def configure(*, use_tor: bool = False) -> None:
    """Enable routing through local Tor SOCKS proxy."""
    global _ENABLED, _SOCKS_URL, _CONTROL_PORT
    invalidate_ip()
    if not use_tor:
        _ENABLED = False
        _SOCKS_URL = None
        return
    port = _detect_socks_port()
    _SOCKS_URL = f"socks5h://127.0.0.1:{port}"
    _ENABLED = True
    ctrl = os.environ.get("TOR_CONTROL_PORT", "").strip()
    _CONTROL_PORT = int(ctrl) if ctrl.isdigit() else _default_control_port(port)


def print_status() -> None:
    if not enabled():
        print("Tor: off", flush=True)
        return
    print(f"Tor: on ({socks_url()})", flush=True)


def fetch_public_ip(*, timeout: float = 10.0) -> str | None:
    """Return the outbound public IP (via Tor when enabled)."""
    from curl_cffi import requests

    proxy = curl_proxy()
    kwargs: dict = {"timeout": timeout, "impersonate": "chrome146"}
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = requests.get(url, proxy=proxy, **kwargs) if proxy else requests.get(url, **kwargs)
            if r.status_code == 200:
                ip = (r.text or "").strip()
                if ip:
                    return ip
        except Exception:
            continue
    return None


def invalidate_ip() -> None:
    """Clear cached IP — call when the route changes (new cookie / new Tor circuit)."""
    global _ip
    _ip = None


def public_ip(*, force: bool = False) -> str:
    """Cached public IP; refetch only after invalidate_ip() or force=True."""
    global _ip
    if not force and _ip is not None:
        return _ip
    _ip = fetch_public_ip() or "?"
    return _ip


def ip_label(*, force: bool = False) -> str:
    """Short label for progress output."""
    ip = public_ip(force=force)
    if enabled():
        return f"Tor IP {ip}"
    return f"IP {ip}"


def print_ip(*, force: bool = True) -> None:
    print(f"Outbound: {ip_label(force=force)}", flush=True)


def _exclude_exit_and_newnym(exit_ip: str, *, wait_sec: float = 4.0) -> bool:
    """Exclude the current exit fingerprint, then signal NEWNYM."""
    try:
        from stem import Signal
        from stem.control import Controller
    except ImportError:
        return False

    try:
        with Controller.from_port(port=_CONTROL_PORT) as controller:
            controller.authenticate()
            fp = None
            for desc in controller.get_network_statuses():
                if desc.address == exit_ip:
                    fp = desc.fingerprint
                    break
            if fp:
                old = (controller.get_conf("ExcludeExitNodes", default="") or "").strip()
                parts = [p for p in old.split(",") if p.strip() and p.strip() != fp]
                parts.append(fp)
                controller.set_conf("ExcludeExitNodes", ",".join(parts[-30:]))
            controller.signal(Signal.NEWNYM)
        time.sleep(wait_sec)
        invalidate_ip()
        return True
    except Exception:
        return False


def request_new_identity(*, wait_sec: float = 5.0, quiet: bool = False) -> bool:
    """Ask Tor for a new exit IP (needs ControlPort in torrc)."""
    if not enabled():
        return False
    if not _port_open("127.0.0.1", _CONTROL_PORT, timeout=0.3):
        if not quiet:
            print(
                f"[tor] Control port {_CONTROL_PORT} not open — cannot rotate IP. "
                "Use Tor Browser → New Identity.",
                file=sys.stderr,
                flush=True,
            )
        return False
    try:
        from stem import Signal
        from stem.control import Controller

        with Controller.from_port(port=_CONTROL_PORT) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        time.sleep(wait_sec)
        invalidate_ip()
        if not quiet:
            new = public_ip(force=True)
            print(f"[tor] NEWNYM — exit now {new}\n", flush=True)
        return True
    except ImportError:
        pass
    except Exception as e:
        if not quiet:
            print(f"[tor] NEWNYM failed: {e}", file=sys.stderr, flush=True)
        return False

    # Minimal fallback without stem (cookie auth only; works for tor daemon).
    try:
        with socket.create_connection(("127.0.0.1", _CONTROL_PORT), timeout=2) as sock:
            sock.sendall(b'AUTHENTICATE ""\r\n')
            sock.recv(256)
            sock.sendall(b"SIGNAL NEWNYM\r\n")
            sock.recv(256)
        time.sleep(wait_sec)
        invalidate_ip()
        if not quiet:
            new = public_ip(force=True)
            print(f"[tor] NEWNYM — exit now {new}\n", flush=True)
        return True
    except Exception as e:
        if not quiet:
            print(f"[tor] NEWNYM failed: {e}", file=sys.stderr, flush=True)
        return False


def mark_cookie_ip() -> str:
    """Remember exit IP for the cookie we just minted."""
    global _last_cookie_ip
    _last_cookie_ip = public_ip(force=True)
    return _last_cookie_ip


def rotate_for_new_cookie(*, quiet: bool = False) -> bool:
    """Request a Tor exit different from the last cookie's IP (retry until changed)."""
    if not enabled():
        return False
    old = _last_cookie_ip or public_ip(force=True)
    if not quiet:
        print(f"[tor] Cookie refresh — need new exit (last cookie was {old})...", flush=True)

    for attempt in range(1, _MAX_ROTATE_ATTEMPTS + 1):
        if attempt > 1:
            if not quiet:
                print(
                    f"[tor] Exit unchanged ({old}) — retry "
                    f"({attempt}/{_MAX_ROTATE_ATTEMPTS})...",
                    flush=True,
                )
            time.sleep(_NEWNYM_WAIT_SEC)
        rotated = _exclude_exit_and_newnym(old, wait_sec=4.0)
        if not rotated:
            rotated = request_new_identity(wait_sec=4.0, quiet=True)
        if not rotated:
            continue
        new = public_ip(force=True)
        if new != "?" and new != old:
            if not quiet:
                print(f"[tor] New exit: {old} → {new}\n", flush=True)
            return True
        old = new

    if not quiet:
        print(
            f"[tor] Could not get a different exit after trying "
            f"(still {public_ip(force=True)}). Use Tor Browser → New Identity.",
            file=sys.stderr,
            flush=True,
        )
    return False


def can_reach_canlii() -> bool:
    """Quick probe: can the current route load CanLII (before opening Chrome)?"""
    from curl_cffi import requests

    proxy = curl_proxy()
    try:
        r = requests.get(
            "https://www.canlii.org/en/on/",
            proxy=proxy,
            timeout=15,
            impersonate="chrome146",
            allow_redirects=True,
        )
        text = (r.text or "")[:16000]
        low = text.lower()
        if any(m in low for m in (
            "err_proxy", "err_tunnel", "can't be reached", "network error",
        )):
            return False
        return r.status_code in (200, 403, 429) or "canlii" in low
    except Exception:
        return False


def ensure_exit_can_reach_canlii(*, quiet: bool = False) -> bool:
    """Rotate until curl can reach CanLII through the current Tor exit."""
    if not enabled():
        return True
    for attempt in range(_MAX_ROTATE_ATTEMPTS):
        if can_reach_canlii():
            return True
        if not quiet:
            print("[tor] Exit cannot reach CanLII — rotating...", flush=True)
        if not rotate_for_new_cookie(quiet=quiet) and attempt + 1 >= _MAX_ROTATE_ATTEMPTS:
            break
    return can_reach_canlii()


def curl_proxy() -> str | None:
    return socks_url() if enabled() else None


def session_get(session, url: str, **kwargs):
    """GET through Tor when enabled (curl_cffi / requests Session)."""
    proxy = curl_proxy()
    if proxy:
        return session.get(url, proxy=proxy, **kwargs)
    return session.get(url, **kwargs)


def sb_proxy_kw() -> dict:
    """SeleniumBase proxy kwarg (socks5 without the h suffix)."""
    url = socks_url()
    if not url:
        return {}
    return {"proxy": url.replace("socks5h://", "socks5://")}
