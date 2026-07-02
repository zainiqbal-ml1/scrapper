#!/usr/bin/env python3
"""Native messaging host: Tor control NEWNYM + Tor Browser New Identity menu (macOS)."""
from __future__ import annotations

import glob
import json
import os
import platform
import socket
import struct
import subprocess
import sys
from pathlib import Path


def read_message() -> dict | None:
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    msg_len = struct.unpack("@I", raw_len)[0]
    if msg_len <= 0 or msg_len > 1_000_000:
        return None
    data = sys.stdin.buffer.read(msg_len)
    return json.loads(data.decode("utf-8"))


def send_message(obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def tor_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/TorBrowser-Data/Tor"
    if sys.platform.startswith("linux"):
        return home / ".tor browser" / "TorBrowser-Data/Tor"
    return home / "TorBrowser-Data/Tor"


def cookie_hex() -> str | None:
    path = tor_data_dir() / "control_auth_cookie"
    if not path.is_file():
        return None
    return path.read_bytes().hex()


def control_targets() -> list[tuple[str, object]]:
    targets: list[tuple[str, object]] = []
    data = tor_data_dir()
    sock = data / "control.socket"
    if sock.exists():
        targets.append(("unix", str(sock)))
    for p in glob.glob("/private/tmp/Tor-*/control.socket"):
        targets.append(("unix", p))
    targets.append(("tcp", ("127.0.0.1", 9151)))
    targets.append(("tcp", ("127.0.0.1", 9051)))
    return targets


def tor_control_cmd(commands: str, timeout: float = 5.0) -> tuple[bool, str]:
    cookie = cookie_hex()
    if not cookie:
        return False, "control_auth_cookie not found (is Tor Browser running?)"

    auth = f'AUTHENTICATE {cookie}\r\n'
    payload = (auth + commands).encode("utf-8")
    errors: list[str] = []

    for kind, addr in control_targets():
        try:
            if kind == "unix":
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(addr)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(addr)
            s.sendall(payload)
            chunks = []
            while True:
                try:
                    part = s.recv(4096)
                except socket.timeout:
                    break
                if not part:
                    break
                chunks.append(part)
            s.close()
            text = b"".join(chunks).decode("utf-8", errors="replace")
            if "250 OK" in text or "250 Closing" in text:
                return True, text.strip()
            errors.append(f"{kind}:{addr} -> {text.strip()[:200]}")
        except OSError as e:
            errors.append(f"{kind}:{addr} -> {e}")
    return False, "; ".join(errors) or "no control port reachable"


def signal_newnym() -> tuple[bool, str]:
    return tor_control_cmd("SIGNAL NEWNYM\r\nQUIT\r\n")


def click_new_identity_macos() -> tuple[bool, str]:
    if platform.system() != "Darwin":
        return False, "menu automation only on macOS"
    script = r'''
on run
  tell application "Tor Browser" to activate
  delay 1.2
  tell application "System Events"
    set procNames to {"Tor Browser", "firefox"}
    repeat with procName in procNames
      if exists process procName then
        tell process procName
          try
            click menu item "New Identity" of menu "File" of menu bar 1
            return "ok:file"
          on error err1
            try
              click menu item "New identity" of menu "File" of menu bar 1
              return "ok:file-lower"
            on error err2
              try
                click menu item "New Identity" of menu 1 of menu bar item "File" of menu bar 1
                return "ok:file-alt"
              on error err3
                return "fail:" & err3
              end try
            end try
          end try
        end tell
      end if
    end repeat
    return "fail:no-process"
  end tell
end run
'''
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        text = (out.stdout or out.stderr or "").strip()
        if text.startswith("ok:"):
            return True, text
        return False, text or "AppleScript failed"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def new_identity() -> dict:
    nym_ok, nym_msg = signal_newnym()
    ui_ok, ui_msg = click_new_identity_macos()
    if ui_ok:
        return {
            "ok": True,
            "newnym": nym_ok,
            "method": "tor-menu",
            "detail": ui_msg,
        }
    if nym_ok:
        return {
            "ok": True,
            "newnym": True,
            "method": "newnym-only",
            "detail": f"NEWNYM sent; menu click failed: {ui_msg}",
        }
    return {
        "ok": False,
        "error": f"NEWNYM: {nym_msg}; menu: {ui_msg}",
    }


def main() -> None:
    msg = read_message()
    if not msg:
        return
    action = msg.get("action", "new_identity")
    if action == "ping":
        send_message({"ok": True, "pong": True})
        return
    if action == "newnym":
        ok, detail = signal_newnym()
        send_message({"ok": ok, "detail": detail})
        return
    if action == "new_identity":
        send_message(new_identity())
        return
    send_message({"ok": False, "error": f"unknown action: {action}"})


if __name__ == "__main__":
    main()
