"""Optional Tor SOCKS proxy for CanLII requests (local Tor Browser or tor daemon).

Supports multiple parallel lanes — each lane is a separate SOCKS/control port pair
with its own exit IP and cookie (see tor-lane2.torrc for lane 1).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_ENABLED = False
_LANES: list["_Lane"] = []
_TLS = threading.local()
_LANE2_PROC: subprocess.Popen | None = None

DEFAULT_GOOD_EXIT_PDF_THRESHOLD = 10
GOOD_EXIT_PDF_THRESHOLD = DEFAULT_GOOD_EXIT_PDF_THRESHOLD
_MAX_ROTATE_ATTEMPTS = 6
_NEWNYM_WAIT_SEC = 11.0  # Tor rate-limits NEWNYM to ~1 per 10s

_LANE0_PORTS = (9150, 9050)
_LANE1_PORTS = (9052, 9152, 9060, 9054, 9154)


class _Lane:
    __slots__ = (
        "lane_id", "socks_port", "control_port",
        "_ip", "_last_cookie_ip", "_keep_same_exit", "_last_burn_downloads",
    )

    def __init__(self, lane_id: int, socks_port: int, control_port: int) -> None:
        self.lane_id = lane_id
        self.socks_port = socks_port
        self.control_port = control_port
        self._ip: str | None = None
        self._last_cookie_ip: str | None = None
        self._keep_same_exit = False
        self._last_burn_downloads = 0

    @property
    def socks_url(self) -> str:
        return f"socks5h://127.0.0.1:{self.socks_port}"

    def invalidate_ip(self) -> None:
        self._ip = None

    def clear_keep_exit(self) -> None:
        self._keep_same_exit = False


def enabled() -> bool:
    return _ENABLED


def lane_count() -> int:
    return len(_LANES) if _ENABLED else 0


def set_current_lane(lane_id: int) -> None:
    _TLS.lane_id = lane_id


def current_lane_id() -> int:
    return getattr(_TLS, "lane_id", 0)


def _lane(lane_id: int | None = None) -> _Lane:
    if not _LANES:
        raise RuntimeError("Tor is not configured")
    lid = current_lane_id() if lane_id is None else lane_id
    if lid < 0 or lid >= len(_LANES):
        lid = 0
    return _LANES[lid]


def set_good_exit_threshold(n: int) -> None:
    global GOOD_EXIT_PDF_THRESHOLD
    GOOD_EXIT_PDF_THRESHOLD = max(1, int(n))


def good_exit_threshold() -> int:
    return GOOD_EXIT_PDF_THRESHOLD


def socks_url(lane_id: int | None = None) -> str | None:
    if not _ENABLED:
        return None
    return _lane(lane_id).socks_url


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_control_port(socks_port: int) -> int:
    return {9050: 9051, 9150: 9151, 9052: 9053, 9152: 9153}.get(socks_port, socks_port + 1)


def _env_port(key: str) -> int | None:
    val = os.environ.get(key, "").strip()
    return int(val) if val.isdigit() else None


def _pick_port(candidates: tuple[int, ...], used: set[int]) -> int | None:
    for port in candidates:
        if port not in used and _port_open("127.0.0.1", port):
            return port
    return None


def _find_tor_binary() -> str | None:
    for candidate in ("tor", "/opt/homebrew/bin/tor", "/usr/local/bin/tor"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("tor")


def _lane2_torrc() -> Path:
    return Path(__file__).resolve().parent / "tor-lane2.torrc"


def _try_spawn_lane2_tor() -> bool:
    """Start tor daemon for lane 1 if port 9052 is not already listening."""
    global _LANE2_PROC
    if _port_open("127.0.0.1", 9052):
        return True
    tor_bin = _find_tor_binary()
    torrc = _lane2_torrc()
    if not tor_bin:
        return False
    if not torrc.exists():
        return False
    print(f"[tor] Starting lane 1 ({tor_bin} -f {torrc.name})...", flush=True)
    try:
        _LANE2_PROC = subprocess.Popen(
            [tor_bin, "-f", str(torrc)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    for _ in range(60):
        if _port_open("127.0.0.1", 9052):
            print("[tor] Lane 1 ready on SOCKS 9052.\n", flush=True)
            return True
        if _LANE2_PROC.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _lane1_unavailable_message(lane_idx: int) -> str:
    tor_bin = _find_tor_binary()
    torrc = _lane2_torrc()
    lines = [
        f"Tor lane {lane_idx} is not available (need a second SOCKS port on 9052).",
    ]
    if not tor_bin:
        lines.append("  Install tor:  brew install tor")
    lines.append(f"  Then re-run (auto-starts lane 1), or in another terminal:")
    lines.append(f"    tor -f {torrc}")
    lines.append(f"  Or set TOR_LANE{lane_idx}_SOCKS_PORT to an open SOCKS port.")
    return "\n".join(lines)


def _build_lanes(n: int) -> list[_Lane]:
    used: set[int] = set()
    lanes: list[_Lane] = []

    p0 = _env_port("TOR_LANE0_SOCKS_PORT") or _env_port("TOR_SOCKS_PORT")
    if p0 and _port_open("127.0.0.1", p0):
        port0 = p0
    else:
        port0 = _pick_port(_LANE0_PORTS, used)
        if port0 is None:
            raise RuntimeError(
                "Tor is not running. Start Tor Browser, or run: brew install tor && tor "
                "(SOCKS on 9050 or 9150)."
            )
    used.add(port0)
    ctrl0 = _env_port("TOR_LANE0_CONTROL_PORT") or _env_port("TOR_CONTROL_PORT")
    lanes.append(_Lane(0, port0, ctrl0 or _default_control_port(port0)))

    for i in range(1, n):
        pi = _env_port(f"TOR_LANE{i}_SOCKS_PORT")
        if pi and pi not in used and _port_open("127.0.0.1", pi):
            port = pi
        else:
            extra = tuple(9050 + i * 2 + off for off in range(6))
            port = _pick_port(_LANE1_PORTS if i == 1 else extra, used)
        if port is None and i == 1:
            _try_spawn_lane2_tor()
            port = _pick_port(_LANE1_PORTS if i == 1 else extra, used)
        if port is None:
            raise RuntimeError(_lane1_unavailable_message(i))
        used.add(port)
        ci = _env_port(f"TOR_LANE{i}_CONTROL_PORT")
        lanes.append(_Lane(i, port, ci or _default_control_port(port)))
    return lanes


def configure(*, use_tor: bool = False, lanes: int = 1) -> None:
    """Enable routing through one or more local Tor SOCKS proxies."""
    global _ENABLED, _LANES
    if not use_tor:
        _ENABLED = False
        _LANES = []
        return
    _LANES = _build_lanes(max(1, lanes))
    _ENABLED = True
    set_current_lane(0)


def print_status() -> None:
    if not enabled():
        print("Tor: off", flush=True)
        return
    if len(_LANES) == 1:
        print(f"Tor: on ({_LANES[0].socks_url})", flush=True)
        return
    parts = [f"lane{ln.lane_id}={ln.socks_url}" for ln in _LANES]
    print(f"Tor: on ({len(_LANES)} lanes: {', '.join(parts)})", flush=True)


def curl_proxy(lane_id: int | None = None) -> str | None:
    return socks_url(lane_id)


def fetch_public_ip(*, timeout: float = 10.0, lane_id: int | None = None) -> str | None:
    from curl_cffi import requests

    proxy = curl_proxy(lane_id)
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


def invalidate_ip(lane_id: int | None = None) -> None:
    _lane(lane_id).invalidate_ip()


def public_ip(*, force: bool = False, lane_id: int | None = None) -> str:
    ln = _lane(lane_id)
    if not force and ln._ip is not None:
        return ln._ip
    ln._ip = fetch_public_ip(lane_id=ln.lane_id) or "?"
    return ln._ip


def ip_label(*, force: bool = False, lane_id: int | None = None) -> str:
    ln = _lane(lane_id)
    ip = public_ip(force=force, lane_id=ln.lane_id)
    tag = f"lane {ln.lane_id} " if len(_LANES) > 1 else ""
    if enabled():
        return f"Tor {tag}IP {ip}"
    return f"IP {ip}"


def print_ip(*, force: bool = True) -> None:
    if len(_LANES) <= 1:
        print(f"Outbound: {ip_label(force=force)}", flush=True)
        return
    for ln in _LANES:
        set_current_lane(ln.lane_id)
        print(f"Outbound lane {ln.lane_id}: {ip_label(force=force)}", flush=True)


def _exclude_exit_and_newnym(ln: _Lane, exit_ip: str, *, wait_sec: float = 4.0) -> bool:
    try:
        from stem import Signal
        from stem.control import Controller
    except ImportError:
        return False

    try:
        with Controller.from_port(port=ln.control_port) as controller:
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
        ln.invalidate_ip()
        return True
    except Exception:
        return False


def request_new_identity(*, wait_sec: float = 5.0, quiet: bool = False, lane_id: int | None = None) -> bool:
    if not enabled():
        return False
    ln = _lane(lane_id)
    if not _port_open("127.0.0.1", ln.control_port, timeout=0.3):
        if not quiet:
            print(
                f"[tor] Lane {ln.lane_id} control port {ln.control_port} not open — "
                "cannot rotate IP.",
                file=sys.stderr,
                flush=True,
            )
        return False
    try:
        from stem import Signal
        from stem.control import Controller

        with Controller.from_port(port=ln.control_port) as controller:
            controller.authenticate()
            controller.signal(Signal.NEWNYM)
        time.sleep(wait_sec)
        ln.invalidate_ip()
        ln.clear_keep_exit()
        if not quiet:
            new = public_ip(force=True, lane_id=ln.lane_id)
            print(f"[tor] Lane {ln.lane_id} NEWNYM — exit now {new}\n", flush=True)
        return True
    except ImportError:
        pass
    except Exception as e:
        if not quiet:
            print(f"[tor] Lane {ln.lane_id} NEWNYM failed: {e}", file=sys.stderr, flush=True)
        return False

    try:
        with socket.create_connection(("127.0.0.1", ln.control_port), timeout=2) as sock:
            sock.sendall(b'AUTHENTICATE ""\r\n')
            sock.recv(256)
            sock.sendall(b"SIGNAL NEWNYM\r\n")
            sock.recv(256)
        time.sleep(wait_sec)
        ln.invalidate_ip()
        ln.clear_keep_exit()
        if not quiet:
            new = public_ip(force=True, lane_id=ln.lane_id)
            print(f"[tor] Lane {ln.lane_id} NEWNYM — exit now {new}\n", flush=True)
        return True
    except Exception as e:
        if not quiet:
            print(f"[tor] Lane {ln.lane_id} NEWNYM failed: {e}", file=sys.stderr, flush=True)
        return False


def mark_cookie_ip(lane_id: int | None = None) -> str:
    ln = _lane(lane_id)
    ln._last_cookie_ip = public_ip(force=True, lane_id=ln.lane_id)
    return ln._last_cookie_ip


def note_cookie_burn(downloads_since_cookie: int, lane_id: int | None = None) -> None:
    ln = _lane(lane_id)
    ln._last_burn_downloads = max(0, downloads_since_cookie)
    ln._keep_same_exit = ln._last_burn_downloads >= GOOD_EXIT_PDF_THRESHOLD
    if not enabled():
        return
    ip = public_ip(lane_id=ln.lane_id)
    prefix = f"[tor] Lane {ln.lane_id} " if len(_LANES) > 1 else "[tor] "
    if ln._keep_same_exit:
        print(
            f"{prefix}Good exit {ip} ({ln._last_burn_downloads} PDFs) — "
            f"reusing IP for new cookie.",
            flush=True,
        )
    else:
        print(
            f"{prefix}Weak exit {ip} ({ln._last_burn_downloads} PDFs, "
            f"need {GOOD_EXIT_PDF_THRESHOLD}+) — rotating IP.",
            flush=True,
        )


def prepare_cookie_refresh(*, quiet: bool = False, force_rotate: bool = False, lane_id: int | None = None) -> bool:
    if not enabled():
        return False
    ln = _lane(lane_id)
    if not force_rotate and ln._keep_same_exit:
        if not quiet:
            tag = f"lane {ln.lane_id} " if len(_LANES) > 1 else ""
            print(
                f"[tor] Keeping good {tag}exit {public_ip(lane_id=ln.lane_id)} — "
                f"harvesting new cookie only.\n",
                flush=True,
            )
        return False
    return rotate_for_new_cookie(quiet=quiet, lane_id=ln.lane_id)


def rotate_for_new_cookie(*, quiet: bool = False, lane_id: int | None = None) -> bool:
    if not enabled():
        return False
    ln = _lane(lane_id)
    old = ln._last_cookie_ip or public_ip(force=True, lane_id=ln.lane_id)
    tag = f"lane {ln.lane_id} " if len(_LANES) > 1 else ""
    if not quiet:
        print(f"[tor] {tag}Cookie refresh — need new exit (last cookie was {old})...", flush=True)

    for attempt in range(1, _MAX_ROTATE_ATTEMPTS + 1):
        if attempt > 1:
            if not quiet:
                print(
                    f"[tor] {tag}Exit unchanged ({old}) — retry "
                    f"({attempt}/{_MAX_ROTATE_ATTEMPTS})...",
                    flush=True,
                )
            time.sleep(_NEWNYM_WAIT_SEC)
        rotated = _exclude_exit_and_newnym(ln, old, wait_sec=4.0)
        if not rotated:
            rotated = request_new_identity(wait_sec=4.0, quiet=True, lane_id=ln.lane_id)
        if not rotated:
            continue
        new = public_ip(force=True, lane_id=ln.lane_id)
        if new != "?" and new != old:
            if not quiet:
                print(f"[tor] {tag}New exit: {old} → {new}\n", flush=True)
            return True
        old = new

    if not quiet:
        print(
            f"[tor] {tag}Could not get a different exit after trying "
            f"(still {public_ip(force=True, lane_id=ln.lane_id)}).",
            file=sys.stderr,
            flush=True,
        )
    return False


def can_reach_canlii(lane_id: int | None = None) -> bool:
    from curl_cffi import requests

    proxy = curl_proxy(lane_id)
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


def ensure_exit_can_reach_canlii(*, quiet: bool = False, lane_id: int | None = None) -> bool:
    if not enabled():
        return True
    for attempt in range(_MAX_ROTATE_ATTEMPTS):
        if can_reach_canlii(lane_id):
            return True
        if not quiet:
            ln = _lane(lane_id)
            print(f"[tor] Lane {ln.lane_id} cannot reach CanLII — rotating...", flush=True)
        if not rotate_for_new_cookie(quiet=quiet, lane_id=lane_id) and attempt + 1 >= _MAX_ROTATE_ATTEMPTS:
            break
    return can_reach_canlii(lane_id)


def session_get(session, url: str, **kwargs):
    proxy = curl_proxy()
    if proxy:
        return session.get(url, proxy=proxy, **kwargs)
    return session.get(url, **kwargs)


def sb_proxy_kw(lane_id: int | None = None) -> dict:
    url = socks_url(lane_id)
    if not url:
        return {}
    return {"proxy": url.replace("socks5h://", "socks5://")}
