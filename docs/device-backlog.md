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

- **Library mirror sync** (`device sync` / `device_sync_library`, **destructive
  in 2.15.0**) — makes the target setlist (default `user`) match a directory of
  authored `.hsp` tones exactly: deletes every preset already in the setlist,
  then installs the library fresh (arbitrary order), uploads referenced IRs, and
  replaces the setlist's ledger entries. Only that setlist is touched; no backup;
  an empty/all-unreadable library deletes nothing (guardrail).
  (`src/helixgen/device/sync.py`)

## 🔲 Remaining

Legend: **[local]** = pure local code, no device needed. **[device-write]** =
implementation is code, but *hardware validation* requires a device write
(gated by the auto-mode classifier — run via `!` or grant a Bash permission
rule). **[discovery]** = also needs an OSC command we haven't captured yet.

### IR — prompt registration (FIXED, 2.7.0)
- **★ IR-registration delay — FIXED.** External uploads now register **instantly
  and under helixgen's `irhash`**, exactly like the editor. Two device
  behaviours, both reverse-engineered:
  1. **Instant = a 2001 subscription.** The device only runs its IR-dir watcher
     while a client is subscribed to the 2001 change stream; `push_ir` opens a
     `HelixSubscriber` on 2001 first → the write registers in ~0.1 s (vs the
     ~15-20 min periodic scan). Every "device treats the editor specially"
     dead-end was really "our tests only used the 2002 RPC socket."
  2. **Correct hash = a `HASH` chunk.** On the watched-dir path the device
     computes its own IR hash *unless* the WAV carries a `HASH` chunk (32 ASCII
     bytes = hex `irhash`), which the editor writes and the device trusts.
     `write_stadium_ir` now embeds it (file layout `fmt `/`HASH`/`data`, matching
     the editor byte-for-byte).
  - Hardware-verified across multiple files: `push_ir` → `registered=True`,
    `device_hash == helixgen irhash`, ~instant. See `helix-sftp-access.md`
    finding #3.

### IR polish
- **#5 IR hash cache** **[local]** — cache `abspath (+ mtime/size) → irhash` in
  `~/.helixgen/cache/irhash.json` so reusing an IR across presets doesn't
  recompute the libsndfile round-trip + MD5. Invalidate on stat change. Ties into
  `mapping.json`, `compute_irhash`, and the bridge IR check. **No blocker.**

### Single-tone install/remove parity with bulk sync
- **#6 Single-tone IR-upload + ledger parity** **[device-write]** — bring the
  *single-tone* paths up to the same behaviour as bulk `device_sync_library`,
  which already uploads referenced IRs (via `push_ir`) and records the slot
  ledger. Gaps today:
  - **MCP `device_install_preset`** installs the recipe but uploads **no IRs**
    and records **no ledger** entry (see `mcp_server/tools.py`
    `device_install_preset_handler`). It should: diff the preset's referenced
    `irhash`es, `push_ir` any missing (unless an `exclude_irs`/`auto_irs`
    opt-out), and `ledger.record(...)` the placement — mirroring the per-tone
    loop in `helixgen.device.sync.sync_library`. The CLI `device install
    --auto-irs` already does both; MCP should reach the same behaviour (ideally
    by extracting the sync per-tone core into a shared helper both call).
  - **MCP `device_delete_preset`** should drop the deleted preset from the
    ledger (the CLI `device delete` already calls `_ledger_remove`; MCP does
    not — see the `device_delete_preset_handler`).
  - **"Update" an already-installed tone** — decide + implement the semantics
    (re-author over the existing slot vs. push local `.hsp` edits to the device
    preset), uploading any newly-referenced IRs and updating (not duplicating)
    the ledger entry. Needs its own brainstorm — no device-side "update" verb
    exists yet; `device restore` (overwrite content from a file) is the closest
    primitive. **Blocked on a design decision.**
  - Rationale: the single-tone verbs are the ones an agent reaches for when
    installing/replacing *one* tone; today they silently skip IRs (cabs won't
    resolve) and drift the ledger. Requested 2026-07-12.

### Slot ordering as its own skill
- **#7 Explicit reordering skill + tools** **[device-write]** — the `device` skill
  deliberately does **not** order slots: `device sync` installs tones in arbitrary
  fill-empty order and records where each landed in the ledger. Ordering is a
  separate concern — give it a dedicated skill (and firm up the
  `device slots reorder` / `device slots sync` tools, which exist but whose
  destructive reorg is not yet hardware-validated) so a user can impose and
  reconcile a desired slot order as an explicit, opt-in step. Keep it out of the
  install path. Requested 2026-07-12.

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
