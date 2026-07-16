# helixgen — project backlog

The single backlog for all deferred helixgen work (renamed from
`device-backlog.md` 2026-07-13 — item numbering unchanged). Most entries are
device/network work: base capability (preset CRUD + content read/save + live
param edits) shipped in **2.0.0**; IR transfer + auto-load shipped through
**2.5.0**. Ordered loosely.

## What "open" means after the 2026-07-15 drain

Every remaining open entry below is **gated on user input** — a design decision,
a brainstorm, physical access (front panel / screen unlock / MIDI gear), or a
prioritization call on an optional feature. There is no deferred-but-doable
work parked here; that class was drained by the 2.26.0/2.27.0 sweeps.

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
   writing content via the **edit buffer** (`load_preset` = `/LoadPresetWithCID`)
   makes a preset active. Install via `CreateContent`+`SetContentData` is
   non-activating (use it). The **non-activating content READ** (`get_content` =
   `/GetContentData`) shipped 2.18.0 (#13) — so `backup`/`pull`, `ir-prune`, and
   `set-info` read/round-trip content **without** activating any preset. The one
   remaining activating path is a deliberate `load_preset` (or the live-ops verbs,
   which change the ACTIVE tone by design).
5. **Don't source-dive to answer behavior/format questions.** The installed
   `helixgen` may be a **pinned release**, NOT the cwd checkout — reading
   source can mislead about the live version/schema. The **CLI `--help`
   text** (the agent contract since the 0.20.0 MCP removal),
   [`docs/CLI.md`](CLI.md), `device setlist list`, and the sync result dict
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
- **#5 IR hash cache** — **✅ SHIPPED (`helixgen ir-cache`).** Caches
  `abspath (+ mtime/size) → irhash` in `~/.helixgen/cache/irhash.json`
  (override `$HELIXGEN_IRHASH_CACHE`, or `$HELIXGEN_CACHE` for the dir) so
  reusing an IR across presets skips the libsndfile round-trip + MD5;
  invalidated on any stat (mtime/size) change. Shared transparently by
  `register-irs`, `ir-scan`, and the MCP IR tools; inspected/maintained via
  `helixgen ir-cache --stats | --clear | --prune` (`src/helixgen/irhash_cache.py`).

### Single-tone install/remove parity with bulk sync
- **#6 Single-tone install/manifest parity** — **✅ MOSTLY RESOLVED (2.19.0,
  tone-library redesign).** MCP `device_install_preset` now records the
  placement in the tone-library manifest (registers the tone, sets its slot +
  observed device); the `SlotLedger` it used to drift is gone — one manifest is
  the single writer, kept in sync by both CLI and MCP. Remaining open:
  - **MCP `device_install_preset` IR upload** — **✅ SHIPPED (2026-07-15).**
    The shared per-tone IR-upload core (diff referenced irhashes vs
    `device_ir_hashes()`, resolve via `mapping.json`, `push_ir`, verify the
    registered hash) now lives in one place —
    `helixgen.device.ir_upload.upload_missing_irs`/`sync_preset_irs` — and
    backs all three call sites: CLI `device install --auto-irs` / `device
    slots restore` (`cli._auto_upload_irs`, now a thin echo wrapper — still
    aborts the whole install on a hard `push_ir` failure, matching its old
    behavior, just via a clean `ClickException` instead of a raw traceback),
    `device sync` (`setlist_sync._upload_missing_irs`, now a thin wrapper —
    incidentally fixes a latent bug where a hash-mismatch upload was
    misreported `ok: True`), and MCP `device_install_preset`, which gained an
    `auto_irs` param (default `True`) and returns per-IR results in the
    tool's `irs` field.
  - **"Update" an already-installed tone** — needs user input: design
    decision (no device-side "update" verb; `device restore` is the closest
    primitive).

### Named-setlist targeting / multi-setlist (device model RE'd 2026-07-12)
**Full findings + design:**
`docs/superpowers/specs/2026-07-12-multisetlist-support-design.md` (the
implemented design; supersedes the earlier
`2026-07-12-helix-content-model-multisetlist-refactor.md` findings/handoff note).
The first 2026-07-12 setlist-sync attempt was **backed out** (built on a wrong
assumption — see #9); the reference-based redesign below then **shipped
2026-07-12**.

- **#8 Create a setlist** — **✅ SHIPPED (2026-07-14, IR + library polish).**
  No capture was needed: a container item's `type` metadata field carries the
  `/CreateContent` `ctype` it was made with (preset=2, setlist=**1003**), and
  `/CreateContent(-5, pos, 1003, {name})` creates a working setlist —
  live-verified on the XL (accepts references, renames, deletes like an
  app-created one). Shipped as `helixgen device setlist create` (+ MCP
  `device_setlist_create`); `device setlist duplicate` auto-creates its
  target; `device sync`'s missing-setlist error now points at the verb
  instead of the Stadium app. Design spec:
  `docs/superpowers/specs/2026-07-14-ir-library-polish-design.md`.
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
- **#11 IR cleanup command** — **✅ SHIPPED (2026-07-14, IR + library polish).**
  `helixgen device ir-prune` (+ MCP `device_ir_prune`): diffs the device's
  user IRs against the `irmd` hashes referenced by every pool preset
  (non-activating `get_content` scan — fails closed if any read fails) AND by
  local tone-library `.hsp` files. **Dry-run by default**; `--yes` executes;
  locally-referenced "protected" IRs need `--force`; `--only` narrows to one
  IR. Plus `device delete-ir` / `device rename-ir` (name-or-hash). Delete is
  *complete*: `/RemoveContent(-11)` + best-effort SFTP removal of the backing
  `.wav` (the device only GCs the file lazily, which false-positives a quick
  re-import — see `helix-protocol.md`). HW-validated on the XL with a
  synthesized junk IR. (`src/helixgen/device/maintenance.py`)

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
- **#1 Set the currently active tone** — **✅ RESOLVED 2026-07-14 (no new verb
  needed).** The 2026-07-14 parity capture proved the app's "make active" click
  is `/LoadPresetWithCID` (load-by-CID) — the *same* command as `device load`;
  there is **no** separate active-preset-index command for presets (only Songs
  have `/setActiveSongRef`). Single-click select is just a `/GetContentRef`
  metadata read. So `device load <cid>` already IS "set the active tone."

