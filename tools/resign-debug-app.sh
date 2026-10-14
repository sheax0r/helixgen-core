#!/usr/bin/env bash
# resign-debug-app.sh — make a Frida-attachable copy of the Helix Stadium editor.
#
# macOS notarizes the shipping app with the hardened runtime, which blocks Frida
# from attaching. This copies the app to ~/Helix Stadium Debug.app and ad-hoc
# re-signs it with the entitlements Frida needs (get-task-allow +
# disable-library-validation + unsigned-exec-memory for its JIT). Redo this per
# firmware — the copy carries whatever app version you point it at.
#
# Usage: tools/resign-debug-app.sh [SRC_APP] [DEST_APP]
# Defaults: SRC  = /Applications/Line6/Helix Stadium.app
#           DEST = ~/Helix Stadium Debug.app
set -euo pipefail
SRC="${1:-/Applications/Line6/Helix Stadium.app}"
DEST="${2:-$HOME/Helix Stadium Debug.app}"
[ -d "$SRC" ] || { echo "no app at: $SRC" >&2; exit 1; }

echo "== source: $SRC"
defaults read "$SRC/Contents/Info" CFBundleShortVersionString 2>/dev/null \
  | sed 's/^/== version: /' || true

rm -rf "$DEST"
cp -R "$SRC" "$DEST"

ENT="$(mktemp -t helix-debug-ent).plist"
cat > "$ENT" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.get-task-allow</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
</dict></plist>
PLIST

echo "== re-signing nested frameworks + dylibs (ad-hoc) ..."
find "$DEST/Contents/Frameworks" \( -name '*.dylib' -o -name '*.framework' \) \
  -maxdepth 1 2>/dev/null | while read -r item; do
  codesign --force --sign - --timestamp=none "$item" 2>/dev/null || true
done

echo "== re-signing the app ..."
codesign --force --deep --sign - --timestamp=none \
  --entitlements "$ENT" --options=runtime "$DEST"

rm -f "$ENT"
echo "== verifying get-task-allow ..."
if codesign -d --entitlements :- "$DEST" 2>/dev/null | grep -q 'get-task-allow'; then
  echo "== OK: $DEST is debuggable."
else
  echo "== WARNING: get-task-allow not present — Frida attach may still fail." >&2
fi
echo "== launch THIS copy (not /Applications), then run tools/frida_sftp_auth.py:"
echo "   open \"$DEST\""
