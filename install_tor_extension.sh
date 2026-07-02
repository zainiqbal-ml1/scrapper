#!/usr/bin/env bash
# Permanently install CanLII PDF extension + Tor New Identity native host (macOS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_SRC="$SCRIPT_DIR/tor_extension"
EXT_ID="canlii-pdf@local"
XPI="$SCRIPT_DIR/canlii-pdf.xpi"
NATIVE_SRC="$EXT_SRC/native/canlii_tor_host.py"
NATIVE_INSTALL="${HOME}/.local/bin/canlii_tor_host.py"
NATIVE_MANIFEST_SRC="$EXT_SRC/native/com.canlii.tor.json"

if [[ ! -f "$EXT_SRC/manifest.json" ]]; then
  echo "error: tor_extension/manifest.json not found" >&2
  exit 1
fi

echo "Building $XPI ..."
rm -f "$XPI"
(cd "$EXT_SRC" && zip -qr "$XPI" . -x "*.DS_Store")

TB_DATA="${HOME}/Library/Application Support/TorBrowser-Data/Browser"
if [[ ! -d "$TB_DATA" ]]; then
  echo "Tor Browser profile not found at:" >&2
  echo "  $TB_DATA" >&2
  echo "Open Tor Browser once, then run this script again." >&2
  exit 1
fi

PROFILE="$(find "$TB_DATA" -maxdepth 1 -type d -name '*.default*' 2>/dev/null | head -1)"
if [[ -z "$PROFILE" ]]; then
  echo "No Tor Browser profile directory found under $TB_DATA" >&2
  exit 1
fi

EXT_DIR="$PROFILE/extensions"
mkdir -p "$EXT_DIR"
cp -f "$XPI" "$EXT_DIR/${EXT_ID}.xpi"

USER_JS="$PROFILE/user.js"
touch "$USER_JS"
grep -q 'xpinstall.signatures.required' "$USER_JS" || \
  echo 'user_pref("xpinstall.signatures.required", false);' >> "$USER_JS"
grep -q 'extensions.torbutton.confirm_newnym' "$USER_JS" || \
  echo 'user_pref("extensions.torbutton.confirm_newnym", false);' >> "$USER_JS"

# Native messaging host (real Tor New Identity via menu + NEWNYM).
mkdir -p "${HOME}/.local/bin"
cp -f "$NATIVE_SRC" "$NATIVE_INSTALL"
chmod +x "$NATIVE_INSTALL"

NM_DIR="${HOME}/Library/Application Support/Mozilla/NativeMessagingHosts"
mkdir -p "$NM_DIR"
sed "s|__HOST_PATH__|${NATIVE_INSTALL}|g" "$NATIVE_MANIFEST_SRC" > "$NM_DIR/com.canlii.tor.json"

echo ""
echo "Installed permanently:"
echo "  Extension: $EXT_DIR/${EXT_ID}.xpi"
echo "  Native host: $NATIVE_INSTALL"
echo "  Native manifest: $NM_DIR/com.canlii.tor.json"
echo ""
echo "IMPORTANT — one-time macOS permission for real New Identity:"
echo "  System Settings → Privacy & Security → Accessibility"
echo "  Add and enable: Terminal (or iTerm) AND Tor Browser"
echo "  (AppleScript clicks File → New Identity when blocked)"
echo ""
echo "Restart Tor Browser completely (quit and reopen)."
echo ""
echo "Test native host (Tor Browser must be running):"
echo "  python3 -c \"import struct,json,subprocess; p=subprocess.Popen(['$NATIVE_INSTALL'],stdin=subprocess.PIPE,stdout=subprocess.PIPE); m=json.dumps({'action':'ping'}).encode(); p.stdin.write(struct.pack('@I',len(m))+m); p.stdin.close(); print(p.stdout.read())\""