### `.hsp` → device transcoder (replaces the template bridge)
- **#12 `.hsp` → `_sbepgsm` transcoder** — **✅ SHIPPED (2.17.0–2.21.1; entry
  was stale, flipped 2026-07-15).** The template-free transcoder
  (`src/helixgen/device/transcode.py`) is live and wired into `device
  install`/`sync`; the template bridge is deleted and the skill de-templated.
  Everything this entry listed as "Remaining" has since shipped and been
  hardware-validated: snapshots/controllers synthesis (2.18.0; snapshot-bypass
  semantics corrected 2.21.1, PR #36), dual-DSP + intra-flow split/join onto
  the real 28-slot grid (byte-for-byte vs HX Edit's own import), and the
  offline `_sbepgsm → recipe → _sbepgsm` byte-fidelity net plus live
  install→pull round-trips. Later extensions: MIDI CC ctrl records (#33) and
  Command Center `cg__.entt` records (#16), both 2.26.0. Design:
  `docs/superpowers/specs/2026-07-12-hsp-to-device-transcoder-design.md`.
- **#13 Non-activating content read** — **✅ SHIPPED 2.18.0.** Captured HX Edit's
  content-read command: `client.get_content(cid)` sends `/GetContentData [reqid,
  cid]` (the non-activating GET counterpart to `/SetContentData`) and returns the
  content blob **without** `load_preset`, so the device's live tone is untouched
  (mental-model #4). `backup`/`pull` now route through it (`device/backup.py`,
  `cli.py` `pull`/`backup`).

### Resolver pattern — single source of truth for agents + skill
- **#14 Implement + maintain a "resolver" pattern** **[infra]** — **✅ SHIPPED
  WITH RESIDUALS (2026-07-15).** Audited the whole `src/` + `mcp_server/` tree
  for duplicated/divergent mapping logic and consolidated the safe cases;
  behavior-preserving (existing tests + new seam tests all green). Residuals
  (semantic-difference reconciliations too risky for a behavior-preserving pass)
  filed as #51–#53 for the #28 refactor pass.

  **Code-level audit table** (mapping → canonical home → duplications → action):

  | Mapping | Canonical home | Duplications found | Action |
  |---|---|---|---|
  | model name↔numeric id | `defs.model_id_for` / `model_name_for` | none (call sites clean) | already clean |
  | helixgen model str→device id | `modelmap.device_model_id` (+`bridge._default_resolve_model` fallback) | none | already clean |
  | param name→pid | `defs.param_id_for` | `transcode._param_pid` verbatim reimpl (3 sites) | **consolidated** (deleted `_param_pid`) |
  | raw `model_params` table | **new** `defs.model_params_for` | 4 ad-hoc `load_defs()["model_params"]` reads (transcode `_pid_name_maps`/`_synth_parm`, bridge `param_name_map`) | **consolidated** |
  | model→category | **new** `defs.category_for` | `transcode._category_for` + `bridge.device_category` copies | **consolidated** |
  | input-endpoint model ids | derived via `defs.model_id_for` | hardcoded numeric ids in `transcode._INPUT_MODEL` | **consolidated** |
  | `irhash`↔device `irmd` | **new** `device/irmd.py` `{irhash_to_irmd, irmd_to_irhash}` | inlined `bytes.fromhex`/`.hex()` at 5 pure sites (transcode×2, maintenance, client, sftp) | **consolidated** (bytes branches) |
  | `irhash`↔wav | `ir.IrMapping` (`mapping.json`) | none — every `mapping.json` load goes through `IrMapping.load` | already clean |
  | setlist name→cid | `client.resolve_setlist_cid` (#39) | none except the reorder clash branch (→ #52) | already clean |
  | setlist keyword→container | **new** `client.container_for_setlist_keyword` | `cli._setlist_container` + `tools._device_container` cloned dicts | **consolidated** (each wraps it) |
  | container cids (-1/-2/-5/-11) | `Container` IntEnum | none (bare numbers are docstrings only) | already clean |
  | posi→"1A".."8D" label | `client.slot_label` | divergent 2nd formula in `manifest._posi_to_slot` (→ #51) | filed |
  | `.hsp`↔`_sbepgsm` | `device/transcode.py` | single implementation | already clean |

  **Contract-level:** verified + fixed the "Corrected mental models" block
  (#4 was stale — the non-activating read #13 shipped and `backup`/`pull` no
  longer activate); confirmed `docs/helix-protocol.md` current through
  2026-07-15; spot-checked 12 MCP tool descriptions against behavior (all
  accurate — no drift); strengthened the `device` skill with a "Where the
  answers live (consult these FIRST)" resolver index (tool descriptions →
  result dicts → `docs/CLI.md`/mental-models → protocol doc) and reinforced the
  no-source-diving rule. Goal met: an agent answers "how does X work / where
  does Y live" from the contract, not the source tree. **Keep it maintained as
  the code evolves (stale resolver = worse than none).**

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

### Transcoder snapshot residuals (from the 2.21.1 bypass-semantics fix review)

- **#23 Input-block state is dropped on transcode** — **✅ SHIPPED
  (2026-07-14).** `bridge.hsp_to_paths` now captures `b00`'s base `@enabled`
  (→ input endpoint `enbl=0` when bypassed) AND its per-snapshot bypass array;
  the transcoder registers the input endpoint's instance id (`(pi,-1,-1)`
  sentinel), emits a bypass `trgs` target when the array varies, and binds the
  input block (`snap=True, tid_`) — so an input muted per-snapshot / bypassed
  at load survives `device install`/`sync`. Offline-tested in
  `tests/test_transcode.py`. **HW caveat:** a snapshot-varying **DSP-A** input
  round-trip is not hardware-validated — its endpoint sits at instance id 0,
  so its bypass trg carries `eID_=0`, and the device treats id 0 as
  null/unassigned in the *trg-id* space (block-id space is separate, so this
  is presumed fine, but unproven). DSP-B (`eID_=28`, the case the Stadium app
  itself produces) is covered by an offline test.
- **#24 EXP-driven param leaves don't carry their target id** — **✅ SHIPPED
  (2026-07-14).** A controller-ONLY param leaf (EXP sweep AND footswitch
  param toggle) now carries `tid_=<trg id>` with `snap=False`, matching the
  `preset_15x` device blobs; a param that is ALSO snapshot-tracked keeps
  `snap=True` (snapshot binding wins). Fixed both cases together (the #21
  controller-depth pass had kept the old `tid_=0`).
  `tests/test_transcode.py`.

- **#25 `device slots restore --force` doesn't force for `.hsp` sources** —
  **✅ SHIPPED (2026-07-14).** `_install_hsp_open` takes a `force` flag (restore
  passes `--force`) so an occupied slot can be overwritten for `.hsp` sources,
  not just `.sbe`; and restore's slot resolution falls back to the last
  observed `device.posi` when the manifest `slot` doesn't resolve (so a synced
  tone no longer reports "no recorded slot"). Remaining note: `device sync`
  change detection hashes the `.hsp`, so a transcoder fix never re-pushes
  already-synced tones. **✅ SHIPPED (2026-07-15).** Added `--repush` to
  `device sync <setlist>` / `device sync --all` (+ `repush: bool` on MCP
  `device_sync_setlist` / `device_sync_all`) — an explicit flag that forces
  every in-scope pool-present tone into the update bucket regardless of hash
  agreement; the content refresh reuses the existing `SetContentData`-on-the-
  existing-cid path (`plan_pool(..., force=True)` in `setlist_sync.py`), never
  delete+recreate and never `/CreateContent` (unreliable per #38). A persisted
  transcode-hash scheme (auto-detecting a transcoder-version bump) was
  considered and rejected for now in favor of the simpler explicit flag.

### Double-click a `.hsp` to load onto the Helix's ACTIVE slot **[device-write]** — **needs user input: capture session** (unlock the screen + grant macOS Accessibility to the harness so the app's native File menu responds; the set-edit-buffer command must be captured from the app)
- Requested 2026-07-13. A macOS file-association / tiny app wrapper so
  double-clicking a `.hsp` transcodes it and pushes it onto the Helix's
  **currently-active** edit-buffer/slot — something HX Edit itself can't do.
- Now feasible: the transcoder produces device content; install/`SetContentData`
  works. The one gap is targeting the *active* slot — needs either a
  **set-edit-buffer** write command (RE: capture what HX Edit sends when it
  pushes an edit to the live buffer) or the still-open **active-preset-cid query**
  (backlog #1) so we can `SetContentData` into it + reload. Ship as a
  `helixgen device load-hsp <file>` verb first, then the double-click wrapper.

### Guitar profiles + per-guitar tone generation **[local][skill]**
- **#22 Guitar-aware tone authoring** — **needs user input: brainstorm + design spec** (profiles schema, per-guitar behavior). Requested 2026-07-13. Make the user's
  guitars first-class in the library, each with a **profile** of what that
  guitar is "for" (pickups, tonal character, typical roles — e.g. "Les Paul Jr:
  P-90 grind, raw rock rhythm" vs "Strandberg: modern fusion lead"). Builds on
  the existing `instruments` / `default_guitar` keys in `preferences.json`.
  When generating a tone, the `tone` skill can then *optionally* offer to:
  - **create a tone per guitar** — one `.hsp` variant per (selected) guitar,
    following the existing `"<Tone Name> — <Guitar>"` naming convention; or
  - **create per-guitar snapshots** within one preset — distinct snapshots
    named per guitar/role, e.g. "Ibanez — Lead", "Strandberg — Lead" (mind the
    8-snapshot ceiling when combined with per-part snapshots).
  The skill should guide the choice with **easy structured questions**
  (AskUserQuestion-style multiple choice: per-guitar tones / per-guitar
  snapshots / single generic tone), defaulting sensibly (single guitar →
  don't ask). Profiles inform *how* params differ per guitar (e.g. brighter
  amp for humbuckers, tamer top for P-90s), not just naming.
  **Update 2026-07-13 (user re-request, folded in rather than duplicated):**
  the profiles live in **the library** as per-guitar JSON metadata (same
  metadata home/format as #35 tone metadata and #36 IR metadata) describing
  what each guitar *sounds like* — pickups, construction, tonal character —
  so tone generation can read the profile and adjust accordingly when
  targeting a specific guitar. The profile also documents the guitar's
  **control inventory** — what its knobs and switches *are* and do (e.g.
  "3-way toggle: neck / both / bridge; volume + tone per pickup; push-pull
  coil split on the tone pot") — so a tone's guitar-settings metadata (#35)
  can reference real, named controls instead of guesses. The `tone` skill must know how to use these
  profiles (read → adapt params). The per-guitar-variant storage question
  (variant `.hsp`s vs replicated snapshots, default per-variant presets) is
  now formalized in **#35** part 3 — implement the two together.

## Stadium-app parity (2026-07-13)

> **Capture plan (2026-07-14):** the argument shapes / stream schemas for the capture-gated parity items (#1, #16, #17, #19, #31, #33, #34, Global EQ) are enumerated in `docs/superpowers/specs/2026-07-14-parity-capture-plan.md`, driven by `tools/re_capture_parity.py`. Run that session, then decode into `docs/captures/` before implementing any of these.


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
  the catalog is live. **Global EQ follow-up: ✅ SHIPPED 2026-07-14** — it IS
  property-based (corrects the old "non-property screen" note): `device globaleq
  list|set` (+ MCP `device_globaleq_*`) writes `dsp.globaleq.<out>.<band>.<param>`
  via `/PropertyValueSet` with a variant `{parm,valu}` blob. Byte-exact codec
  (golden-tested) + HW-validated (all 3 outputs × 7 bands, `/success`). Write-only
  over the network (no `/PropertyValueGet` read-back → no `get`). Findings:
  `docs/superpowers/specs/2026-07-14-parity-capture-findings.md` §2. Matrix §8.
- **P2 · #16 Command Center** **[device-write] — ✅ SHIPPED 2026-07-14
  (EXPERIMENTAL, native+transcode path).** The footswitch/Instant command
  subsystem. **Route: NATIVE `.hsp`** — corpus recon found `preset.commands` in
  real exports (`Mandarin Fuzz` = PresetSnapshot on FS1, `Epic Lots of EQ` =
  MIDI CC/PC on Instant 1/2), so commands are authored NATIVELY (no sidecar,
  unlike #33). Shipped: a top-level recipe `commands` list (families midi_cc /
  midi_pc / midi_note / midi_mmc / snapshot on `FS1`–`FS5`/`FS7`–`FS11`
  + `Instant1`–`Instant6`, ≤2 per switch) → `mutate.wire_command` →
  `preset.commands`; `view`
  lift-back; commands survive block edits (switch-keyed, not block-keyed);
  transcoder synthesizes `cg__.entt` `srcs`→`cmnd`→`trgs`. CLI/MCP/CLAUDE.md/
  tone-skill in sync. Design + decoded ground truth: `docs/superpowers/specs/
  2026-07-14-command-center-design.md`.
  **Decoding note:** the live device pulls (Mandarin + the `ZZCAP-CC` parity
  preset, non-activating GetContentData) **corrected findings §5** — the `cmnd`
  slot layout is **type-dependent**: PresetSnapshot = 5 int (`pvla..pvle`) + 5
  bool; MIDI footswitch/Instant = 12 (`pvla..pvll`) + 12 bool
  (`[0,ch,msb,lsb,-1,0,0,0,100,1,0,0]` for PC); NOT the uniform 12 §5 guessed.
  Each command is an entity (`cmnd.cid_` == its trg `eID_`, `enty 6`, `type 4`).
  **HW-validated:** a snapshot (FS1) + MIDI PC (Instant2) preset
  SetContentData'd → GetContentData'd back preserved both `cmnd` + `srcs`
  byte-for-byte (the #33 bar; the create-path hit the #38 anomaly, restore path
  worked).
  **Footswitch CC/Note/MMC layouts — ✅ HW-ANCHORED + FIXED (2026-07-15,
  PR `command-center-layout-fix`).** Real saved-blob capture (each subtype
  isolated on `ZZCAP-CC`, distinct values) pinned the footswitch 12-slot layout
  — it **reserves pvl1 = subtype and shifts data +1** vs the Instant layout
  (`ch@pvl2`, `MSB/LSB@pvl3/4`, `CC#@pvl6`, `CC value@pvl7`, `Note#@pvl8`,
  `velocity@pvl9`, `MMC message@pvl11`) — AND the device **`func` enum is
  Note=2 / MMC=3** (the opposite of the `.hsp` `Command` order Note=3/MMC=2).
  `transcode.py::_command_payload` now branches on source class
  (footswitch/`ctxt==1` vs Instant) and swaps `.hsp Command`→device `func` via
  `_HSP_TO_DEVICE_MIDI_FUNC`; the captured `pvl` arrays are golden assertions in
  `tests/test_commands.py`, and the HW round-trip (author FS CC+Note+MMC →
  `device install` → non-activating pull) confirmed the stored `cmnd` records
  match the captured tables byte-for-byte. Findings + tables:
  `docs/superpowers/specs/2026-07-15-hss-and-cc-capture-findings.md`.
  **Remaining residuals (honest):** no live authoring verb (the wire path
  `/attachCommandWithType` + `/setCommandParamVal` stays unimplemented);
  **recall-`preset` family deferred** (unanchored + byte-indistinguishable from
  snapshot 0 without a decoded discriminator — adversarial-review H1/H2);
  **HotKey/Utility (family 4)** + **EXP-continuous commands** out of scope
  (no anchor / not authored); **FS+command composition** (a switch carrying both
  a block-bypass footswitch AND a command) still rejected at authoring time
  (see below); **Instant** CC/Note/MMC slot placement + footswitch PC/Bank slots
  still inferred (only Instant PC + footswitch CC/Note/MMC captured) — and the
  Note/MMC `func` swap on **Instant** sources is an ASSUMPTION (global-enum
  reasoning; Instant PC's `0→0` is swap-invariant, so zero HW evidence),
  pinned by golden tests, verifiable only via an app-authored Instant Note/MMC
  capture which is **user-gated** (capture rig needs screen unlock /
  Accessibility — same gate as the XY/set-edit-buffer captures); and the
  **audible/functional MIDI pass is uncharacterized** (byte-survival only, like
  #33 — needs the user's physical MIDI gear). FS command-src
  `locl` for FS2–FS11 is EXTRAPOLATED from the single FS1 anchor (`25+NN`
  controller progression); a switch shared by `footswitches` + `commands` is
  rejected at AUTHORING time (device allows it; composing the two stores is
  deferred) — on READ, `view` keeps such a command under `unknown_controllers`
  (parseable, labeled) so a composed device export (Mandarin Fuzz) still
  round-trips. Adversarial review (2026-07-14, two rounds): round 1 fixed an
  `nxtm` divergence for command-free presets (M1), capped merged commands at 2
  (M2), added the `.hlx` warning (M3); round 2 found 1 Critical — the
  FS-collision projection broke the 211-export acceptance net (210/211) —
  fixed via the `unknown_controllers` bucket, nets re-verified 211/211.
  Design + review:
  `docs/superpowers/specs/2026-07-14-command-center-design.md`. Findings spec §5.
  Matrix §6.
- **P3 · #17 Matrix Mixer** — **🚫 CLOSED 2026-07-14: NOT an app feature.** The
  manual + app-bundle surveys confirm the desktop app has no mixer view (only
  the Output block's Pan+Level); per-output fader/pan/mute/solo is a
  device-hardware screen. Out of app-parity scope. Matrix §3.
- **P4 · #18 Signal-flow param depth** — **✅ SHIPPED (this release).**
  First-class authoring of input params (impedance/pad/trim/gate — recipe
  `input` object form), output level/pan (`output` object), split TYPE
  (y/ab/crossover/dynamic) + validated per-type params, merge-mixer params,
  and FX-loop trails; `set-param` pseudo-blocks (`input`/`output`/`split`/
  `join`/`merge`); `view` lifts everything back; params survive the
  `.hsp → _sbepgsm` transcode (incl. `preset.instN.z` impedance ints from
  the device's self-described enum). Design + evidence:
  `docs/superpowers/specs/2026-07-14-signal-flow-param-depth-design.md`.
  Matrix §3 rows all ✅.
- **P5 · #19 Live device ops** **[device-write] — ✅ MOSTLY SHIPPED 2026-07-14.**
  Shipped (all HW-validated on a Stadium XL):
  - **`device snapshot <index>`** — `/activateSnapshot [cmd, index]` (absolute).
  - **`device bypass PATH BLOCK on|off`** + **`device blocks`** lister —
    `/BlockEnableSet [cmd, dsp, block, enable]`; confirmed via the `/setBlockEnable`
    2001 echo. Live toggle is volatile until save.
  - **`device model PATH BLOCK <model>`** — `/ModelSet [cmd, dsp, block, sub, modelId]`.
  - **`device tuner`** — 2003 `/dspEvent` `eid_10/mid_796` fractional-MIDI pitch
    (network tuner, no engage needed). MCP mirrors for all.
  - **tempo** already works via `device settings set global.tempo.bpm`.
  - **`device meters`** — ✅ **SHIPPED 2026-07-14.** 2003 `/dspEvent` `eid_1/
    mid_796|800` 128-float grid-level arrays (same burst as the tuner); `--json`
    one-reading-per-line. MCP mirror `device_meters` (sampling one-shot). The
    semantic split between the two `mid_`s (input/output, path 1/2, …) isn't
    characterized — both are surfaced by their raw id.
  - **`device reorder <setlist> <target> --to <N>`** — ✅ **SHIPPED 2026-07-14**
    (see #20 below for the arg-decode history); a direct, immediate DEVICE-side
    write, distinct from the manifest-based `device slots reorder` + `sync`.
  Still open: **no `/CopySnapshot`** exists (copy = duplicate/batch writes);
  **time signature** = Song property over SFTP (not OSC); the `/ModelSet`
  cascade (controller re-attach + default push) is not replayed. Matrix
  §5/§9/§10.
- **P6 · #20 IR + library polish** — **🟡 MOSTLY SHIPPED (2026-07-14).**
  Shipped: IR delete/rename/prune (**#11** ✅), setlist
  create/rename/delete/duplicate (**#8** ✅ — creation cracked, no capture
  needed), preset color + notes (`device set-info`, batch-capable;
  color=`colr` int attr, notes=`pm__ preset.meta.info` content property).
  Design + protocol findings:
  `docs/superpowers/specs/2026-07-14-ir-library-polish-design.md`.
  Still open from the original row:
  - **#31 `.hss` setlist-bundle import/export** — ✅ **SHIPPED (2026-07-15):
    reader corrected + byte-faithful writer + device export, EXPERIMENTAL.**
    Capture + correction that unblocked it:
    `docs/superpowers/specs/2026-07-15-hss-and-cc-capture-findings.md` (real
    non-empty export captured to the expendable `Throwaway` setlist, then
    cleaned up). `.hss` = 24-byte Line 6 header + gzip + POSIX tar of
    `manifest.json` + 128 `.N` slot files (empty = 1-byte `0x00` sentinel,
    manifest `type: "<null>"`; filled = the preset's **`.hsp`** — magic
    `rpshnosj` + JSON, manifest `type: "application/stadium-preset"`).
    **Reader fix:** the import/install path no longer routes `slot.blob`
    through `content.decode_any` (which *raised* on a real export's `.hsp`
    payload) — it detects the payload format by **magic bytes** (cross-checked
    against the manifest `type`, disagreement warns) and **transcodes** a
    `.hsp` via `transcode.hsp_to_sbepgsm` (or normalizes a device content blob
    via `content.to_content_data`) before install; the preset name is read
    from the embedded `.hsp`'s `meta.name`. `--list` reports each slot's
    payload format. **Writer:** `hss.write_hss` emits a **byte-faithful**
    `.hss` — *given the same slot payload bytes*, the 24-byte header, the gzip
    10-byte header (`MTIME`/`XFL`/`OS`), and the *entire decompressed tar*
    (member names/order/bytes + exact octal ustar header field formatting via
    a hand-rolled writer + two-zero-block EOF) are byte-identical to a real
    export (pinned by re-serializing both captures); only the compressed
    DEFLATE stream differs (the app uses a non-zlib encoder no `zlib`
    window/mem/level reproduces — benign, any gunzip yields the identical
    tar). **Export verb:** `device setlist export-hss <setlist> <out.hss>`
    (+ MCP `device_export_hss`) builds a `.hss` from a device setlist's
    references, embedding each preset's **local `.hsp`** (resolved by name via
    the tone library) verbatim — mirroring the app; note helixgen `.hsp`s are
    compact JSON where the app pretty-prints, so an export built from
    helixgen-authored tones is functionally equivalent (same `rpshnosj`+JSON
    family, re-importable), not bit-for-bit the app's member bytes. HW
    round-trip validated in a **live device session 2026-07-15** (import the
    real app export → pool install + setlist reference verified on-device;
    export that setlist → header + `.1` payload + member names byte-identical
    vs the app's export of the same content → re-imported clean; all scratch
    setlists/pool presets deleted after). Not a committed test — the real
    fixtures are gitignored; offline tests pin the same paths with fake
    clients. MCP mirrors: `device_import_hss` / `device_export_hss`.
    **Residual: device-born presets can't be exported** — a referenced preset
    with no local `.hsp` is skipped (helixgen has no `_sbepgsm` → `.hsp`
    converter; a full device-content decompiler is the follow-up).
    **Residual: no dedupe-on-retry** — re-running an import after a partial
    failure installs + references the already-succeeded slots again (duplicate
    pool presets/references); the verb's help/docs say to clean up or use a
    fresh setlist before retrying. Making retry idempotent (skip-by-name
    against the pool, like `device sync`'s hash-skip) is future work.
    **Residual: pathless-on-pathless provenance loss** — recording an imported
    preset whose name is already registered as a pathless `save`/`create` tone
    silently rebrands that record's `source` to `import-hss` and drops its
    `doc`/`auto_marked` fields (`register_pathless` rebuilds the record; only
    `slot`/`device` are preserved). Narrow — no data-loss path (path-backed
    names are guarded and warned) — but a future guard should preserve or at
    least warn on overwriting an existing pathless record's provenance.
    Findings spec §8 + `2026-07-15-hss-and-cc-capture-findings.md`.
  - IR folders / move-to-folder (matrix §7) — content-path surface not RE'd. **Needs user input: capture session** (same screen-unlock/Accessibility gate as #34).
  - **Active-preset select (#1) — ✅ RESOLVED 2026-07-14:** the app's "make
    active" is `/LoadPresetWithCID` (load-by-CID) = existing `device load`; there
    is no separate active-index. (Only Songs have `/setActiveSongRef`.)
  - **Setlist/preset reorder** (`/ReorderContainerContent [cmd, container,
    [cids], newPos]`) — **arg decoded 2026-07-14; ✅ SHIPPED 2026-07-14** as
    `helixgen device reorder <setlist> <target> --to <N>` (+ MCP
    `device_reorder`), HW-validated on a Stadium XL against the `throwaway`
    setlist. `<setlist>="setlists"` reorders the setlist list itself (the same
    command works on both shapes). Direct DEVICE-side write — distinct from the
    local-manifest `device slots reorder` + `sync` path.
- **P7 · #21 Quick wins** — **✅ SHIPPED (2026-07-14).** `helixgen device
  info` / MCP `device_info` (`/ProductInfoGet` — model/device-id/serial/
  firmware/storage; HW-validated live) + controller depth: FS→**param
  toggles** (raw-unit min/max), **merge switches** (N targets per switch; one
  `srcs` + `scid → [cids]`), **scribble label/color** (palette ints anchored
  by live pulls), **curve/threshold** authoring + round-trip (non-linear
  curves EXPERIMENTAL — vocabulary + `curv` index anchored, audible response
  uncharacterized). Also fixed en route: EXP1Toe device `locl` 42→37 (was
  colliding with EXP1), momentary encoding `behv=1` (was `togl=True` — `togl`
  is volatile latch state), authored tones no longer inherit the chassis's
  stale scribble labels. Design + evidence:
  `docs/superpowers/specs/2026-07-14-controller-depth-device-info-design.md`.
  ZZB install→pull cycle persisted every field byte-exact. MIDI/XY sources
  split out to **#33**/**#34**. Matrix §6/§12.
- **#33 MIDI CC controller source** **[device-write] — MOSTLY SHIPPED
  2026-07-14 (EXPERIMENTAL, transcode path).** Recipe authoring: a top-level
  `midi` list (`{"cc": 0-127, "targets": [{block, param, min, max} | {block,
  bypass:true}]}`) parses/validates in `spec.py` (one-controller-per-param
  exclusivity across FS/EXP/MIDI; block bypass may be multi-source), authors via
  `mutate.wire_midi` into a helixgen-namespaced `preset._helixgen_midi` list,
  round-trips through `view`, and the **transcoder** (`bridge` + `transcode`)
  synthesizes the device `cg__.entt` `ctrl[]` (`cnt2`=CC#, `midi`=`0xB0<<8|cc`,
  `type` 1 bypass / 3 param, `tid_`) + `ctm_.ptid` mapping per findings spec §6.
  MCP `generate_preset` + CLAUDE.md + CLI.md + tone skill updated.
  **Residuals / honesty:**
  1. **Route decision (scope):** the binding is NOT written as a device-native
     `.hsp` controller. The `.hsp` `midisource` field is 0 across the whole
     211-export corpus (no factory preset uses MIDI) and the capture pinned only
     the *device* `.sbe`/wire encoding — the `.hsp` JSON MIDI-source shape is
     **unknown and was not invented**. So MIDI lives in `preset._helixgen_midi`
     and is consumed only by the transcoder. If the `.hsp`-native encoding is
     ever decoded, migrate to it.
  2. **`ctrl` record completeness — STORAGE HW-validated 2026-07-14.** §6 pinned
     `cid_`/`cnt2`/`midi`/`tid_`/`type` + the `ptid` map, not the *full* MIDI
     `ctrl` field set. helixgen emits the uniform ctrl schema (`behv`/`curv`/
     `dlay`/`goid`/`min_`/`max_`/`thrs`/`togl` + `trig=0`, no `srcs`/`scid` — the
     source is inline). Install→`SetContentData`→`GetContentData` round-trip on
     the Stadium XL persisted BOTH ctrl records **byte-for-byte** (`cnt2`
     61/79, `midi` 0xB03D/0xB04F, `type` 3/1, `min_`/`max_`, `tid_`, `ptid`
     `[65538,1]`, empty `srcs`/`scid`) — the device accepts + preserves the
     synthesized records. **Still uncharacterized — needs user input: physical MIDI gear** — the *audible* response (no
     external CC source was sent) — whether the device actually reacts to
     incoming CC61/79 with this stored shape is unverified.
  3. **MIDI Note is out of scope** (CC-only): §6 pinned only the CC controller
     source; a `note` field errors. (MIDI Note as a *Command Center* command is
     a separate subsystem — §5/#16.)
  4. **No live verb** — the "(+ optional live verb)" (`/attachParamController` +
     `/ControllerMIDISourceAdd` at runtime) stays open. Matrix §6.
  5. **Adversarial-review notes (2026-07-14, two rounds).** Round 1 (pre-PR
     diff) found no HIGH/MEDIUM; round 2 (PR #49 delta) found **1 Important —
     FIXED**: the surgical edit verbs (`add_block`/`remove_block`) renumber
     `bNN` positions without the `_helixgen_midi` records (which live outside
     the block dicts, unlike FS/EXP controllers), silently orphaning or
     mis-targeting MIDI bindings on install/sync. Fixed by reconciling the
     records on every renumbering path (identity-based old→new position map,
     so raw-export key gaps compact correctly; a record on the *deleted*
     block is dropped with a stderr warning naming the CC), by `swap_model`
     dropping bindings whose param the new model lacks (warning, same style
     as its dropped-param warnings) and refreshing survivors' stored block
     name, and by `view` treating the coordinate as authoritative (an
     unresolvable coordinate drops with a warning — no silent name fallback;
     a name mismatch warns and projects the placed block). Still open (LOW):
     (a) two different CCs on the SAME block's bypass are rejected within the
     `midi` list (conservative; FS+MIDI multi-source bypass IS allowed —
     relax if a real use appears); (b) findings §6 shows the device-serialized
     MIDI-param *leaf* also carrying `cid_` — helixgen's transcode stamps only
     `snap`/`tid_` (same as the HW-validated EXP/FS path); flag for the
     audible-response validation pass; (c) a MIDI param whose library name has
     no device mapping is silently dropped in `bridge` (pre-existing
     `ctl_params` pattern).
- **#34 XY-controller assignment** — **needs user input: capture session** (screen unlock + Accessibility grant; exploratory zone-storage dig). **[device-write] — ACTIVATION DECODED
  2026-07-14; storage still open.** Selecting an XY zone emits
  `/SetBatchedParamValues` = a 12-tuple `[dsp,block,sub,paramId,valueF64]` batch
  (the block's whole param set; no zone-index — the batch *is* the activation).
  ⚠️ **The inactive zones are NOT in the saved `_sbepgsm`** (only the active
  zone's params appear; no zone container/labels) — so XY *storage* location is
  unresolved and a `.sbe` round-trip does not preserve zones. Blocker for
  authoring; needs a follow-up dig on where zones persist. Findings spec §7.
  (Related: stomp **bank B**
  `0x010102NN` — encoding fully mapped (ctxt 2, live-anchored) and
  transcoded, but not exposed as an authoring identifier: the physical
  second-stomp-page layout/English naming is undecided; `view` keeps bank-B
  controls labeled in `unknown_controllers`.) Matrix §6.

**Out of scope** (matrix 🚫, listed for honesty): firmware update, factory
reset, SD format, full-device microSD backup, Showcase multitrack player,
Clones/Proxy captures, block favorites, preset templates, cloud/Remote-Access,
LED control, focus-view/UI cosmetics.

## Workflow / project health (added 2026-07-13)

- **#26 Git-commit tones/IRs from the skills** — **✅ SHIPPED (2026-07-14).** If
  the user's tone `.hsp`/`.md` output dir and/or IR library are inside a git
  repo, the `tone`/`setup`/`device` skills commit whenever they change those
  files (authoring a tone, editing a preset, registering IRs), with a sensible
  message. Detection is "managed by git" per-directory (`git rev-parse
  --is-inside-work-tree`); `guard_paid_irs_in_git` is respected (never
  force-add gitignored paid IR WAVs — commit `mapping.json`/catalog only); the
  new preference key `git_commit_tones` (`src/helixgen/preferences.py`,
  default `"auto"` = commit when a repo is detected, also accepts
  `true`/`false`, env override `HELIXGEN_GIT_COMMIT_TONES`) gates it, with a
  skill-level contract note in each of the three `.claude/skills/*` files. No
  Python git automation — the skills operate through the agent running `git`
  itself; only the preference key is code.
- **#27 CLAUDE.md freshness + best practices** — **✅ SHIPPED (2026-07-14).**
  Audited CLAUDE.md against the merged surface (PRs #45–#50): removed the
  duplicate `device save` bullet, fixed the stale `origin/main` → `github/main`
  remote name in the Development workflow, and confirmed the six just-merged
  verbs/fields (`device reorder`/`meters`/`setlist import-hss`, recipe
  `midi`/`commands`, `ir-prune --ignore-warnings`, `git_commit_tones`) are
  present + accurate. Restructured for length (768 → ~374 lines): the exhaustive
  recipe field reference moved to `docs/recipe-reference.md` and the full
  per-verb device reference to `docs/CLI.md`, leaving CLAUDE.md the project map,
  mental models, concise verb/field indexes with links, and the rules. All
  agent-critical guardrails (show-block-first, naming convention,
  `.hsp`-is-source-of-truth, device-write gating, flaky-network) stay in
  CLAUDE.md; skill pointers updated to the moved sections.
- **#28 Full code review + refactor pass** — **✅ SHIPPED WITH RESIDUALS
  (2026-07-15).** Findings doc + sequenced plan:
  `docs/superpowers/specs/2026-07-15-structural-review-findings.md`. **Executed
  the safe subset** (all behavior-preserving, full suite + 211-export
  acceptance net green after every commit): **S1** removed 6 verified-dead
  symbols + 1 unused import; **S3/S4/S5** resolved the #14 resolver residuals
  #51/#52/#53 (see below); **S6** extracted `cli.py`'s 2170-line `# --- device`
  section into `src/helixgen/cli_device.py` as a pure move (`cli.py` 2792 → 649
  lines; `device` group re-imported via `cli.add_command(device)`,
  `_auto_upload_irs` re-exported, `helixgen.cli:cli` entry point + full command
  tree byte-identical). **Deferred → #54 (since ✅ SHIPPED, structural pass 2):** S7 (fold the
  65 lazy device imports), S8 (rename `hss.slot_label`), S9 (decompose the
  oversized functions in F5), S10 (`mcp_server` result-shape consolidation).
- **#29 helixgen-tui** — **needs user input: brainstorm + design spec** (stack, UX; also see the 3-repo split). Design + implement a TUI that covers everything the
  Stadium desktop app does (the parity matrix above), driving the same library/
  device engines. **Key requirement: "slots" are invisible** — an implementation
  detail the user never sees or types; the UI speaks in tones and setlists only
  (the tone-library model's "slots are just addresses" taken to its conclusion).
  Needs its own brainstorm + design spec before any code.

- **#30 Slot semantics for slot-only tones — verify + decide** — **needs user input: front-panel check** (is a reference-less pool preset browsable from the device panel?) **+ the (a)/(b) decision.** — `device add`
  + sync installs a slot-marked tone into the **pool** but references it into no
  setlist, so its slot label is an address in name only: nothing is placed at
  `5A` in the user setlist, and `assign_slots` never fetches real device
  occupancy. The user's own workflow treats pool-only presets as reachable
  (sync-through-transient-setlist then remove references), but this is not
  hardware-verified. Verify on hardware whether a reference-less pool preset is
  browsable/loadable from the device panel; then either (a) make sync reference
  slot-marked tones into the `user` setlist at their slot position (spec §3.2's
  "the user setlist *is* the on-device population"), or (b) retire slot labels
  in favor of a bare on/off-device flag. Surfaced by the PR #34 adversarial
  review (finding 9).

- **#32 ir-prune / delete-ir minor residuals (PR #37 review)** — non-gating
  leftovers from the adversarial review, all fail-closed or flag-gated today:
  (a) **✅ SHIPPED (2026-07-14)** — `ir-prune`'s two consents are split:
  `--force` deletes *protected* (locally-referenced) IRs; a new
  `--ignore-warnings` proceeds despite unverifiable-local-tone *warnings*
  (CLI + MCP `ignore_warnings` arg);
  (b) **✅ SHIPPED (2026-07-14)** — a dangling setlist `rcid` (reference to a
  deleted pool preset) is now detected (probing the cid via `get_ref`) and
  aborts with an actionable error naming the stale reference + suggesting a
  re-sync/removal, instead of the misleading "listing looks incomplete /
  reboot" error (which is now reserved for a genuinely incomplete listing).
  Unvalidated assumption: `/GetContentRef` is presumed to return a dict for
  an existing-but-unlisted pool cid (a `None` reads as "dangling") — either
  way the prune aborts (fail closed), only the error text differs;
  (c) **✅ SHIPPED (2026-07-14)** — `resolve_device_ir_live` (the `--force-wedge`
  path's resolver) lists strictly, so a dropped/partial `-11` reply raises
  rather than resolving as "no such IR" and silently taking the file-only
  wedge cleanup;
  (d) theoretical: multi-message paginated listings (never observed; blob
  chunking IS covered) could evade the cross-check — **note only** (still open).

- **#35 Tone naming schema + embedded metadata + guitar variants** — **needs user input: brainstorm + design spec** (explicitly required by the entry). Requested
  2026-07-13. Three coupled changes to the tone library (needs its own
  brainstorm → design spec before implementation; #33/#34 are reserved by the
  in-flight PR #38):
  1. **Consistent naming schema.** Every tone is named
     `$artist - $song - $guitar` (display name / device preset name); the
     filename is the same schema slugged lowercase with dashes instead of
     spaces (e.g. `foo-fighters-white-limo-les-paul-jr.hsp`). Supersedes the
     current "<Tone Name> — <Guitar>" convention in CLAUDE.md's preset-naming
     section; `tone` skill + auto-registration adopt it.
  2. **Tone metadata as part of the library.** Each tone gets a JSON file in
     the plugin data dir (`$PLUGIN_DATA_DIR`) whose attributes include one
     that *is* the markdown describing the tone (today's companion `.md`
     folded in, not a sidecar path). The CLI can print a tone's description
     (e.g. `helixgen describe <tone>`). The metadata also records the
     **guitar settings for the tone** — not tuning, but how to set the
     guitar's knobs and switches for this tone (e.g. "bridge pickup, tone
     rolled to 7, coil split on"), expressed against the target guitar's
     control inventory from its profile (#22). Design must reconcile with the
     manifest (`setlists.json` already carries a `doc` path — the JSON
     metadata likely replaces it).
  3. **Guitar variants of one tone.** A tone can carry variants per guitar,
     stored one of two user-selectable ways, chosen when adding a variant or
     creating a multi-variant tone:
     (a) **snapshot replication** — one `.hsp`, all snapshots replicated per
     variant; or
     (b) **per-variant presets** — a different actual `.hsp` file for each
     variant, grouped under the same logical tone.
     **Default: (b) "different presets within the same tone"** unless the
     user chooses otherwise.

- **#36 IR metadata at registration — research + record** — **needs user input: brainstorm** (schema, research depth, backfill policy). Requested
  2026-07-13. When IRs are registered with the library (`register-irs`,
  `ir-scan`, MCP IR tools), research each IR and record descriptive metadata
  in the library JSON format (same shape/home as #35's per-tone metadata):
  what the IR models (cab/speaker/mic/mix provenance) and how it sounds
  (character tags). Sources: the pack's manual/docs (the `irs/_catalog/`
  pipeline already mines these — fold it in rather than duplicating: catalog
  README's controlled tag vocabulary + mic legend are the vocabulary),
  filename conventions, and optionally FFT analysis (the catalog's 5-band
  measured-tag pass). Explicitly *not* aimed at "make me sound like so-and-so"
  — the value is when crafting or tweaking your own tones (the `tone` skill /
  user can ask "which registered IR is tight/bright/vintage" without
  re-analysing WAVs). CLI/MCP read access to the metadata. Needs a brainstorm
  (metadata schema, research depth per-IR vs per-pack, backfill of
  already-registered IRs, offline behavior when no manual exists).

- **#38 `/CreateContent` returning a non-zero status code on a live device** —
  **🟡 HARDENED 2026-07-15 (anomaly not reproducible; root cause unconfirmed).**
  Found 2026-07-14 while hardware-validating #31's write path: every
  `/CreateContent` returned `code == 1` (not the documented `0`) while still
  allocating the pool entry, so `device install` / `import-hss` reported "failed
  to install" and left stubs. **Investigated 2026-07-15** (findings:
  `docs/superpowers/specs/2026-07-15-createcontent-status1-findings.md`):
  - **The anomaly CLEARED.** On fw **1.3.2 build 1340** every `/CreateContent`
    now returns `code == 0` — single raw create, 5 rapid create/delete cycles,
    and a full create→SetContentData→readback install (live-verified). The
    07-14 `code == 1` was **transient device/session state** (device
    power-cycled/settled between sessions). *Why* it returned 1 that day is
    still unknown and not reproducible — the one remaining open question; if it
    recurs, capture the raw `/status` + 2001/2003 streams at that moment.
  - **The "empty stub — `blck=-1, flow=-1`" claim was a MISDIAGNOSIS.** In a
    **pool** listing **every** preset shows `blck=-1, flow=-1`, including
    freshly + successfully installed ones; all 29 suspected "orphans" held
    11–20 KB of real `/GetContentData` content (the user's real library). Use
    `/GetContentData` size, not `blck`/`flow`, to tell an empty stub from a real
    preset. There were **zero** actual orphan stubs on the device.
  - **Client hardened** (`src/helixgen/device/client.py`, evidence-backed, safe
    regardless of root cause): new `_create_content_status` returns
    `(cid, code)` so a non-zero code no longer discards the side-effect
    allocation; new `_delete_created_stub` does **verify-before-delete** (match
    entry by name+`posi`, delete its *listed* cid — never the unreliable
    create-reply cid, fixing a latent wrong-delete bug); `_push_to_slot` /
    `_save_edit_buffer_to` now **raise a `HelixError` surfacing the code + the
    allocated cid** (with a "power-cycle + retry" hint) instead of silently
    orphaning. `_create_content`'s `code == 0` success contract is unchanged
    (no evidence supports accepting `code 1` as success). Regression tests
    added; suite 1501 passed. **No user action needed** unless the code-1
    anomaly recurs (then: power-cycle the Helix and retry — the client now
    self-cleans and names the cid to recover).

- **#39 `resolve_setlist_cid` is non-strict — a timeout can mint a
  duplicate-named setlist** — **✅ SHIPPED (2026-07-15).** `resolve_setlist_cid`
  (`src/helixgen/device/client.py`) now defaults to `strict=True` (threaded
  straight into `list_setlists`), so a timeout/undecodable listing raises
  `HelixError` instead of silently reading as "setlist absent" — `None` now
  means definitively absent, never "couldn't tell". Every caller that gates a
  create decision on it inherits the fix for free: `device setlist create`
  (pre-check), `rename` (both the source and new-name checks), `duplicate`
  (both src and the auto-created dst), and `device setlist import-hss`. The
  one deliberate exception is `create_setlist`'s own post-create relist retry
  loop, which now explicitly passes `strict=False` (it already knows the
  device just accepted the create — a transient listing hiccup there means
  "not yet visible, keep polling," not "duplicate risk"; it still falls back
  to the unreliable create-reply cid with a warning after 4 tries, unchanged).
  The wider audit (task 2) found the risk pattern also applies **beyond**
  setlist names: `HelixClient.mirror_setlist`'s own current-references listing
  (the add/remove reconciliation `sync`'s reference-rebuild step drives) was
  hardened to `strict=True` — a truncated read there would make a real
  reference look absent and the add-pass would then mint a **second**
  reference to the same pool preset, the identical duplicate-mint failure
  class #39 fixed for setlist names, just one layer down. `setlist_sync.py`'s
  pool listings (`list_presets(POOL)`, feeding both the install/skip plan AND
  the reference-rebuild step) and its never-orphan gate
  `_device_referenced_names` (feeding both the per-tone unsynced-delete step
  and `--gc`) were hardened to `strict=True` too — an under-reported pool
  listing could mint a duplicate-named pool preset or make `mirror_setlist`
  drop a still-wanted reference, and an under-reported referenced-set could
  make `--gc`/unsynced-delete treat a still-referenced preset as an orphan and
  delete it. `reorder.py`'s three listings (the numeric-setlist collision
  check, the target container listing, and the pool-name join) were hardened
  the same way — they gate the actual `/ReorderContainerContent` write. A
  strict-listing failure inside a per-setlist/per-tone step (the setlist
  resolve, `mirror_setlist`, the never-orphan gate) is caught locally and
  reported in `errors[]` — it skips just that item, matching the function's
  existing per-tone resilience contract, rather than aborting the whole sync
  run and losing already-recorded progress. Two read sites were audited and
  deliberately left lenient: `setlist_sync.py`'s post-write reference listing
  (pure bookkeeping into the manifest after the real write already happened —
  self-heals next run) and the plain browse verbs `device setlists` /
  `device_list_setlists` (interactive listing, not a write gate — the
  documented split in `list_container`'s own docstring). See the PR body for
  the full site-by-site audit table. Tests: strict-default + explicit
  `strict=False` unit tests on `resolve_setlist_cid`, a tolerant-retry test on
  `create_setlist`, a strict-propagation test on `mirror_setlist`, CLI + MCP
  abort-before-create tests for `setlist create`/`rename`/`duplicate`, and
  `sync_setlists`/`reorder_setlist_item` tests proving a listing failure is
  reported distinctly from "not found" (no "go create it" guidance), never
  proceeds to a write, and — for the per-item gates — doesn't abort sibling
  setlists/tones in the same run.
- **#40 `_lowest_empty_posi` picks a write position off a non-strict listing**
  — **✅ SHIPPED (2026-07-15).** `_lowest_empty_posi`
  (`src/helixgen/device/client.py`) — which `install_into_pool` and
  `create_setlist` call whenever the caller doesn't pin an explicit `pos` —
  now lists its container with `strict=True`, so a timeout/undecodable
  listing raises `HelixError` instead of silently reading as "container
  empty" and returning posi 0 into an already-full container. This is a
  **positional** collision, distinct from the **name**-based duplication #39
  fixed (that one made an existing setlist look absent *by name*; this one
  made an existing occupant look absent *by position*). Both callers already
  had exactly the right error-handling shape for this (no code changes
  needed there): `create_setlist`'s CLI/MCP sites already catch `HelixError`
  and abort with a clean message (`device setlist create`/`duplicate`, their
  MCP mirrors), and `install_into_pool`'s two batch callers
  (`setlist_sync.py`, `hss.py`) already catch it per-tone/per-slot into
  `errors[]` without aborting the rest of the run — the existing #38/#39
  resilience contract absorbs the new strict failure for free.
  What the device actually does on a genuine posi collision remains
  **unconfirmed** — the `/status` non-zero error taxonomy is uncatalogued
  (`docs/helix-protocol.md` §9), and the one non-zero code caught live so far
  (the transient #38 `code == 1` anomaly) happened once during ordinary
  hardware validation, not a deliberate occupied-slot write — a targeted
  attempt to reproduce it via rapid create/delete cycling explicitly *failed*
  (5/5 cycles returned `code == 0`). So it's only *plausible*, not evidenced,
  that a collision would ride that same code path; the strict listing here
  prevents an already-occupied posi from ever being chosen in the first
  place, which is the actual fix regardless.
  **Wider audit (per the #40 filing's ask), site-by-site:**
  | site | verdict | why |
  |---|---|---|
  | `_lowest_empty_posi`'s listing | **hardened → strict=True** | picks the exact posi the next `/CreateContent` targets |
  | `find_by_pos` (6 call sites: CLI `device install`/`save`/`push`/`slots restore`, MCP `device_install_preset`/`device_save_preset`) | **hardened → `strict=True` param, all 6 callers updated** | each gates "is this slot empty, safe to write?" — the same silent-empty-on-timeout risk as `_lowest_empty_posi`, just checking a caller-supplied `pos` instead of computing one |
  | `find_by_pos`'s own default | **left `strict=False`** | preserves the one legitimate lenient caller, `_find_by_pos_retry` (below); every real external caller now passes `strict=True` explicitly |
  | `_find_by_pos_retry` (→ `_create_from`) | **left lenient** | runs *after* its `/CreateContent`-equivalent already succeeded, polling for the device to re-index; a listing hiccup there means "not yet visible, keep polling," not "collision risk" — same shape as `create_setlist`'s own post-create relist (#39) |
  | `reorder_container`'s post-write fallback listing (~client.py:800), the "some reply, not the confirmation frame" case | **left lenient** | a reply frame having arrived at all proves the device processed the request (`/error`/non-zero-`/status` both raise first); pure bookkeeping to recover the confirmed order for the return value, not a write gate — same precedent as #39's post-write reference listing |
  | `reorder_container`'s fallback on a **total** timeout (zero reply frames) | **hardened → raises `HelixError`** (adversarial-review finding, fixed same PR) | the initial audit's "left lenient" reasoning didn't cover this sub-case: with *no* reply at all there is no `/error`/`/status` to have raised, so the original code silently re-listed and returned as if the reorder were confirmed — a false-success gap on a device-mutating write, distinct from (and worse than) plain "left lenient" bookkeeping |
  Tests: strict-default-preserved + strict-propagation unit tests for both
  `find_by_pos` and `_lowest_empty_posi`, abort-before-create tests for
  `install_into_pool`/`create_setlist` (assert no `/CreateContent` frame sent
  on a listing failure), an explicit-`pos` test proving that path skips
  `_lowest_empty_posi` entirely, a lenient-fallback regression test for
  `reorder_container`'s "some reply" case plus a raise-on-total-timeout test
  for its zero-reply case, and CLI + MCP abort-before-write tests for `device
  save`/`push`/`install`/`slots restore` and
  `device_install_preset`/`device_save_preset`
  (assert the write primitive — `save_edit_buffer_to`/`push_to_slot` — was
  never called). Full suite green.

### Resolver-pattern residuals (from the #14 audit, 2026-07-15)

These are the audit findings whose consolidation is **not** a pure
behavior-preserving swap — each carries a real semantic difference that must be
reconciled deliberately, so they were filed rather than forced into the #14
pass. **All three ✅ SHIPPED as part of #28 (2026-07-15)** — see
`docs/superpowers/specs/2026-07-15-structural-review-findings.md` "which
behavior wins" for each reconciliation.

- **#51 Unify the two `posi`→"1A".."8D" slot-label formulas.** **✅ SHIPPED
  (2026-07-15, S5).** `client.slot_label` is now the single source of the
  forward formula; `manifest._SLOT_LABELS` is derived from it
  (`tuple(slot_label(i) for i in range(_SLOT_BANKS*4))`, byte-identical), and
  `_posi_to_slot` keeps its capped / `None`-for-out-of-range contract unchanged.
  *Winner:* the formula lives once; both callers' contracts preserved exactly
  (no import cycle — client's deps never import manifest). The `hss.slot_label`
  name-collision rename was deferred to plan step S8 (→ #54).

- **#52 Extract a multi-match `list_setlists_by_name` helper for the reorder
  clash branch.** **✅ SHIPPED (2026-07-15, S3).** Added
  `HelixClient.list_setlists_by_name(name, *, strict, setlists=None)` as the one
  home for the case-insensitive (strip+casefold both sides) setlist name-match.
  `resolve_setlist_cid` returns the first match's cid through it; `reorder.py`'s
  numeric-argument clash branch routes through it too, passing its single strict
  listing as `setlists=` (no extra RPC; the `cid_present` scan reuses that same
  listing). *Winner:* `resolve_setlist_cid`'s strip-both-sides semantics — the
  reorder clash check gains stored-name stripping (a whitespace edge case,
  strictly more consistent). Returns all matches, so no caller loses info.

- **#53 Reconcile the two device-IR-hash normalizers.** **✅ SHIPPED
  (2026-07-15, S4).** Added `irmd.normalize_hash_string(s)` (= lowercase iff
  `len==32`, else `None`); both `client._hex_hash` and `sftp._addcontent_hash`
  string branches route through it. *Winner:* the safer **union** — length
  validation (from `_addcontent_hash`) **and** lowercasing (from `_hex_hash`).
  Observable only on the defensive string path (device IR hashes arrive as 16
  raw msgpack bytes, never strings); `sftp`'s loop still scans later args on a
  malformed hash. Original divergence: `_hex_hash` lowercased with no length
  check; `_addcontent_hash` enforced `len==32` but preserved case.

- **#54 Structural-plan residuals — steps S7–S10 of the #28 refactor.**
  **✅ SHIPPED (2026-07-15, structural pass 2).** All four steps executed
  behavior-preservingly (full suite + 211-export acceptance green after each;
  recursive click command-tree dump byte-identical to `main`):
  - **S7 — done.** Folded the ~65 repeated lazy `from helixgen.device import
    HelixClient, HelixError` / `SetlistManifest` statements in `cli_device.py`
    into two lazy accessors `_client()` / `_manifest()`. The optional-extra
    ImportError surface is fixed by construction (imports stay call-time inside
    the accessor). Pinned by new `tests/test_device_extra_import_surface.py`
    (poisons `sys.modules['zmq'/'msgpack']`: help paths + non-device commands
    exit 0; device verbs still error with the friendly `pip install
    'helixgen[device]'` message).
  - **S8 — done.** `hss.slot_label` → `hss_slot_label` (+ `test_slot_label_empty`
    → `test_hss_slot_label_empty`).
  - **S9 — done.** Decomposed the F5 oversized functions (the findings' per-
    function line counts were mislabeled by innermost nested def — real targets
    re-identified by AST body length): `synthesize_sfg` 125→62 (3 placement
    strategies + shared output-group tail), `_to_hsp_bnn` 120→85, `wire_
    footswitch` 144→96, `device_setlist_import_hss` 105→81, and
    `_synth_cg_from_recipe` 340→290 (extracted the two clean tail phases
    `_synth_commands` + `_emit_snapshots`). **Deliberately left (not a re-file —
    a net-negative rewrite):** `_synth_cg_from_recipe`'s controller-building core
    (nested closures `_new_trg`/`_src_for`/`_new_ctrl`/`_new_midi_ctrl` over
    shared mutable `srcs`/`ctrl`/`trgs`/`next_*` state) is intact — extracting it
    would require converting the closure state into an explicit state object, not
    a behavior-preserving-cheap change.
  - **S10 — done (safe scope).** Consolidated the `device_*` MCP handlers'
    connect + error-shaping boilerplate into a `_device_client(ip)` context
    manager (24 handlers routed through it; the 2 with inner HelixError handling
    and the subscriber/sync/globaleq paths left). `server.py` needs no change —
    its per-tool docstrings are the intentional agent-facing schema, not
    duplicated result-shaping. Deeper per-handler *result-dict* restructuring (if
    ever wanted) remains a genuinely-separate review pass — F6's own "belongs in
    its own review" caveat — and is not tracked as an active residual.

- **#55 PURGE paid IR WAVs from git history — needs user input: destructive
  force-push decision.** 1605 York Audio `.wav` files were accidentally
  committed in `4b503c1` (2026-07-13, manifest schema v2 — `irs/` was never
  actually gitignored despite the documented rule) and were public until
  untracked at HEAD on 2026-07-15 (PR #64, which also added the ignore rule).
  The blobs remain in git history (~100 MB of objects). Removing them requires
  a coordinated history rewrite + force-push of `main`/`stable`/tags (the
  1.0.3 mailmap-scrub playbook applies), which invalidates clones and is the
  owner's call — including whether to also rotate the release tags the
  workflow owns. Local files are untouched.

- **#56 `.hss`/CC optional follow-ups — needs user input: prioritization.**
  Genuinely optional features surfaced by the 2026-07-15 work, none blocked,
  build on request: (a) dedupe-on-retry for `import-hss` (skip-by-name against
  the pool, like sync's hash-skip); (b) device-born (pathless) presets in
  `export-hss` (needs a general `_sbepgsm`→`.hsp` converter — the decompiler
  round-trip problem, big); (c) live MIDI-CC/Command authoring verbs over the
  wire (`/attachParamController`/`/ControllerMIDISourceAdd`/
  `/attachCommandWithType` — protocol pinned, verbs unbuilt); (d) the
  `_synth_cg_from_recipe` closure-core rewrite the #28 findings doc rated
  net-negative without a state-object conversion.

### Three-repo split (2026-07-14)

helixgen was split into three repos under `sheax0r`: **helixgen-core** (this
repo — libs + CLI + MCP server, history carried over then purged of `irs/`,
so #55's blobs do NOT ship here), **helixgen** (the Claude Code
plugin/marketplace + skills, keeps its repo identity), and **helixgen-tui**
(the terminal UI, backlog #29 — design spec still pending, tracked in that
repo's own backlog). Consumers take core as a PyPI dependency (name
`helixgen`, availability verified 2026-07-14). Note #55 (the paid-IR history
purge of the ORIGINAL repo) is unchanged by the split and still pending.
Remaining follow-ups:

- **#57 Publish `helixgen` to PyPI via trusted publisher** — publish
  workflow committed (`.github/workflows/publish.yml`, OIDC trusted
  publishing on `v*` tags); the PyPI-side pending-publisher registration
  needs the owner's PyPI account. Until the first publish, consumers install
  from git (`uv` git pin).
- **#58 Slim the plugin repo to plugin-only content** — drop `src/`,
  `mcp_server/`, `tests/`, core docs from `sheax0r/helixgen`; repoint
  `.mcp.json` at core via `uv` git pin now, PyPI pin after #57's first
  publish; relocate the bundled block library (currently
  `mcp_server/data/library`); keep skills + `.claude-plugin/` + release
  workflow. Until #58 lands, the plugin repo keeps bundling core source and
  ships releases exactly as before — nothing breaks for users.
- **#59 Backlog + docs curation across repos** — this file stays core's
  backlog; plugin- and TUI-specific work moves to each repo's own
  `docs/BACKLOG.md` as it arises. Parity matrix + protocol docs stay in core.

- **#62 Loudness feedback loop — measured volume normalization** — the 2003
  `/dspEvent` meter grids (`{eid_:1, mid_:796/800}`) are per-node live audio
  envelopes with instrument-input and chain-out levels in the same burst.
  Spec + phase-0 hardware findings:
  `docs/superpowers/specs/2026-07-14-loudness-feedback-normalization.md`.
  **Phases 0–1 SHIPPED 2026-07-14** (same session as the spec): grid
  semantics characterized on hardware (~10 Hz, linear amplitude, taps
  upstream of output-block gain), the live-ops wire-index bug fixed
  (`(blks_key−1)/2` — bypass/model/set-param had been targeting the wrong
  blocks), and `device measure` + MCP `device_measure` (playing-gated robust
  dB stats incl. the input-invariant output÷input chain gain). **Remaining:**
  (a) phase 2 `device normalize` — per-snapshot / per-setlist closed loop;
  needs snapshot-aware `set-param` in `mutate` for per-snapshot `.hsp`
  trims, and note the phase-0 caveat that output-gain trims are dB-exact but
  invisible to the grid (verify via an in-chain actuator or trust the math);
  (b) the full per-layout cell-index formula (splits, dual-amp, DSP1, the ×4
  clusters) → `label_cells(reading, layout)`; (c) phase 3 USB-audio capture →
  quality metrics (LUFS, crest factor, FFT band energies in the IR-catalog
  vocabulary) feeding a tone-skill refinement loop at creation time when the
  device is online, and later iteration via the device skill; (d) skills
  integration lives in the plugin repo (cross-repo, after #56).
- **#63 MCP server removal — CLI is the only engine surface** — **✅ SHIPPED
  0.20.0** (mirrored in the coordination-workspace backlog, which is now the
  authoritative one). `mcp_server/` + `tests/mcp_server/` + the `[mcp]` extra
  deleted; every MCP tool mapped to a CLI verb (gaps closed: `helixgen patch`
  = atomic batch ops, `helixgen irhash` = stateless hash/discovery); the MCP
  tool descriptions' contract text ported into click `--help`; `--json` added
  to `list-blocks`/`show-block`/`list-irs`; parity pinned by
  `tests/test_cli_parity.py`. Spec + inventory table:
  `docs/superpowers/specs/2026-07-15-mcp-removal-cli-only.md`. **Cross-repo
  residual (plugin repo):** the plugin's `.mcp.json` + skills still reference
  the MCP server of pinned `helixgen[mcp,device]==0.19.1` (unaffected — a
  released version); when the plugin bumps to >=0.20.0 it must drop
  `.mcp.json`/the `[mcp]` extra and repoint skills at the CLI (fold into the
  #58 slim).

## Notes / principles
- **Local-file-first:** every device-write feature should also work offline
  against local `.sbe`/`.hsp`/`.wav` copies and sync to hardware on demand.
- **Device-write gating:** the auto-mode classifier blocks the agent from writing
  to the device (`no writes without telling me` — brick risk). Hardware
  validation therefore either runs via a user-invoked `!` script or a granted
  Bash permission rule. Reads (list/get_ref/download/watch) are unrestricted.
- The device is at `192.168.4.84` (ignores ICMP ping; ports 22/2001/2002/2003
  open).
