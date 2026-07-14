# helixgen

CLI that generates Line 6 Helix Stadium `.hsp` presets (and legacy `.hlx`) from
JSON tone specs. The library lives at `~/.helixgen/library/` (override with
`$HELIXGEN_LIBRARY`) and is built by ingesting real device exports.

User IRs (impulse responses) registered with `helixgen register-irs` live at
`~/.helixgen/irs/` by default (override with `$HELIXGEN_IRS`). The mapping
file `mapping.json` records `irhash → wav-path`. See `helixgen list-irs`.

## CLI

- `helixgen list-blocks [--category amp|cab|drive|delay|reverb|modulation|filter|eq|dynamics|pitch|volume|send]` — list blocks, optionally filtered.
- `helixgen show-block "<name>"` — print a block's exact param names, types, defaults, and observed ranges. **Run this before writing a spec** — param names are case-sensitive and the generator rejects unknown ones.
- `helixgen generate <recipe.json> -o <out.hsp>` — author a preset from a transient recipe (no sidecar is written). The `-o` flag is required. Output extension `.hsp` writes a Stadium-format file (8-byte magic + compact JSON); `.hlx` writes pretty JSON for the original Helix.
- `helixgen view <preset.hsp> [-o recipe.json]` — read-only projection of a `.hsp` back into the recipe shape (replaces the old `decompile`; `-o` dump is non-authoritative).
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg. Use `--force` to overwrite existing mappings.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash directly (no device export needed) and register. Requires libsndfile (`brew install libsndfile` on macOS). Only 48 kHz sources supported; non-48 kHz raises an error suggesting `sox`. This 48 kHz limit is a **helixgen** input constraint (it does not resample) — the **device** itself accepts any sample rate and normalizes internally, so a non-48k IR still works once imported onto the hardware; you just can't hash it off-device with helixgen without resampling first. Stereo WAVs are reduced to the left channel (matches Stadium's import).
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache. A WAV is skipped only when it is already registered **and** its cached hash is still valid for the file on disk (matching mtime + size), so an edited or replaced WAV is detected and re-hashed; `--rescan` recomputes unconditionally. Per-file failures (non-48 kHz, libsndfile errors) print a stderr warning and the scan continues. `--remove <basename>` forgets a single entry. Use this to bulk-register a whole IR library at once; use `register-irs` for one-off binding from a preset.
- `helixgen list-irs` — print `<hash>  <wav-path>` for every registered IR.
- `helixgen ir-cache --stats | --clear | --prune` — inspect/maintain the IR-hash **cache** (a pure-local perf layer that memoizes expensive Stadium-hash computes, keyed by absolute path + mtime + size; **not** `mapping.json`). `--stats` prints entry count, path, and size; `--clear` deletes the cache file; `--prune` drops entries whose backing WAV is gone. Default location `~/.helixgen/cache/irhash.json` (override with `$HELIXGEN_IRHASH_CACHE`, or `$HELIXGEN_CACHE` for the cache dir). All IR-hashing paths (`register-irs`, `ir-scan`, MCP IR tools) share it transparently.

Example: `helixgen ir-scan ~/IRs && helixgen list-irs | wc -l`.

### `helixgen device` — network control of a Helix Stadium (2.0+)

Talks to a **Stadium** over the LAN directly (OSC-over-ZeroMQ; no editor app).
Requires the `device` extra (`pip install 'helixgen[device]'` → pyzmq+msgpack).
Point at the device with `--ip`/`--port` or `$HELIXGEN_HELIX_IP` (default
`192.168.4.84`). Protocol reference: `docs/helix-protocol.md`. **Stadium-only**;
these verbs **mutate the device** — prefer an empty/expendable slot when testing.

