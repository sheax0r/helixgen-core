#!/usr/bin/env bash
# find-hedit-key.sh — locate the Helix Stadium editor's `hedit` SFTP credential.
#
# helixgen moves IR files to/from the Stadium over SFTP as user `hedit`
# (docs/helix-sftp-access.md). App <=1.3.2 shipped the key at
# Contents/Resources/sshKeys/id_hedit; app 1.4 dropped that folder and the device
# rejects the old key (hgc-6r5.11), breaking push-ir/pull-ir/backup IR collection.
# This hunts the 1.4 bundle for an embedded key, tests each candidate against a
# real device with a READ-ONLY listing, and on a hit installs it where helixgen
# looks (~/.helixgen/id_hedit). The `hedit` key is a shared credential shipped
# identically in every copy of the app, not a per-user secret.
# Candidate bytes go to files + fingerprints, never stdout. Device is only LISTED.
#
# Usage: tools/find-hedit-key.sh [DEVICE_IP] [APP_PATH]
set -euo pipefail
IP="${1:-${HELIXGEN_HELIX_IP:-192.168.4.84}}"
APP_DIR="${2:-/Applications/Line6/Helix Stadium.app}"
BIN="$APP_DIR/Contents/MacOS/Helix Stadium"
OUT="${HELIX_KEYHUNT_DIR:-$HOME/helix-hedit-hunt}"
DEST="$HOME/.helixgen/id_hedit"
[ -f "$BIN" ] || { echo "no app binary at: $BIN" >&2; exit 1; }
mkdir -p "$OUT"; cd "$OUT"; rm -f cand_* try_key cand_pem.txt
echo "== app: $APP_DIR"; echo "== device: $IP   == scratch: $OUT"
# 1. embedded PEM/OpenSSH block (libssh2 ships the markers as code; a real key
#    shows base64 BETWEEN them).
LC_ALL=C strings -a "$BIN" | LC_ALL=C awk '
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/ {k=1}
  k {print > "cand_pem.txt"}
  /-----END [A-Z ]*PRIVATE KEY-----/ {k=0; print "" > "cand_pem.txt"}' || true
if [ -s cand_pem.txt ] && [ "$(grep -c . cand_pem.txt)" -gt 2 ]; then
  cp cand_pem.txt cand_00.pem
  echo "cand_00.pem  (embedded PEM, $(grep -c . cand_00.pem) non-blank lines)"
fi
# 2. long base64 runs -> one file each.
LC_ALL=C strings -a -n 100 "$BIN" \
  | LC_ALL=C grep -E '^[A-Za-z0-9+/]{100,}={0,2}$' \
  | awk '{f=sprintf("cand_%02d.b64",NR); print > f}'
echo "== candidates (name / bytes / sha256) — contents NOT shown:"
shopt -s nullglob; cands=(cand_*)
if [ "${#cands[@]}" -eq 0 ]; then
  echo "  (none) — no embedded key blob; the 1.4 key is not a plain binary string."
  echo "  Use the Frida route: tools/frida_sftp_raw.py while importing one IR."
  exit 2
fi
for f in "${cands[@]}"; do
  printf '  %-14s %8s bytes  %s\n' "$f" "$(wc -c < "$f" | tr -d ' ')" \
    "$(shasum -a256 "$f" | cut -d' ' -f1)"
done
# 3. test each as an SFTP key (read-only ls).
echo "== testing each against hedit@$IP (read-only ls) ..."
WIN=""
for f in "${cands[@]}"; do
  cp "$f" try_key; chmod 600 try_key
  if printf 'ls data/stadium-family-fw/ir\n' | \
     sftp -i try_key -o BatchMode=yes -o StrictHostKeyChecking=no \
          -o PasswordAuthentication=no -o ConnectTimeout=8 \
          "hedit@$IP" >/dev/null 2>&1; then WIN="$f"; break; fi
done
if [ -z "$WIN" ]; then
  echo "== no candidate authenticated — key may be obfuscated/password/fetched;"
  echo "   fall back to the Frida capture route."; rm -f try_key; exit 3
fi
echo "== WORKS: $WIN"
mkdir -p "$(dirname "$DEST")"; cp "$WIN" "$DEST"; chmod 600 "$DEST"; rm -f try_key
echo "== installed to: $DEST"
echo "== export HELIXGEN_HELIX_SSH_KEY=\"$DEST\""
