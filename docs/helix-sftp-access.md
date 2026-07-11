# Helix Stadium editor — bundled SFTP credentials (device file transfer)

How the Line 6 **Helix Stadium** editor moves *files* (impulse responses, song
archives) to/from the hardware, and where its SFTP identity lives. This is the
transfer channel behind IR import/export — distinct from the OSC-over-ZeroMQ
control protocol in [`helix-protocol.md`](helix-protocol.md).

> The editor ships a **private SSH key** in the app bundle — the credential it
> uses to reach the hardware over the LAN. It's the **same key in every copy of
> the app**. This document records where to find it so you can verify it in your
> own copy; it does **not** reproduce the private key material — don't paste a
> credential around. **Writes are the hazard**: pushing/moving/deleting files
> under `/data/…` can brick the unit, so keep to **read-only** (listing,
> downloading) unless you really know what you're doing.

## Where to find it yourself (macOS)

Inside the app bundle, right-click **Helix Stadium.app → Show Package Contents**,
then navigate to `Contents/Resources/sshKeys/`:

```
/Applications/Line6/Helix Stadium.app/Contents/Resources/sshKeys/
├── id_hedit            # editor's SFTP PRIVATE key (RSA 3072)   ← credential
├── id_hedit.pub        # matching public key
├── setup_sftp_key.sh   # script to add the key to a target's authorized_keys
└── SFTP_SETUP.md       # Line 6's internal dev setup guide
```

- **Present in the shipping release build**, not just internal/debug copies
  (verified in `/Applications/Line6/Helix Stadium.app`, and in a debugger-enabled
  copy). `SFTP_SETUP.md` is Line 6's internal developer guide (it references their
  dev VMs and usernames).
- Public-key fingerprint (safe to cite):
  `SHA256:nsoXOr2+xP1CptdRXv2mRq5a1bQ+Yd/W0Ah4DQV9cY8` (RSA 3072, `id_hedit.pub`).

## Version this was observed in

| Field | Value |
|-------|-------|
| App | Helix Stadium (macOS) |
| `CFBundleShortVersionString` | **1.3.2** |
| `CFBundleVersion` | 1.3.2.**9805** |
| Bundle id | `com.line6.p35edit` |

(Check your own build: `defaults read "/Applications/Line6/Helix Stadium.app/Contents/Info.plist" CFBundleShortVersionString`. Later builds may rotate the key or remove the bundle — re-verify.)

## The scheme (from the bundled `SFTP_SETUP.md`)

`SFTP_SETUP.md` is Line 6's own developer guide (it references internal dev VMs
like `rkylberg@10.211.55.3` and Xcode schemes). Key facts it documents:

- **User:** `hedit` — env override `P35_SSH_USERNAME` (default `"hedit"`).
- **Auth:** public-key; the app authenticates with the bundled `id_hedit` private
  key. The device carries the matching public key in the `hedit` user's
  `~/.ssh/authorized_keys` (provisioned at the factory).
- **Remote root:** `data/stadium-family-fw` — env override `P35_SFTP_REMOTE_ROOT`.
- **Layout under the root** (observed / documented):
  - `ir/` — impulse-response `.wav` files (matches the on-device IR paths seen in
    the OSC `/xxxIrxPathForHash1` replies, e.g. `/data/stadium-family-fw/ir/YA … .wav`).
  - `songs/archives/` — song-file archives (the guide's example).
- **Transport:** SSH/SFTP on the device's port 22 (`OpenSSH_9.6`, `libssh2` on the
  editor side).

## Device filesystem layout (observed, read-only)

SFTP as `hedit` lands at `/` (the account is chroot/SFTP-only — no shell). Under
`/data/stadium-family-fw/`:

```
bluetooth/  db/  ir/  presetclip/  proxy/  showcase/  songs/  tmp/  user_data/
```

`ir/` holds, per impulse response, three files:

```
-rwxr--r--  2000 3000  24660  YA DXVB 112 121-1.wav        # the 48 kHz IR
-rw-rw-r--  2001 3000  23038  YA DXVB 112 121-1_FULL.png   # full waveform image
-rw-rw-r--  2001 3000   5730  YA DXVB 112 121-1_THUMB.png  # thumbnail
```

Notes:
- The on-disk **filenames** (e.g. `… 121-1.wav`) are *not* the same as the IR
  **display names** in the OSC user-IR list (e.g. `YA DXVB 112 Mix 01`); the
  `irhash` is the reliable join key.
- The `ir/` directory is a **superset** of the OSC user-IR list — a downloaded
  file's `irhash` may not be in `/GetContainerContents(-11)` (that list tracks
  the registered user IRs, via `db/`). So a naive `sftp put` of a `.wav` would
  **not** register the IR in the device's list/db — a real import needs the file
  drop **and** whatever registration the editor does (a strong reason to gate any
  write behind a verified procedure).
- Read-only download works (validated: `sftp get` of a `.wav`, then
  `helixgen.ir.compute_stadium_irhash` on it).

## How this maps to helixgen

- The device's IR list (OSC `/GetContainerContents(-11)`) already gives each IR's
  **name + hash**, and that hash **is** helixgen's `irhash`. So *awareness*
  (list-irs, "is this IR already loaded?") needs no SSH at all — shipped in 2.3.0.
- **IR upload/download** (backlog #2/#3, the "load it for the user" half of #4)
  would use this SFTP channel: `hedit` + `id_hedit` → read/write
  `data/stadium-family-fw/ir/`. Downloading/listing is safe; **uploading writes to
  the firmware filesystem and is the brick-risk path** — gate every write behind
  explicit user confirmation and a tested procedure.

## Read-only usage (validation)

```bash
KEY="/Applications/Line6/Helix Stadium.app/Contents/Resources/sshKeys/id_hedit"
# ssh may require the key be private (chmod 600 a copy):
cp "$KEY" /tmp/id_hedit && chmod 600 /tmp/id_hedit
ssh -i /tmp/id_hedit -o StrictHostKeyChecking=no hedit@<device-ip> \
    'ls -la /data/stadium-family-fw/ir/ | head'
```

Read-only (`ls`, `sftp get`) can't brick the unit. Do **not** write, move, or
delete files under `/data/…` without a verified, reversible procedure.