- `helixgen device list [--setlist user|factory|throwaway] [--json]` — presets in a setlist.
- `helixgen device setlists [--json]` — the device's setlist containers.
- `helixgen device read <cid> [--json]` — a preset's metadata (name/slot/parent).
- `helixgen device load <cid>` — load a preset into the edit buffer.
- `helixgen device create --from <src_cid> --setlist <name> --pos <N>` — copy a preset into a slot.
- `helixgen device save <name> --setlist <name> --pos <N>` — save the live edit buffer as a new preset (slot must be empty).
- `helixgen device rename <cid> <new_name>` — rename a preset.
- `helixgen device delete <cid> [--setlist <name>] [--yes]` — delete a preset.
- `helixgen device set-param <path> <block> <param_id> <value>` — set one edit-buffer param (`/ParamValueSet`).
- `helixgen device save <name> --setlist <n> --pos <N>` — save the live edit buffer as a new preset (slot must be empty).
- `helixgen device pull <cid> <outfile.sbe>` — back up a preset's raw content blob.
- `helixgen device push <file.sbe> <name> --pos <N>` — install a local content file into a new slot (restore/clone).
- `helixgen device restore <file.sbe> <cid>` — overwrite an existing preset's content from a file.
- `helixgen device backup [--setlist <n>] [--dir <D>]` — pull a whole setlist to local `.sbe` files + `manifest.json` (offline backup).
- `helixgen device local-list [--dir <D>]` — list locally backed-up presets (works with the Helix disconnected).
- `helixgen device watch [--seconds N] [--filter <addr>]` — stream the device's live property/telemetry events (2001/2003).
- `helixgen device push-ir <file.wav>` — import an impulse response onto the device **instantly**, exactly like the editor. Uploads the device-canonical processed IR (`helixgen.ir.write_stadium_ir`), which embeds a `HASH` chunk carrying helixgen's `irhash` — the device reads that and registers under exactly that hash. And `push_ir` subscribes to the device's **2001 change stream first**, which activates the device's watched-dir monitor so the file registers in ~0.1 s (without a 2001 subscriber, external uploads wait on the device's slow ~15-20 min scan). Confirms via the `/addContent` broadcast; result reports `device_hash`/`hash_match`. See `docs/helix-sftp-access.md`.
- `helixgen device pull-ir <filename> <outfile>` — download an IR `.wav` by its on-device filename. EXPERIMENTAL.
- `helixgen device install <preset.hsp> <name> --pos <N> [--auto-irs]` — **author a helixgen `.hsp` onto the device as a new, playable preset** (the `/tone` → on-your-amp path). **Transcodes** the `.hsp` straight into the device's native content format (`_sbepgsm`) via `device/transcode.py` and `/SetContentData`s it into the empty pool slot — **no template, any block chain, full fidelity** (models/params/IRs); model/param names bridge helixgen↔device via `device/modelmap.py` + `device/defs.py`. Synthesizes the **full signal graph** — dual-amp / dual-DSP, **intra-flow parallel splits**, **snapshots** (per-scene bypass + param deltas), and **footswitch/EXP assignments** all transcode faithfully onto the device's real 28-slot grid (hardware-validated byte-for-byte vs HX Edit's own import, 2.18.0). `--auto-irs` uploads any IRs the preset references that aren't already on the device (resolving each `irhash` to a local WAV via `mapping.json`, then `push-ir`). Each `push-ir` registers instantly under the preset's `irhash` (via the `HASH` chunk + 2001 subscription — see `push-ir` above), so the installed preset's cabs resolve immediately with no editor step. EXPERIMENTAL.
- `helixgen device setlist list|add <setlist> <tone.hsp> [--pos N]|remove <setlist> <tone>|create-local <setlist>` — **manage the local setlist manifest** (`~/.helixgen/setlists.json`, override `$HELIXGEN_SETLISTS`). The device stores a preset **pool** (container `-2`) plus named **setlists** that hold **references** into it, so one authored tone can belong to many setlists. The manifest records, per setlist, an ordered list of tone names backed by a `tones` path map; it also **absorbs the old slot ledger** (one file now). `add` registers a tone's `.hsp` (by its `meta.name`) and appends it to the setlist's membership; `remove` drops membership (keeping the tone in the pool if other setlists still use it); `create-local` makes an empty setlist in the manifest only. **Never hand-edit the file** — use these verbs (or the MCP tools / `tone` skill). Device-side setlist *creation* is deferred (backlog #8): `create-local` and `add` only touch the manifest; a new setlist must also be created by hand in the Stadium app before it can be synced.
- `helixgen device sync <setlist> [--exclude-irs]` / `helixgen device sync --all [--gc] [--exclude-irs]` — **push the manifest's setlist(s) onto the device** (reference-based; **not** a destructive mirror). Resolves the named setlist under `-5` (errors clearly, telling the user to create it in the Stadium app first, if the device doesn't have it — #8). Then reconciles the **pool first** — installs tones missing from the pool, re-pushes ones whose `.hsp` content hash changed, skips unchanged ones (idempotent) — and **rebuilds the setlist's references** to manifest order, adding/removing/reordering as needed and **never orphaning** a pool preset another setlist still references. Uploads each tone's referenced IRs (unless `--exclude-irs`). `--all` reconciles every manifest setlist; `--gc` (only with `--all`) deletes pool presets no setlist references any more. Install **transcodes** each tone's `.hsp` straight into device content (no template, full fidelity — dual-amp, parallel splits, snapshots, and footswitch/EXP assignments all synthesized). Per-tone install/IR failures are reported in `errors[]` without aborting; result is `{ok, setlists, pool, references, gc, irs, errors}`. **The Stadium's network stack is flaky — if a sync drops or stalls, just re-run it (idempotent, auto-reconnecting); if it keeps dropping, reboot the Helix.** EXPERIMENTAL.

### The tone library (which tone lives where)

Every tone helixgen **generates auto-registers** into the **tone library** — the
manifest `~/.helixgen/setlists.json` (override `$HELIXGEN_SETLISTS`; a legacy
`device-slots.json` / v1 manifest is migrated on first load). A **tone** is
*content + identity + management state*: its `.hsp` (or nothing, if it came off
the device), a unique name (also the device preset key), a desired **user slot**
(`null` = off device, `"auto"` = wants device / address TBD, or `"1A".."8D"`),
its **setlist memberships** (ordered), provenance `source`, and observed device
placement. **"On the device" ⟺ the tone has a slot.** There is **no separate
slot ledger** — this one manifest is the single management record (design
`docs/superpowers/specs/2026-07-13-tone-library-model-redesign.md`).

- `helixgen register <tone.hsp> [--doc <md>]` — import an existing local `.hsp`
  into the library (off-device; `source: import-local`).
- `helixgen device add <tone> [--slot auto|5A]` — mark a library tone for the
  device (default `--slot auto`; placed on the next `device sync`).
- `helixgen device unsync <tone>` — clear a tone's slot so the next sync
  **deletes it from the device** (it stays in the library); cascades it out of
  any *synced* setlist.
- `helixgen device library [--json]` / `helixgen device slots [list] [--verify]`
  — list every tone: slot, on/off-device, and setlist memberships. Offline
  unless `--verify`, which cross-checks the live user setlist and flags
  `ok` / `missing` / `offline` / `untracked`.
- `helixgen device slots restore <name-or-slot> [--pos N] [--setlist S] [--force]`
  — re-install a tone from its recorded `.hsp` (re-authored) or `.sbe` (re-pushed).
  Pathless `save`/`create` tones have no local source and can't be restored.
- `helixgen device slots reorder <tone> --to <N> [--setlist S]` — move a tone
  within a setlist's order (default `user`). **Local only**; run `device sync
  <setlist>` to apply it to the device.
- `helixgen device setlist sync-on|sync-off <setlist>` — mark a named setlist as
  device-mirrored (marks all its members on-device) or a local-only draft.

**Sync is a managed-set mirror.** `device sync` installs/updates/reorders/**deletes**
only the tones helixgen manages (matched by name), auto-assigns `"auto"` slots to
free addresses, and **never touches untracked device presets** — a preset helixgen
didn't place is invisible to sync (not moved, not deleted, its slot not reused).

**Pushing tones to the device is driven by the `device` skill**
(`.claude/skills/device/`), which runs after `tone` has authored the `.hsp`.
It centers on `device sync <setlist>` / `device_sync_setlist` (the pool-first,
reference-rebuilding, IR-uploading, idempotent path) — the retired
directory-mirror `device sync [dir]` and `device_sync_library` tool are **gone**.
The skill adds the judgment those verbs need: manifest membership via `device
setlist add/remove` (the `tone` skill can add a freshly-authored tone to a
setlist), the **setlist-must-exist-first** rule (helixgen can't create a device
setlist — #8 — so a new setlist is made by hand in the Stadium app), the
**template-free transcode** install (the `.hsp` is re-serialized straight into
device content — any block chain, full fidelity, no template/coverage step; so
*don't hunt for templates or check factory-preset coverage*), the **never-orphan**
guarantee, the **full-graph synthesis** (dual-amp, parallel splits, snapshots,
and footswitch/EXP assignments all transcode — no serial-only limit any more),
the fact that the
single-tone **MCP** `device_install_preset` uploads no IRs and records no ledger
(use `device sync` or the CLI `install --auto-irs` instead), and the
**flaky-hardware** rule (re-run a dropped sync; reboot the Helix if it persists).
Read it before scripting a setlist sync.

Presets are addressed by integer **CID**; a preset lives once in the **pool**
(container `-2`) and is referenced by **setlists** enumerated under the setlists
root `-5` (`-5` is the *root*, **not** a setlist — `factory`=-1; `user`,
`throwaway`, and any user-created setlist like `helixgen` are child setlists with
their own positive cids under `-5`); slot `posi` maps to the Helix
`1A`..`8D` label. MCP mirrors these as `device_*` tools (`device_setlist_list`,
`device_setlist_add`, `device_setlist_remove`, `device_sync_setlist`,
`device_sync_all`). Full-preset semantic
authoring (helixgen `.hsp` → device) is a documented follow-up — the device's
native content format (`_sbepgsm`) is a separate schema from `.hsp`; see
`docs/helix-protocol.md` and `docs/superpowers/specs/2026-07-11-helix-device-v2-plan.md`.

## IR cab-pack catalog (character reference)

The IR library at `irs/` (gitignored — paid packs stay local) carries a
grep-first tonal catalog at `irs/_catalog/`. It answers "which IR is beefiest /
brightest / best for a vintage clean / tightest for modern metal" without
re-analysing WAVs. Start at `irs/_catalog/README.md` (index + controlled tag
vocabulary + mic legend + example greps); one file per pack holds per-mix mic
combos and character tags.

**When a new IR pack is added to `irs/`, catalog it before moving on:**
1. Read the pack's `*Manual*.pdf` — cab/speaker/amp, mic legend, per-mix mic
   combos, and any artist/usage notes.
2. `ls` the pack's `Mixes/` folder for the exact WAV basenames (these are what a
   preset's cab block references via `mapping.json`).
3. Optionally FFT-analyse each Mix WAV (stdlib `wave` + `numpy`, 5 guitar bands)
   for measured bright/dark/beefy/tight tags — relative *within* the pack.
4. Write `irs/_catalog/<slug>.md` from the template in the catalog README, using
   ONLY the controlled vocabulary; add a row to the README index table.

Don't invent character the manual doesn't state, but well-established general
knowledge is fine (Greenback = classic-rock, V30 = modern metal, ribbon = warm
top, SM7 = fat). The catalog README's "Adding a new pack" section is the
authoritative procedure and self-documenting template.

## Architecture: `.hsp` is the source of truth

A `.hsp` file is the 8-byte magic `rpshnosj` followed by a JSON document — it
**is** the canonical, editable artifact. There is no persisted intermediary
spec and **no `.spec.json` sidecar**. Two flows act on it:

- **Author** a new preset by feeding a transient **recipe** (the JSON shape
  below) to `generate`; helixgen clones the chassis template and replays the
  recipe as in-place mutations. The recipe is input-only — it is not written to
  disk and is never read back as truth.
- **Edit** an existing `.hsp` with the surgical verbs (`set-param`, `enable`,
  `add-block`, …); each reads the `.hsp`, mutates its body in place, and writes
  the `.hsp` back. No recompile, no sidecar.

To read a `.hsp` back into the recipe shape (for inspection or hand-authoring a
similar preset), use `helixgen view <preset.hsp>` — a read-only projection.

## recipe shape (author input to `generate`)

```json
{
  "name": "Preset Display Name",
  "author": "you",
  "paths": [
    {
      "blocks": [
        {"block": "Compulsive Drive", "params": {"Gain": 0.45, "Tone": 0.55}},
        {"block": "Brit Plexi Brt",   "params": {"Drive": 0.7, "Master": 0.5}},
        {"block": "Mic Ir_4x12 Greenback 25 With Pan"},
        {"block": "Tape Echo Stereo", "params": {"Mix": 0.18}},
        {"block": "Plate Stereo",     "params": {"Mix": 0.12}}
      ]
    }
  ]
}
```

- `paths` is 1–2 entries (each maps to one DSP); parallel splits inside a path are not supported in v1.
- `block` matches the display_name from `list-blocks` (e.g. "Brit Plexi Brt") — case-sensitive. If ambiguous, use the model_id in brackets (e.g. "HD2_AmpBritPlexiBrt").
- `params` values are floats 0.0–1.0 for most knobs; some are ints/bools/Hz. Verify ranges with `show-block`.

### Optional: per-path input routing

Each path entry may carry an optional `"input"` field with one of:
- `"inst1"` — Instrument 1 jack only
- `"inst2"` — Instrument 2 jack only
- `"both"` — both jacks (stereo) — **default on paths[0]**
- `"none"` — input disabled — **default on paths[1]**

Stadium-only; ignored with a warning for `.hlx` (legacy Helix) chassis.

### Optional: snapshots (Stadium scenes)

Add a top-level `snapshots` array (up to 8 entries) to define named scenes that override block bypass and param values within one preset:

```json
"snapshots": [
  {"name": "Rhythm"},
  {"name": "Lead",  "params": {"Brit Plexi Brt": {"Drive": 0.85}, "Tape Echo Stereo": {"Mix": 0.30}}},
  {"name": "Clean", "disable": ["Compulsive Drive"], "params": {"Brit Plexi Brt": {"Drive": 0.30}}}
]
```

- Each snapshot is a delta from path-level base values. Snapshot 0 (the first) is active on load.
- `disable: [...]` bypasses those blocks in that snapshot; `params` overrides values.
- Block references must resolve to a block already placed in `paths`.
- Omit `snapshots` entirely to use the device's defaults (8 unnamed slots, no variation).

When a snapshot references a block whose display name is ambiguous (multiple
placed blocks humanize to the same name, e.g. two "Stereo" blocks across a
split), carry a `(lane, pos)` coordinate:

- `disable` entries may be objects instead of bare strings:
  `"disable": [{"block": "Stereo", "lane": 1, "pos": 2}]`
- `params` may be a list instead of a name-keyed object:
  `"params": [{"block": "Stereo", "lane": 1, "pos": 2, "params": {"Mix": 0.3}}]`

Coordinates are only needed to disambiguate; the bare string / name-keyed object
forms remain valid for uniquely-named blocks. `path` (0 or 1) is added only when
the same name is ambiguous across both DSP paths.

### Optional: footswitches

Assign blocks to physical footswitches on the device. The Stadium XL has 12
capacitive footswitches in **2 rows × 6 columns** (top row FS1–FS6, bottom row
FS7–FS12), but only **10 are assignable**: `FS1`–`FS5` (top row) and
`FS7`–`FS11` (bottom row). `FS6` (**MODE**) and `FS12` (**TAP/Tuner**) are
reserved and rejected with a tailored error if you try to assign them. There is
also `EXP1Toe` — the toe switch under the onboard expression pedal (push the
pedal fully forward to click it).

```json
"footswitches": [
  {"switch": "FS3", "block": "Compulsive Drive"},
  {"switch": "FS4", "block": "Tape Echo Stereo", "behavior": "momentary"},
  {"switch": "EXP1Toe", "block": "Teardrop 310 Mono"}
]
```

- `switch` — an assignable footswitch `"FS1"`–`"FS5"` or `"FS7"`–`"FS11"`, or
  `"EXP1Toe"` (expression-pedal toe switch). `"FS6"`/`"FS12"` are reserved
  (MODE / TAP-Tuner) and not assignable.
- `block` — must reference a block placed in `paths`.
- `behavior` — `"latching"` (default; toggle) or `"momentary"` (on while held).
- One switch may be assigned at most one block; one block may be on at most one switch.
- **Wah/expression auto-engage:** assign the wah's bypass to `EXP1Toe` (with
  `EXP1` sweeping its `Pedal` param) so pressing the pedal toe-down engages the
  wah — the standard Helix wah behavior. A regular `FS` works too but requires a
  separate stomp.

**Controller vocabulary & English rendering.** `helixgen controllers`
(add `--json` for the machine-readable table) lists every assignable
controller with its English name + physical position, e.g.
`Footswitch 5 (top row, 5th from left)`. When reporting a tone to a human,
render controllers in this English form (via
`controllers.english_for_controller` / the `controller_mapping` MCP tool),
never a bare `FS#`. When a human *describes* a control in plain language
("the top-left switch", "second from right on the bottom", "the wah toe"),
translate it to a canonical identifier with a dedicated small-model
translation sub-agent fed `controller_mapping(stadium_xl)` — it returns exactly
one identifier (or `AMBIGUOUS`/`NONE`); validate the result against the
canonical set before writing it into a recipe. `view` never drops controls it
can't map: an un-tabled/out-of-v1-scope source is kept and labeled under a
separate top-level `unknown_controllers` list (ignored by `parse_spec`, so it
stays round-trip safe).

### Optional: expression pedal

Sweep one or more parameters with the expression pedal(s). Stadium XL
exposes `EXP1` and `EXP2`.

```json
"expression": [
  {
    "pedal": "EXP1",
    "targets": [{"block": "Teardrop 310 Mono", "param": "Pedal"}]
  },
  {
    "pedal": "EXP2",
    "targets": [
      {"block": "Brit Plexi Brt",   "param": "Master", "min": 0.0, "max": 0.7},
      {"block": "Tape Echo Stereo", "param": "Mix",    "min": 0.0, "max": 0.4}
    ]
  }
]
```

- `pedal` — `"EXP1"` or `"EXP2"`.
- `targets` — non-empty list. Each target sweeps one param on one block.
- `min`/`max` — normalized 0..1 floats; default `0.0`/`1.0`; must satisfy `min ≤ max`.
- One pedal may have many targets. One `(block, param)` pair may be driven by at most one pedal.
- v1 only sweeps 0..1-style float params (knob values). Hz/int/bool params are out of scope.

### Optional: per-block IR reference

For IR blocks (`"block": "With Pan"` and other `HX2_ImpulseResponse*` variants),
add an optional `ir` field to load a registered user IR:

```json
{"block": "With Pan", "ir": "YA DXVB 112 Mix 01.wav",
 "params": {"HighCut": 6500.0, "LowCut": 90.0, "Mix": 1.0}}
```

- `ir` accepts a wav basename (looked up in `mapping.json` values) or a
  32-char hex hash (looked up in keys).
- If `ir` is omitted, the block uses the canonical `irhash` recorded during
  ingest of an IR-bearing preset.
- Register IRs first with `helixgen register-irs`; see `list-irs` for what's
  available.

Stadium-only; ignored without warning for `.hlx` (legacy Helix) chassis output.

### Optional: delay/reverb trails (`trails`)

Delay and reverb blocks may carry an optional `"trails"` boolean that controls
harness spillover — whether the block's echoes / reverb tail keep ringing when
the block is **bypassed** (manually or via a footswitch):

```json
{"block": "Tape Echo Stereo", "params": {"Mix": 0.25}, "trails": true},
{"block": "Plate Stereo",     "params": {"Mix": 0.15}, "trails": true}
```

- `trails: true` / `false` sets the block's bNN `harness.params.Trails`.
  - `true` → tail rings out and fades when you bypass the block.
  - `false` → tail cuts off abruptly the instant you bypass the block.
- Trails governs tail spillover on **block bypass** (footswitch or manual) —
  and also across **snapshot switches** within the same preset (the tail rings
  through a scene change instead of cutting). It never bridges a **preset**
  change. To hear the bypass case, bypass the block — ideally while palm-muting
  so the guitar's natural sustain doesn't mask the wet tail. (Footswitch/
  manual-bypass behavior is hardware-validated on Stadium XL.)
- On the device, FX-Loop blocks also carry a `Trails` param, but helixgen's
  `trails` authoring field is scoped to **delay and reverb only** (below).
- Omitting `trails` leaves the device default (or whatever a decompiled
  `raw.harness` carried) untouched.
- **Delay and reverb only.** Setting `trails` on any other block category is a
  generate error.
- `view` lifts an existing `Trails` out of `raw.harness` into this clean
  `trails` field (delay/reverb blocks only), so it round-trips as a first-class
  setting. If both `trails` and a `raw.harness` are present, `trails` wins.
- Stadium-only; ignored for `.hlx` (legacy Helix) chassis (no harness emitted).
- Editing an existing `.hsp` never needs `trails`: `set-param`/edit verbs
  preserve the block's `harness` (and its `Trails`) verbatim in place.

### Optional: per-block verbatim state (`raw`)

A recipe block may carry an optional `"raw"` object holding verbatim Stadium bNN
state that helixgen does not model, so that *authoring* a preset from a recipe
can reproduce it:

- `"harness"` — the bNN-level `harness` dict (carries structural fields like
  `dual`, `upper`, `bypass`, `EvtIdx`, and its own `@enabled`). Non-deterministic;
  preserved verbatim. The one author-facing harness field, `Trails`
  (delay/reverb spillover), is modeled separately as the block-level `trails`
  field above and is lifted out of `raw.harness` by `view`.
- `"slots"` — additional slots beyond the first (`slot[1:]`), i.e. the second
  cab of a dual-cab block.

`raw` is emitted by `view` and consumed by `generate`. **Editing an existing
`.hsp` never needs `raw`** — in-place mutation leaves every unmodeled field
untouched by construction; `raw` matters only for authoring a fresh preset that
carries such state. Stadium-only.

## User preferences (`preferences.json`)

The `setup` / `tone` skills read explicit settings from a user-editable JSON
file — `~/.helixgen/preferences.json` (override the whole-file location with
`$HELIXGEN_PREFS`; override any single key with `HELIXGEN_<KEY>`, e.g.
`HELIXGEN_FAVOR_IRS=1`). Loaded by `src/helixgen/preferences.py`; per-key
precedence is env var > file value > built-in default. Keys include
`device.model`, `favor_irs`, `reveal_in_finder`, `guard_paid_irs_in_git`,
`preset_output_dir`, `author`, `default_guitar`, and `instruments`.

- **`default_guitar`** (string, default `null`) — which of the user's
  `instruments` to default to when a tone request doesn't name a guitar. Env
  override `HELIXGEN_DEFAULT_GUITAR`. When unset and the `tone` skill needs a
  guitar, the skill asks the user and offers to save the answer here.

**Preset naming convention.** Generated presets are named for the guitar they
target — the target guitar is appended to the preset **display title**, the
`.hsp`/`.md` **filename** slug, and stated near the top of the companion
markdown **description** (format `"<Tone Name> — <Guitar>"`, e.g. `White Limo
Lead — Les Paul Jr`). The guitar is omitted only when a tone is explicitly not
targeted at a specific guitar (a guitar-agnostic/generic patch). Guitar
resolution order in the `tone` skill: a user-named guitar wins; else
`default_guitar`; else the skill asks and offers to save the choice as
`default_guitar`.

## Surgical edits

Once a preset exists, don't re-author it to change one setting — use the edit
verbs below. Each reads the `.hsp`, mutates its body **in place**, and writes
the `.hsp` back, reusing all of helixgen's validation, model-id translation,
and IR injection. Works on ANY `.hsp` — one helixgen authored or a raw device
export — with no decompile step and no sidecar.

**Mental model:** the `.hsp` is the source of truth. An edit verb loads it,
applies one change to the verbose device-native JSON, and saves it. Fields
helixgen doesn't model (dual-cab slots, harness, `xyctrl`, …) are preserved
untouched by construction.

**Run `helixgen show-block "<block>"` first** to confirm the exact,
case-sensitive param name — the same guardrail `generate` already enforces.

- `helixgen set-param <preset> <block> <param> <value> [--path/--lane/--pos]` — set one param on one block; `<value>` is auto-coerced (bool → int → float → string).
- `helixgen enable <preset> <block> [--snapshot NAME] [--path/--lane/--pos]` — un-bypass a block at base level, or (with `--snapshot`) enable it in that snapshot.
- `helixgen disable <preset> <block> [--snapshot NAME] [--path/--lane/--pos]` — bypass a block at base level, or (with `--snapshot`) bypass it in that snapshot.
- `helixgen add-block <preset> <block> [--path N] [--after NAME]` — insert a block (append to `--path`, default 0, or after a named block).
- `helixgen remove-block <preset> <block> [--path/--lane/--pos]` — delete a block.
- `helixgen swap-model <preset> <old> <new> [--path/--lane/--pos]` — replace a block with another of the **same category**; carries over params the target shares, warns on any it has to drop.
- `helixgen view <preset.hsp> [-o recipe.json]` — read-only projection of a `.hsp` into the recipe shape (replaces `decompile`; the dump is non-authoritative).

`--path`/`--lane`/`--pos` disambiguate when a block name appears more than once
in the preset (e.g. dual-cab, both lanes of a split). (`--index` was removed in
1.0.0 — block addressing is `(path, lane, pos)`.) `--snapshot` applies only to
`enable`/`disable`.

MCP tools mirror the CLI for agent-driven edits, operating on `.hsp` **file
paths** (no base64 — the bytes never round-trip through agent context):
`generate_preset(model, recipe, out_path)` authors a `.hsp` from a recipe,
writes it to `out_path`, and returns `{path, warnings}`; `patch_preset(model,
hsp_path, operations)` applies a list of `{op, ...}` operations (`set_param`,
`set_enabled`, `add_block`, `remove_block`, `swap_model`) to the file **in
place** and returns `{path, warnings}`; `view_preset(model, hsp_path)` returns
the read-only recipe-shape projection. The agent edit loop is just a single
`patch_preset` call on the file — no decompile/regenerate round-trip.

### Worked examples

**Change a delay's Mix:**

```bash
helixgen show-block "Tape Echo Stereo"        # confirm the param is "Mix"
helixgen set-param MyTone.hsp "Tape Echo Stereo" Mix 0.3
# mutates MyTone.hsp in place (no sidecar)
```

MCP: `{"op": "set_param", "block": "Tape Echo Stereo", "param": "Mix", "value": 0.3}`

**Disable a block (kill the reverb):**

```bash
helixgen disable MyTone.hsp "Plate Stereo"
# add --snapshot Lead to bypass it only in the "Lead" snapshot
```

MCP: `{"op": "set_enabled", "block": "Plate Stereo", "enabled": false}`

**Swap an amp:**

```bash
helixgen list-blocks --category amp          # find the exact target display name
helixgen swap-model MyTone.hsp "Brit Plexi Brt" "Brit 2204"
# same-category only; carries over shared params, warns on any it had to drop
```

MCP: `{"op": "swap_model", "old": "Brit Plexi Brt", "new": "Brit 2204"}`
(surface any returned `warnings` to the user)

Disambiguate duplicate block names (e.g. two cabs across a split) with
`--pos`/`--lane`/`--path` on the CLI, or `"pos"`/`"lane"`/`"path"` on the MCP
op.

## Generation notes

- The chassis is whatever was first ingested. A Stadium chassis (`_helixgen_chassis_shape: "hsp"`) produces `.hsp` output; a `.hlx` chassis produces `.hlx`. Carryover `meta.color` / `meta.info` / `device_id` from the originating export is currently expected.
- Some Stadium model IDs are translated on ingest (e.g. `HD2_DistScream808Mono` → `HD2_DrvScream808`); generate translates back when writing `.hsp`.
- If the param validator fails with a list of valid names, run `show-block` and correct the recipe — don't guess.

## Project layout

- `src/helixgen/` — `cli`, `ingest`, `hsp`, `chassis`, `library`, `spec` (recipe parser/validator), `mutate` (in-place `.hsp` edit verbs), `recipe` (author `.hsp` from a recipe), `view` (read-only `.hsp` → recipe projection), `generate` (shared low-level `.hsp` builders + legacy `.hlx`), `controllers`, `preferences`, `bootstrap`, `ir`
- `tests/` — pytest suite (run with `PYTHONPATH=$PWD/src python -m pytest`); the golden-output contract (`tests/golden/`) and the 211-export real-device round-trip (`tests/test_decompile_acceptance.py`) pin `.hsp` fidelity
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — the user's personal `.hsp` exports

## Development conventions

- TDD throughout: failing test first, then minimal implementation. See existing test files for the established pattern.
- Pure stdlib + `click` for the CLI; no other runtime deps.
- Real-export fixtures live in `tests/fixtures/presets/` and are loaded by tests under skip-if-not-present guards so the suite stays green on a clean clone.

## Releasing (automated — do NOT move `stable` or push tags by hand)

Releases are published by `.github/workflows/release.yml`, which fires when
`.claude-plugin/plugin.json` or `.claude-plugin/marketplace.json` changes on
`main`. The plugin is installed from the GitHub **`stable` branch**, so merging
to `main` does NOT ship a release — only the version bump + workflow does.

To cut a release:

1. Bump the version in **both** `.claude-plugin/plugin.json` and
   `.claude-plugin/marketplace.json` (the workflow fails the build if they
   disagree). Conventionally also bump the lib version in `pyproject.toml` and
   `src/helixgen/__init__.py` (separate `0.1.x` line; feeds preset `meta`).
2. Commit `release X.Y.Z — …`, open a PR, merge to `main`.
3. The workflow then auto-creates the annotated tag `helixgen--vX.Y.Z` and
   fast-forwards `stable` to that commit. It is idempotent (no-op if the tag
   exists) and refuses to force-push if `stable` diverged.

Do **not** manually `git branch -f stable …`, push `stable`, or push a
`helixgen--v*` tag — the workflow owns those refs. The release is live once the
workflow has run; users then get it via `/plugin` update.

The plugin's MCP server loads its **bundled** `helixgen` + `mcp_server` from
`${CLAUDE_PLUGIN_ROOT}` (set via `PYTHONPATH` in `.mcp.json`), not a global
`pip install`. Only the `mcp` SDK + `click` must exist in the environment.
