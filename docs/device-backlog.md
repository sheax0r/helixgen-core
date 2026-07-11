# helixgen device — feature backlog

Future work for the network device-control feature (shipped in 2.0.0: preset
CRUD + content read/save + live param edits). Ordered loosely; not yet scheduled.

## In progress (this session)
- **Local backup library** — bulk-pull a setlist to local `.sbe` files + manifest;
  browse/restore offline. (`src/helixgen/device/backup.py`)
- **Live PUB mirror** — subscribe to the 2001/2003 streams for real-time
  param/meter/state events. (`src/helixgen/device/subscribe.py`)
- **`push`/restore** — capture the "set edit buffer" command so a pulled `.sbe`
  can be pushed back / cloned. (needs a live capture)
- **`.hsp` ↔ device authoring bridge** — install a helixgen-authored preset onto
  a device slot (author-by-replay via `/ModelSet` + `/ParamValueSet` +
  `/CreateContent` + `/SavePresetWithCID`, using the bundled model/param defs).

## Requested backlog (2026-07-11)

1. **Set the currently active tone** — select which preset is *active/playing* on
   the device. We already have `device load <cid>` (loads a preset into the edit
   buffer); confirm whether that fully covers "active tone" or whether there's a
   separate active-preset-index command (see `/PresetSnapshot` in the command
   defs). Expose cleanly (`device select <cid>` / MCP).

2. **Load IRs onto the device** — push a local `.wav` IR to the device's IR
   store. On-device IRs live under `/data/stadium-family-fw/ir/` (seen via
   `/xxxIrxPathForHash1`); the transfer is likely SSH/SFTP or a dedicated OSC
   command — needs a capture of the editor's IR-import. Reuse helixgen's existing
   `irhash` (48 kHz Stadium hash, already implemented) to name/identify.

3. **Pull IRs off the device** — download the device's IRs to local `.wav`
   files (inverse of #2); build/refresh the local IR library + `mapping.json`
   from what's actually on the hardware.

4. **Auto-load IRs referenced by a preset** — closing the loop: a wrapper/sidecar
   (e.g. JSON referencing an IR **file** + the tone file) so that when a preset
   uses a local IR, helixgen **checks whether the IR is already on the device**
   (by `irhash` — we can enumerate on-device IRs and compare hashes) and, if not,
   **loads it automatically** (feature #2) before/while installing the preset —
   instead of telling the user "remember to import the IR." This makes the
   `/tone` → playable-on-amp flow fully hands-off, IRs included.
   - Ties into: helixgen `register-irs`/`ir-scan`/`mapping.json`,
     `compute_irhash`, and the authoring bridge.

## Progress on the IR features (2026-07-11)

**Done + shipped-ready:**
- **On-device IR enumeration** — `/GetContainerContents(-11)` returns all 386
  IRs with `{cid_, name, hash (raw 16 bytes -> hex), mono, posi}`. The `hash`
  **is** helixgen's `irhash` (verified: all 386 device hashes match the user's
  `~/.helixgen/irs/mapping.json`, which knows each one's local WAV path).
  Exposed as `client.list_irs()` / `device_ir_hashes()` and CLI `device list-irs`.
- **Preset IR-presence check (feature #4's "check if it's already there")** —
  `bridge.hsp_ir_hashes()` / `check_irs()` split a preset's referenced IRs into
  present vs missing on the device; `device install` warns about missing ones.

**Remaining — IR file transfer (upload #2 / download #3, the "load it for them"
half of #4):** the editor does NOT push IR bytes over the OSC ports — IR browsing
is client-side cached (no OSC traffic), and the device stores IRs as files under
`/data/stadium-family-fw/ir/`. So transfer goes over the device's **credentialed
SSH channel** (libssh2, port 22, encrypted). Options: (a) extract the editor's
SSH credentials via Frida (hook `libssh2_userauth_*`) then SFTP files ourselves —
bounded but invasive/firmware-fragile; (b) find an OSC/drag-drop import command
if one exists. Until then, `device install` *warns* about missing IRs (with the
local path available from `mapping.json`) instead of auto-loading.

## Requested backlog (2026-07-11, cont.)

5. **Cache precalculated IR hashes in the plugin data dir** — computing an IR's
   Stadium `irhash` (libsndfile float round-trip + MD5) is relatively expensive;
   cache `wav-path (+ mtime/size) -> irhash` in the plugin/user data directory so
   reusing the same IR across presets doesn't recompute. Invalidate on
   mtime/size change. Ties into `mapping.json` and the authoring-bridge IR check
   (which needs each referenced IR's hash). Store under the plugin data dir
   (e.g. `~/.helixgen/cache/irhash.json`), keyed by absolute path + stat.

## Notes / dependencies
- #2/#3 hinge on the device's SSH file channel (see above) — the mechanism is now
  known; the blocker is credentials, not discovery.
- #4 depends on #2 (upload), on-device IR enumeration (list IRs + their hashes),
  and the authoring bridge (#0 above) to reference the IR from the pushed preset.
- All device-write features should keep the **local-file-first** principle: work
  offline against local `.sbe`/`.hsp`/`.wav` copies, sync to hardware on demand.
