# helixgen device — feature backlog

Future work for the network device-control feature. Base capability (preset CRUD
+ content read/save + live param edits) shipped in **2.0.0**; IR transfer +
auto-load shipped through **2.5.0**. Ordered loosely.

## Corrected mental models — READ THIS FIRST (2026-07-12)

Recurring places agents (incl. this project's own assistant) got the model wrong
and had to be redirected. Start here so future work begins from the right model.

1. **A `.hsp` *is* a complete device preset — there is no "template."** `.hsp` is
   Line 6's **JSON file format** (`rpshnosj` magic); the device stores/accepts
   presets as **`_sbepgsm` msgpack** (numeric model/param ids, flat block grid,
   `cg__`/`pm__`/`sfg_`). Same preset, two serializations; HX Edit transcodes on
   import. Getting a tone on the device = **transcode `.hsp` → `_sbepgsm` and
   `/SetContentData` it** (see #12). The old `device install` "map blocks onto a
   template preset's slots" was a shortcut for the missing transcoder — it is the
   sole source of the template precondition, coverage failures, and dual-amp
   flattening. Do NOT reason about templates.
2. **The `device` skill is library management, not authoring.** Its job: get a
   `.hsp` on the device + manage it across setlists. It must have **no opinions
   about device contents** (which factory preset to use, coverage buckets, what
   "fits"). Authoring `.hsp` files is the `tone`/`setup` skills' job.
3. **IRs are identified only by hash.** The `.hsp` carries the `irhash`; the
   device references it in content as **`mdls[0].irmd` = the 16-byte hash**
   (`bytes.fromhex(irhash)`); the WAV is uploaded/registered by hash separately.
   File↔hash caching is `mapping.json`'s job — **local only, never inside the
   `.hsp`**. (The pre-2.16 bridge set the cab *model* but never wrote `irmd` — a
   latent bug the transcoder fixes.)
4. **Device ops must NOT change the active tone unless the user asks.** Reading OR
   writing content via the edit buffer (`load_preset` = `/LoadPresetWithCID`)
   makes a preset active. Install via `CreateContent`+`SetContentData` is
   non-activating (use it). `backup`/`pull` still activate → needs a
   non-activating content-read command (RE) or save-and-restore (#13).
5. **Don't source-dive to answer behavior/format questions.** The running MCP is
   the **bundled** plugin (`${CLAUDE_PLUGIN_ROOT}`), NOT the cwd checkout —
   reading source can mislead about the live version/schema. The **tool
   descriptions, CLI `--help`, `device setlist list`, and the sync result dict**
   are the authoritative contract. (See the resolver pattern, #14.)
6. **A tone belongs in as many setlists as you want.** `device setlist add` is
   idempotent within a setlist; it errors ONLY on a name/**different-file**
   collision. Never pre-check membership or read the manifest to add safely.

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

- **Library mirror sync** (`device sync [dir]` / `device_sync_library`,
  destructive in 2.15.0) — **RETIRED 2026-07-12**, superseded by the
  reference-based multi-setlist `device sync <setlist>` (#10). The old path made
  the target setlist match a directory of `.hsp` tones by deleting every preset
  in it and reinstalling the library fresh; the new engine reconciles a preset
  pool + setlist references non-destructively instead. The `device_sync_library`
  MCP tool and the directory-mirror CLI form are gone.
  (was `src/helixgen/device/sync.py`)

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
- **#6 Single-tone install/manifest parity** — **✅ MOSTLY RESOLVED (2.19.0,
  tone-library redesign).** MCP `device_install_preset` now records the
  placement in the tone-library manifest (registers the tone, sets its slot +
  observed device); the `SlotLedger` it used to drift is gone — one manifest is
  the single writer, kept in sync by both CLI and MCP. Remaining open:
  - **MCP `device_install_preset` IR upload** — still uploads no IRs (the CLI
    `device install --auto-irs` and `device sync` do). Fold the shared per-tone
    IR-upload core in.
  - **"Update" an already-installed tone** — still needs its own brainstorm (no
    device-side "update" verb; `device restore` is the closest primitive).
    **Blocked on a design decision.**

### Named-setlist targeting / multi-setlist (device model RE'd 2026-07-12)
**Full findings + design:**
`docs/superpowers/specs/2026-07-12-multisetlist-support-design.md` (the
implemented design; supersedes the earlier
`2026-07-12-helix-content-model-multisetlist-refactor.md` findings/handoff note).
The first 2026-07-12 setlist-sync attempt was **backed out** (built on a wrong
assumption — see #9); the reference-based redesign below then **shipped
2026-07-12**.

- **#8 Create a setlist** **[device-write][discovery]** — **still deferred.**
  helixgen can *resolve* a user setlist by name (`client.resolve_setlist_cid`,
  enumerating `cctp==1001` under -5) but cannot *create* one. The 2002 create
  command is uncaptured (only the 2001 `/addContent` result was seen). Next:
  `tcpdump` port 2002 while the Stadium app creates a setlist. Until then, the
  user creates a new setlist by hand in the Stadium app; `device sync` resolves
  it by name and errors clearly ("create '<name>' in the Stadium app first") when
  it's absent. `device setlist create-local` / `add`'s auto-create only touch the
  local manifest, not the device.
- **#9 Install a preset INTO a setlist** — **✅ IMPLEMENTED (2026-07-12).**
  Confirmed model: `/AddContentsToContainer(setlist,[poolCid],…)` creates a
  **REFERENCE** (`cctp 1003`, `rcid`→pool preset), **not a copy**; deleting the
  referenced pool preset **orphans** the reference (`RemoveContent -21`). Shipped
  as `client.reference_into_setlist` / `remove_reference` / `mirror_setlist`, with
  `install_into_pool` (`/CreateContent` in -2 only) and a `client.mutating()`
  2001-subscription context for prompt propagation. Rolled into #10.
- **#10 Multi-setlist support** — **✅ IMPLEMENTED (2026-07-12, this release).**
  The device model — a **preset pool** in -2 (`cctp 1000`) + named setlists that
  are **reference-lists** (`cctp 1003`) into it, so a tone can be referenced by
  many setlists — is now live. A local manifest `~/.helixgen/setlists.json`
  (override `$HELIXGEN_SETLISTS`, absorbs the old slot ledger) records
  `setlist-name → [tone names]` + a `tones` path map; `device sync <setlist>` /
  `--all [--gc]` reconciles the pool (install/update/skip by content hash) then
  rebuilds each setlist's references in order, **never orphaning** a
  still-referenced preset (GC only on `--all --gc`). CLI `device setlist
  list|add|remove|create-local` + MCP `device_setlist_*` / `device_sync_setlist`
  / `device_sync_all` manage/drive it; the retired directory-mirror `device sync
  [dir]` + `device_sync_library` are removed. Includes the **device-client
  refactor** (container/cctp enums, the `-5`-is-the-root correction, privatized
  raw primitives, model-correct high-level ops, `client.mutating()`, bounded
  auto-reconnect for the flaky network stack). See the design spec.
  - **Follow-up — validate other category unifications.** The install bridge maps
    interchangeable device slot families (`CATEGORY_MAP` in
    `src/helixgen/device/bridge.py`): cab = `{ir, cab, cab_ir_interp}`, amp =
    `{amp, preamp}`, etc. The **cab** unification (an `ir` cab installing onto a
    modeled-cab slot) is what lets a plain factory full-rig template host IR
    tones, and it's **hardware-validated**. The amp/preamp (and eq/filter,
    pitch/synth, volume/pan) unions are mapped but **not yet hardware-confirmed** —
    e.g. verify a helixgen amp installs onto a template `preamp` slot and sounds
    right. Worth a validation pass before relying on them in a sync.

### Quick-win (independent of the redesign)
- ✅ **`device.model` load fix (2026-07-12, shipped 2.16.0)** — the user's
  `preferences.json` had `device.model: "stadium_xl"` (MCP token), which the
  validator **rejected**, so `load_preferences()` threw on the real file.
  `preferences.py::_validate_device_model` now accepts display forms AND MCP
  tokens case/separator-insensitively, normalizing to the display form
  (`stadium_xl` → `Stadium XL`). (`resolve_setlist_cid` + the setlist-name
  resolution shipped with #10.)

### IR maintenance
- **#11 IR cleanup command** **[device-write]** — `helixgen device ir-prune`
  (or similar): delete IRs on the device that no preset references. Diff the
  device's user IRs (`client.list_irs()`, container -11) against the `irhash`es
  referenced by all presets currently on the device (across setlists), and
  remove the orphans (`/RemoveContent` on -11). Dry-run first; confirm; report
  freed slots. Guard against deleting an IR referenced by an off-device preset
  the user still has locally.

### Slot ordering
- **#7 Slot ordering** — **✅ REFRAMED (2.19.0, tone-library redesign).**
  Ordering is now a first-class property of the manifest: within a named setlist,
  membership order == device reference order, edited with `device slots reorder
  <tone> --to N [--setlist S]` and applied by the managed-mirror `device sync`.
  The old destructive `device slots sync` reorg is retired (superseded).
  User-setlist slot order is deliberately unordered (slots are just addresses,
  auto-assigned). No separate skill needed. **Hardware-validate the reorder →
  sync path on an expendable setlist.**

### Device-control breadth
- **#1 Set the currently active tone** **[device-write][discovery]** — `load
  <cid>` fills the edit buffer; confirm whether there's a separate
  active-preset-index command and expose it (`device select <cid>` + MCP). OSC
  command names live in `client.py`; the active-preset verb isn't captured yet.

### `.hsp` → device transcoder (replaces the template bridge)
- **#12 `.hsp` → `_sbepgsm` transcoder** **[in progress 2026-07-12]** — the real
  fix behind mental-model #1: faithfully transcode a `.hsp` into the device's
  stored `_sbepgsm` and `/SetContentData` it into the pool. No template, no
  coverage limits, full fidelity. Design +
  progress: `docs/superpowers/specs/2026-07-12-hsp-to-device-transcoder-design.md`.
  Module `src/helixgen/device/transcode.py` (`sbepgsm_to_recipe` /
  `recipe_to_sbepgsm` / `hsp_to_sbepgsm`), gated by an offline
  `_sbepgsm → recipe → _sbepgsm` byte-fidelity net. Hardware-confirmed so far:
  device tolerates synthesized `tid_`/identity `bmap`; harness (`hrns`) varies by
  block kind (not constant); IR = `mdls[0].irmd` 16-byte hash. Remaining:
  snapshots/controllers (`snps`/`srcs`/`trgs` + per-param `tid_`), dual-amp
  (split/join), wire `install`/`sync` onto it + delete the template/bridge, strip
  templates from the skill, device audio-validate.
- **#13 Non-activating content read** — **✅ SHIPPED 2.18.0.** Captured HX Edit's
  content-read command: `client.get_content(cid)` sends `/GetContentData [reqid,
  cid]` (the non-activating GET counterpart to `/SetContentData`) and returns the
  content blob **without** `load_preset`, so the device's live tone is untouched
  (mental-model #4). `backup`/`pull` now route through it (`device/backup.py`,
  `cli.py` `pull`/`backup`).

### Resolver pattern — single source of truth for agents + skill
- **#14 Implement + maintain a "resolver" pattern** **[infra]** — so future
  developer-agents AND the runtime skill get authoritative answers without
  source-diving or re-deriving (mental-model #5). Two faces of one idea:
  - **Code-level resolvers** (one canonical function/table per mapping, reused
    everywhere — never re-implemented ad hoc): model name↔device id + param
    name↔`pid` (`defs`), setlist name→cid (`resolve_setlist_cid`), `irhash`↔wav
    (`mapping.json`) and `irhash`↔device `irmd`, and `.hsp`↔`_sbepgsm` (the #12
    transcoder). Audit for duplicated/divergent mapping logic and consolidate.
  - **Contract-level resolver for agents:** behavioral/format facts live at the
    point of use — **MCP tool descriptions, CLI `--help`, and the result dicts** —
    and a short authoritative index (this "mental models" block + the protocol
    doc) that a skill/agent consults FIRST. The `device` skill should point at
    the resolver and explicitly forbid source-diving. Goal: an agent can answer
    "how does X work / where does Y live" from the contract, not the source tree.
    Keep it maintained as the code evolves (stale resolver = worse than none).

### Authoring-bridge depth — ✅ SHIPPED 2.18.0 (template-free transcoder synthesis)
- ✅ **Snapshots over the network** — the transcoder synthesizes the 8-snapshot
  scenes (per-snapshot bypass + param deltas) into `cg__.entt`.
- ✅ **Controllers over the network** — footswitch + EXP-pedal assignments
  synthesized (1-based `srcs`/`trgs`/`ctrl`, `scid` keyed by source id; FS
  `locl=24+N` ctxt 1, EXP `locl=42`).
- ✅ **Multi-chain / parallel routing** — dual-DSP AND intra-flow split/join
  synthesized onto the real 28-slot device grid (`bmap[gridpos]=id`; split.bblk
  = first lane-1 slot, join.bblk = 14 + join.pos). Hardware-validated vs HX
  Edit's own import.

### Double-click a `.hsp` to load onto the Helix's ACTIVE slot **[device-write]**
- Requested 2026-07-13. A macOS file-association / tiny app wrapper so
  double-clicking a `.hsp` transcodes it and pushes it onto the Helix's
  **currently-active** edit-buffer/slot — something HX Edit itself can't do.
- Now feasible: the transcoder produces device content; install/`SetContentData`
  works. The one gap is targeting the *active* slot — needs either a
  **set-edit-buffer** write command (RE: capture what HX Edit sends when it
  pushes an edit to the live buffer) or the still-open **active-preset-cid query**
  (backlog #1) so we can `SetContentData` into it + reload. Ship as a
  `helixgen device load-hsp <file>` verb first, then the double-click wrapper.

## Stadium-app parity (2026-07-13)

Full app-function inventory + coverage matrix: **`docs/stadium-app-parity.md`**
(app v1.3.2.9805). These items close the 🔴/🟡/🔍 rows so the desktop app is
never needed. Ranked by impact (how often the app is the *only* way to do it) ×
effort. The complete OSC command namespace + 251 `global.*` keys are already
known from the app binary — most 🔍 items need only a targeted frida capture of
an **argument shape**, run at implementation time.

**⚠️ Coordinate with the in-flight tone-library-model-redesign (PR #31 / 2.19.0).**
It is actively reworking the preset/setlist/library CLI. Parity items that touch
setlist/preset CRUD (#20 create-setlist/#1, setlist rename/delete) must sequence
**after** it merges to avoid collisions. The P1 item below is deliberately
orthogonal to it.

- **P1 · #15 Global settings read/write** — **✅ SHIPPED 2.20.0.** The 8 Global
  Settings pages + Tuner + Wireless (161 curated `global.*` keys) are read/written
  over `/PropertyValueGet` / `/PropertyValueSet` via `helixgen device settings
  list|get|set` + MCP `device_settings_*`. Protocol RE'd + hardware-validated
  (get/set/def, enum-by-label, range validation, round-trip) — see
  `docs/superpowers/specs/2026-07-13-global-settings-re-findings.md`. The device
  self-describes each key (name/type/range/enum) via `/PropertyDefWithKeyGet`, so
  the catalog is live. **Remaining follow-up: Global EQ** (`dsp.globaleq.*` +
  `/GraphEnableSet`) is a separate, non-property screen — its param-write path
  still needs a capture. Matrix §8.
- **P2 · #16 Command Center** **[device-write][discovery]** — the footswitch-
  command subsystem (`commanddefs` families: PresetSnapshot, Song, Looper,
  Utility, ExtAmp, MIDI CC/PC/Note/MMC, HotKey; 2 cmds/switch, 16 instant
  commands, EXP MIDI, per-command channel). Extend the authoring/transcode path
  (`cg__`/command graph) + capture `/ExecuteCommand`/`/CommandTypeSet`. Matrix §6.
- **P3 · #17 Matrix Mixer** **[device-write][discovery]** — per-output-layer
  (1/4"/XLR/Phones) mixing of paths + 8 Song tracks + click + USB/BT/aux, with
  fader/pan/mute/solo and output linking (`/MixerSave`). Matrix §3.
- **P4 · #18 Signal-flow param depth** **[local]** — first-class authoring of
  input params (impedance/pad/trim/gate), output level/pan, split TYPE
  (Y/A-B/Crossover/Dynamic) params, merge mixer params, FX-loop send/return/mix.
  Pure `.hsp` authoring; no device needed. Matrix §3.
- **P5 · #19 Live device ops** **[device-write][discovery]** — live snapshot
  recall/copy (`/ActiveSnapshotIndexSet`/`/CopySnapshot`), live block bypass
  (`/BlockEnableSet`), live model set (`/ModelSet`), tempo (`/SetTempo`,
  `/SetTimeSignature`), tuner engage + readout (2001/2003 stream schema). The
  performance surface. Matrix §5/§9/§10.
- **P6 · #20 IR + library polish** **[device-write]** — IR delete/prune
  (see **#11**), IR rename/folders; setlist rename/delete/duplicate; import/
  export `.hss`/single-file bundles; preset color/notes. **Sequence after
  PR #31.** Also folds in active-preset select (**#1**) and create-setlist
  (**#8**). Matrix §1/§2/§7.
- **P7 · #21 Quick wins** **[device-write]** — `helixgen device info`
  (`/ProductInfoGet`: firmware/model/capabilities); controller depth
  (curve/threshold/MIDI-CC/label/merge/XY assignment). Matrix §6/§12.

**Out of scope** (matrix 🚫, listed for honesty): firmware update, factory
reset, SD format, full-device microSD backup, Showcase multitrack player,
Clones/Proxy captures, block favorites, preset templates, cloud/Remote-Access,
LED control, focus-view/UI cosmetics.

## Notes / principles
- **Local-file-first:** every device-write feature should also work offline
  against local `.sbe`/`.hsp`/`.wav` copies and sync to hardware on demand.
- **Device-write gating:** the auto-mode classifier blocks the agent from writing
  to the device (`no writes without telling me` — brick risk). Hardware
  validation therefore either runs via a user-invoked `!` script or a granted
  Bash permission rule. Reads (list/get_ref/download/watch) are unrestricted.
- The device is at `192.168.4.84` (ignores ICMP ping; ports 22/2001/2002/2003
  open).
