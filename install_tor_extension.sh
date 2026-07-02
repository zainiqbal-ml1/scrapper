#!/usr/bin/env bash
# Permanently install the CanLII PDF extension into Tor Browser (macOS).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_SRC="$SCRIPT_DIR/tor_extension"
EXT_ID="canlii-pdf@local"
XPI="$SCRIPT_DIR/canlii-pdf.xpi"

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
SIGNATURE_LINE='user_pref("xpinstall.signatures.required", false);'
if [[ -f "$USER_JS" ]]; then
  if ! grep -q 'xpinstall.signatures.required' "$USER_JS"; then
    echo "$SIGNATURE_LINE" >> "$USER_JS"
  fi
else
  echo "$SIGNATURE_LINE" > "$USER_JS"
fi

# Optional: disable New Identity confirmation so manual recovery is one click.
CONFIRM_LINE='user_pref("extensions.torbutton.confirm_newnym", false);'
if [[ -f "$USER_JS" ]]; then
  if ! grep -q 'extensions.torbutton.confirm_newnym' "$USER_JS"; then
    echo "$CONFIRM_LINE" >> "$USER_JS"
  fi
else
  echo "$CONFIRM_LINE" >> "$USER_JS"
fi

echo ""
echo "Installed permanently:"
echo "  $EXT_DIR/${EXT_ID}.xpi"
echo ""
echo "Restart Tor Browser completely (quit and reopen)."
echo "The extension will appear in about:addons — no about:debugging needed."
echo ""
echo "Tip: Tor → Settings → disable 'Always ask before New Identity' for faster manual recovery."
