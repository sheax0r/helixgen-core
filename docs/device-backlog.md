# helixgen device — feature backlog

Future work for the network device-control feature. Base capability (preset CRUD
+ content read/save + live param edits) shipped in **2.0.0**; IR transfer +
auto-load shipped through **2.5.0**. Ordered loosely.

## ✅ Shipped

- **Preset CRUD + content read/save + live param edits** (2.0.0) — `device
  list/read/load/create/save/rename/delete/set-param`.
- **Local backup library** (`device backup` / `local-list`) — bulk-pull a
  setlist to local `.sbe` files + manifest; browse/restore offline.
  (`src/helixgen/device/backup.py`)
- **Live PUB mirror** (`device watch`) — subscribe to the 2001/2003 streams for
  real-time param/meter/state events. (`src/helixgen/device/subscribe.py`)
- **`push` / `restore`** — install a pulled `.sbe` back into a slot / overwrite
  an existing preset's content.
- **`.hsp` → device authoring bridge** (`device install`) — map a
  helixgen-authored `.hsp`'s blocks onto a device template's same-category slots
  and install a playable preset. **Scope today: single serial chain, base param
  values only** (no snapshots / controllers / parallel — see Remaining).
- **On-device IR enumeration** (`device list-irs`) — `/GetContainerContents(-11)`
  → every user IR's `{cid_, name, hash, mono, posi}`; the `hash` **is** helixgen's
  `irhash`. `client.list_irs()` / `device_ir_hashes()`.
- **#2 Load IRs onto the device** (`device push-ir`, 2.4.0) — SFTP the `.wav`
  into `ir/`; the device auto-registers it. **Made reliable in 2.5.0** via atomic
  stage→rename upload (a plain streaming put let the device hash a half-written
  file — see `helix-sftp-access.md`).
- **#3 Pull IRs off the device** (`device pull-ir`, 2.4.0) — download an IR by
  on-device filename.
- **#4 Auto-load IRs referenced by a preset** (`device install --auto-irs`,
  2.5.0) — diff a preset's referenced `irhash`es against `device_ir_hashes()`,
  resolve each missing one to a local WAV via `mapping.json`, `push-ir` it, and
  **verify** the device registered it under the expected hash (warns if not).
  Closes the `/tone` → playable-on-amp loop for IRs.

## 🔲 Remaining

Legend: **[local]** = pure local code, no device needed. **[device-write]** =
implementation is code, but *hardware validation* requires a device write
(gated by the auto-mode classifier — run via `!` or grant a Bash permission
rule). **[discovery]** = also needs an OSC command we haven't captured yet.

### IR — trigger prompt registration (NEXT)
- **★ Fix the IR-registration delay** **[device-write][discovery]** — **the next
  thing to tackle.** As of 2.6.0, `push-ir` uploads the correct *processed* IR
  and the device DOES register it (confirmed: 10 external uploads all eventually
  registered as cids), but only on the device's **own slow, periodic scan** —
  the editor's own import registers **instantly** (~0.15 s). We want that
  instant path.
  - **Hypothesis (Mike): the editor triggers a sync/rescan somehow after its
    write.** Worth pinning down. What we've RULED OUT as the trigger (all
    identical between editor and an external upload): file bytes, the
    `INIT/OPEN/WRITE/CLOSE` SFTP protocol (captured via Frida on internal
    `_libssh2_channel_write`), `0744` perms, key/user/IP, the `libssh2` client
    banner, and persistent-vs-fresh session. The only OSC the editor sends near
    an import is `/IrPathForHashGet` (a pure hash lookup — proven NOT to trigger
    registration).
  - **Where to dig next:** (a) hook the editor's *own* app-level functions
    around the import (not just libssh2/OSC) — Ghidra/Frida on symbols matching
    `import`/`sync`/`register`/`ir` — to see what it calls after the SFTP close;
    (b) watch the 2001/2003 PUB streams during an editor import for a device→
    editor event that reveals a device-side sync path; (c) probe whether the
    device runs a periodic scanner (time the delay across several uploads) and
    whether any safe OSC verb (e.g. a container-refresh / watched-dir command)
    forces it. Needs the Frida-attachable debug editor build.
  - Until solved: `push-ir`/`--auto-irs` upload correctly; registration
    completes on the device's scan or the next editor import (hash is correct
    whenever it lands). Documented in `helix-sftp-access.md` finding #3.

### IR polish
- **#5 IR hash cache** **[local]** — cache `abspath (+ mtime/size) → irhash` in
  `~/.helixgen/cache/irhash.json` so reusing an IR across presets doesn't
  recompute the libsndfile round-trip + MD5. Invalidate on stat change. Ties into
  `mapping.json`, `compute_irhash`, and the bridge IR check. **No blocker.**

### Device-control breadth
- **#1 Set the currently active tone** **[device-write][discovery]** — `load
  <cid>` fills the edit buffer; confirm whether there's a separate
  active-preset-index command and expose it (`device select <cid>` + MCP). OSC
  command names live in `client.py`; the active-preset verb isn't captured yet.

### Authoring-bridge depth (bridge is single serial chain / base params today)
- **Snapshots over the network** **[device-write]** — push the 8-snapshot scenes
  (per-snapshot bypass + param overrides) so an installed preset carries its
  verse/chorus/lead scenes, not just the base state.
- **Controllers over the network** **[device-write]** — push footswitch and
  EXP-pedal assignments so the installed preset is stomp-ready without on-device
  wiring.
- **Multi-chain / parallel routing** **[device-write]** — the bridge maps one
  serial chain; add parallel A/B splits + the second DSP path.

## Notes / principles
- **Local-file-first:** every device-write feature should also work offline
  against local `.sbe`/`.hsp`/`.wav` copies and sync to hardware on demand.
- **Device-write gating:** the auto-mode classifier blocks the agent from writing
  to the device (`no writes without telling me` — brick risk). Hardware
  validation therefore either runs via a user-invoked `!` script or a granted
  Bash permission rule. Reads (list/get_ref/download/watch) are unrestricted.
- The device is at `192.168.4.84` (ignores ICMP ping; ports 22/2001/2002/2003
  open).
