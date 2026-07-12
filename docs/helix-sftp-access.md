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

## IR registration model (from the device SQLite db, read-only)

The device tracks content in `/data/stadium-family-fw/db/StadiumDataStore.sqlite3`
(root-owned). Relevant tables:

| Table | Cols | Notes |
|-------|------|-------|
| `Content` | `cid, ctype, ccid, position, locked, premium, owner, name, color` | master content table (presets, folders, IRs); backs `/GetContainerContents` |
| `IRContent` | `cid, mono, hash, path` | one row per registered user IR; `cid` == the OSC `cid_` (e.g. 492…) |
| `IrHashToPath` | `hash (16 bytes), path` | IR hash → `/data/…/ir/<name>.wav` |

So a **registered IR** = the `.wav` on disk **plus** rows in `Content` +
`IRContent` + `IrHashToPath`. The db is **root-owned**, so the `hedit` SFTP user
can't write it — the editor does **not** hand-edit the db over SFTP. Instead it
uses device-mediated OSC commands (the device, running as root, updates its own
db). Registration/assignment commands in the editor binary:

- `/UserIRSet` — set/register a user IR (primary registration command).
- `/CreateContent` + `/SetContentPath` + `/SetContentData` — the generic content
  path used for everything (IRs are `ctype`=IR rows).
- `/observeWatchedDirChange`, `/rootdir`, `/imports`, `/currentdir` — the device
  **watches the `ir/` directory**, so a dropped file may be auto-detected.
- Assignment to a cab/IR block: `/setUserIR`, `/setTargetIR`, `/setSnapshotIR`.

### Import flow — CONFIRMED by live capture

A live IR import (drag a WAV onto a cab/IR block) showed the whole thing:

1. Editor **SFTP-uploads** the `.wav` to `/data/stadium-family-fw/ir/<name>.wav`
   (`libssh2_sftp_open_ex` with WRITE|CREAT). That's the only client-side write.
2. The **device auto-registers it**: it watches `ir/`, and on the new file it
   computes the hash, writes the `Content`/`IRContent`/`IrHashToPath` rows
   **itself** (as root), and broadcasts **`/addContent`** on the 2001 PUB stream:
   `[_, _, msgpack{ccid:-11, cctp:1002, cid_:<new>, hash:<16 bytes>, mono:…}]`.
   Verified: after the upload, `GetContentRef(<new cid>)` returns the IR with the
   correct hash/name (`/UserIRSet` was **not** needed for registration — it's for
   block assignment, not import).

**Upshot for a safe upload:** `push-ir` SFTPs the `.wav` into `ir/` and the
device does the registration itself (db writes stay device-side; we never touch
SQLite). The editor didn't upload the `_FULL/_THUMB` PNGs in the capture — the
device appears to generate them (or they're optional). Duplicate hash → the
device dedups (no second registration). This is about as low-risk as a device
write gets, but it *is* a filesystem write — gate it behind explicit
confirmation and test on one throwaway IR first (as we did: `cid 946`,
`YA KW 412 M25 121-2`).

> **Upload the *processed* IR, and write it directly.** The editor uploads the
> Stadium-canonical processed IR (8192-sample, `helixgen.ir.write_stadium_ir`),
> not the raw WAV, and the device's `irhash` is MD5 of that file's data chunk —
> so uploading the raw source registers the wrong hash for any IR longer than
> 8192 samples. Write it **directly** to `ir/<stem>.wav` (a temp+rename lands as
> `IN_MOVED_TO` and doesn't trigger the device's registration; the editor writes
> directly). See finding #3 below for the full mechanism and the
> device-gated-registration caveat.

## Findings from building `push-ir` (device write behavior)

SFTP-uploading a `.wav` into `ir/` works, but registration is not the instant
inotify I first assumed. Confirmed by uploading several IRs:

1. **Registration is real but delayed.** When the *editor* is connected it
   triggers an immediate rescan (via a watched-dir command), so its imports
   register in ~0.15 s. An SFTP-only upload (no editor) still registers, but on
   the device's own slower scan — `GetContentRef(<new cid>)` confirms it later.
2. **The container listing lags.** `/GetContainerContents(-11)` keeps returning
   the old count/set after an upload until something refreshes it; `get_ref` by
   cid sees the new IR immediately. So confirm uploads by cid/name, not by
   re-listing (and don't trust the list's count right after a write).
3. **`push-ir`'d IRs registered under a wrong hash — ACTUAL ROOT CAUSE: the
   editor uploads a *processed* IR, not the raw WAV.** (An earlier hypothesis —
   a partial-write race, "fixed" in 2.5.0 with an atomic upload — was **wrong**;
   see below.)

   Live capture of a real editor IR import (Frida on `_libssh2_channel_write` +
   the OSC streams) showed the editor's complete wire behavior:

   - The editor SFTP-writes a file to `ir/<stem>.wav` that is **24660 bytes /
     8192 frames** — **not** the 72 kB raw source. It is the *processed* IR: the
     source truncated to the Stadium's 8192-sample form + exp fade — exactly the
     pipeline `helixgen.ir.compute_stadium_irhash` runs internally.
   - The device registers it by taking **MD5 of that file's data chunk**, and
     that MD5 **is** helixgen's `irhash` (verified: device's editor-uploaded file
     → `MD5(data) == irhash`, byte-for-byte).
   - `push-ir` was uploading the **raw** WAV. For any IR longer than 8192 samples
     the device then sees different bytes → the wrong hash (and the device's own
     raw-file processing differs slightly from helixgen's, which is why the
     `620d381f`/`44cf68fe` values matched no simple variant). Short IRs (≤8192)
     upload raw == processed, which is why the user's regular IRs never broke.

   **Fix (2.6.0):** `helixgen.ir.write_stadium_ir(src, out)` emits the
   device-canonical processed IR (data-chunk MD5 == `irhash`); `push_ir` uploads
   **that** to `ir/<stem>.wav` via a direct write (mirroring the editor). The
   device then registers the IR under exactly the hash a preset references.

   **Known limitation — registration timing is device-gated.** The editor's
   import registers **instantly** (~0.15 s); an identical external SFTP write of
   the identical processed file does **not** (confirmed against every replicable
   variable: same file bytes, same `INIT/OPEN/WRITE/CLOSE` SFTP protocol, same
   `0744` perms, same key/user/IP, same `libssh2` client banner, persistent vs
   fresh session). The device correlates the write with the editor's own trusted
   control session in a way not reproducible from an external client. External
   uploads therefore register on the device's own (slower, unpredictable) scan,
   or when the user next imports through the editor — but **when they register,
   the hash is now correct**. The 2.5.0 atomic-upload was reverted (a rename
   lands as `IN_MOVED_TO`, which does not trigger registration; the editor writes
   directly).

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
