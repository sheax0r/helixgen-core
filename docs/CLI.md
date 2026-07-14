# helixgen CLI

helixgen is primarily a [Claude Code plugin](../README.md) that drives a `/tone`
skill — but it ships with a Python CLI you can use directly. The CLI is the
right surface when you want to:

- Hand-tweak a JSON spec and generate from it
- Bulk-register an IR library
- Ingest your own `.hsp` exports to grow the block library
- Wire helixgen into your own tooling

The Claude Code plugin uses this same CLI under the hood (via the bundled MCP
server) — anything you can do in `/tone` you can do here, and vice versa.

## Install

Requires **Python 3.11+**.

```bash
git clone https://github.com/sheax0r/helixgen
cd helixgen
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

If all you want is the CLI as a black box (no source checkout), install it
straight from the `stable` branch — helixgen is **not** published to PyPI:

```bash
pip install "git+https://github.com/sheax0r/helixgen.git@stable"
```

The source install is what's recommended for contributors and for use alongside
the Claude Code plugin. Add the `[mcp]` extra
(`pip install "helixgen[mcp] @ git+…@stable"`) if you also want the MCP server.

## Quickstart

A fresh install has an **empty** block library at `~/.helixgen/library/` — you
must seed it before `generate` / `list-blocks` / `show-block` will find any
blocks. Seed it once with `helixgen bootstrap` (below) or point
`$HELIXGEN_LIBRARY` at an existing library. (The Claude Code plugin ships bundled
library data, so this step is CLI-only.)

```bash
# 1. Seed the library — from your own exports (preferred for accuracy)
helixgen ingest ~/MyPresets/

# Or from the sensorium/phelix community catalog
helixgen bootstrap

# 2. Browse the library
helixgen list-blocks
helixgen list-blocks --category amp
helixgen show-block "Brit 2204"

# 3. Generate a preset
helixgen generate my-tone.json -o my-tone.hsp
```

## Spec format

A tone spec is a JSON document. Minimal example:

```json
{
  "name": "My Rhythm Tone",
  "paths": [
    {
      "blocks": [
        { "block": "Noise Gate", "params": { "Threshold": 0.4 } },
        { "block": "Brit 2204",  "params": { "Drive": 0.6, "Bass": 0.5 } },
        { "block": "4x12 Greenback 25" }
      ]
    }
  ]
}
```

- `name` is the preset name shown in HX Edit.
- `paths` contains 1 or 2 chains (mapping to dsp0 / dsp1).
- Each block has a `block` (display name or model_id) and optional `params`
  (wire values: 0–1 floats for amp gain, integer Hz for cut frequencies,
  strings for enums like mic types).

For the full spec surface — input routing + input block params (impedance/
pad/trim/gate), output level/pan, parallel splits (split type + merge-mixer
params), snapshots, footswitch assignment (incl. merge switches, param
toggles, scribble label/color, response curves), expression pedal targets,
MIDI CC control (param sweeps + bypass toggles; EXPERIMENTAL, #33),
Command Center commands (footswitch/Instant MIDI PC/CC/Note/MMC + Preset/
Snapshot actions; EXPERIMENTAL, #16), per-block IR references, trails — see
[`docs/recipe-reference.md`](recipe-reference.md) which documents every field.
Device-network verbs (`helixgen device …`) are documented in the "Device
commands" section below.

## IR commands

`helixgen` reproduces Stadium's IR hash bit-identically without any device
round-trip, so you can register an IR library locally and reference IRs by
basename in your specs. See [`docs/ir-hash-algorithm.md`](ir-hash-algorithm.md)
for the algorithm.

**Prerequisite:** direct hash computation (`register-irs <wav>`, `ir-scan`)
needs **libsndfile** (`brew install libsndfile` on macOS; `apt install
libsndfile1` on Debian/Ubuntu).

```bash
# Bulk-register a whole IR directory (recurses; ~1 ms per IR after warm-up)
helixgen ir-scan ~/path/to/IRs/
helixgen list-irs | wc -l   # verify

# Register a single WAV
helixgen register-irs ~/path/to/some.wav

# Forget one entry
helixgen ir-scan --remove some.wav
```

Reference an IR by basename in a spec:

```json
{"block": "With Pan",
 "ir": "YA MRSH 412 T75 Mix 03.wav",
 "params": {"HighCut": 6800, "LowCut": 90, "Mix": 1.0}}
```

**Caveat:** for the `irhash` in a generated preset to actually resolve on the
device, the matching WAV must also be loaded onto the device via the Helix
Stadium app's **Librarian → Cab IRs → Import**. helixgen only handles the
preset side; importing IRs onto the device is the Stadium app's job. If a
slot displays "No Model" on the device after loading a preset, that IR
wasn't imported.

**Preset-binding form (legacy).** The original `register-irs` form binds the
irhash slots inside an exported preset:

```bash
helixgen register-irs <preset.hsp> <wav1> <wav2> ...
```

…this is still the only way to register IRs that aren't 48 kHz, since for
those you need to round-trip through a registration preset.

**Limitations:**
- **48 kHz sources only** for direct hash computation. Non-48 kHz raises a
  clear error with a `sox in.wav -r 48000 out.wav` suggestion.
- Stereo input is reduced to the **left channel** (matches Stadium's own
  import behavior).

## Library location

Default: `~/.helixgen/library/`. Override with `--library DIR` or the
`HELIXGEN_LIBRARY` env var.

## Commands

- `helixgen list-blocks [--category amp|cab|drive|delay|reverb|modulation|filter|eq|dynamics|pitch|volume|send]` — list blocks, optionally filtered.
- `helixgen show-block "<name>"` — print a block's exact param names, types, defaults, and observed ranges. **Run this before writing a spec** — param names are case-sensitive and the generator rejects unknown ones.
- `helixgen generate <spec.json> -o <out.hsp>` — generate a preset. The `-o` flag is required. Output extension `.hsp` writes a Stadium-format file; `.hlx` writes pretty JSON for the original Helix.
- `helixgen set-param <preset> <block> <param> <value> [--path/--lane/--pos]` — surgical edit of one param, in place. Besides library blocks, accepts the signal-flow pseudo-blocks `input` / `output` / `split` / `join` (`merge` alias) — e.g. `helixgen set-param t.hsp input impedance 1M`, `helixgen set-param t.hsp output level -- -3`, `helixgen set-param t.hsp join "A Level" -- -2`. **Negative values need the `--` sentinel** (else the shell-style parser reads `-3` as an option); put any `--path`/`--lane`/`--pos` flags *before* the `--`. See CLAUDE.md "Surgical edits" for the full verb set (`enable`/`disable`/`add-block`/`remove-block`/`swap-model`/`view`).
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.
- `helixgen bootstrap` — clone sensorium/phelix and ingest its `blocks/` folder.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash and register. Use `--force` to overwrite existing mappings.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg.
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache.
- `helixgen list-irs` — print `<hash>  <wav-path>` for every registered IR.


## Device commands (`helixgen device …`)

With the `device` extra (`pip install 'helixgen[device]'` → pyzmq+msgpack)
helixgen talks to a **Stadium** over the LAN directly (OSC-over-ZeroMQ; no
editor app). Point at the device with `--ip`/`--port` or `$HELIXGEN_HELIX_IP`
(default `192.168.4.84`). Protocol reference: [`helix-protocol.md`](helix-protocol.md).
**Stadium-only**; these verbs **mutate the device** — prefer an empty/expendable
slot when testing. CLAUDE.md carries the concise verb list + the mental-model
rules (device-write gating, flaky-network, tone-library); this is the full
per-verb reference. MCP mirrors are named inline (`device_*`).

### Preset + edit-buffer verbs

- `helixgen device list [--setlist user|factory|throwaway] [--json]` — presets in a setlist.
- `helixgen device setlists [--json]` — the device's setlist containers.
- `helixgen device info [--json]` — the device's identity over the network: model (+ helixgen chassis key), numeric device id, serial, firmware version/build/date, SD storage free/total (`/ProductInfoGet`; read-only, never touches presets or the edit buffer). MCP mirror: `device_info`.
- `helixgen device read <cid> [--json]` — a preset's metadata (name/slot/parent).
- `helixgen device load <cid>` — load a preset into the edit buffer.
- `helixgen device create --from <src_cid> --setlist <name> --pos <N>` — copy a preset into a slot.
- `helixgen device save <name> --setlist <name> --pos <N>` — save the live edit buffer as a new preset (slot must be empty).
- `helixgen device rename <cid> <new_name>` — rename a preset.
- `helixgen device delete <cid> [--setlist <name>] [--yes]` — delete a preset.
- `helixgen device set-param <path> <block> <param_id> <value>` — set one edit-buffer param (`/ParamValueSet`).
- `helixgen device blocks [--json]` — list the **live edit buffer's blocks** with their `(path, block)` coordinates, model name, and saved base on/off. Read-only. These are the coordinates `device bypass`/`device model`/`device set-param` address. MCP: `device_blocks`.
- `helixgen device pull <cid> <outfile.sbe>` — back up a preset's raw content blob.
- `helixgen device push <file.sbe> <name> --pos <N>` — install a local content file into a new slot (restore/clone).
- `helixgen device restore <file.sbe> <cid>` — overwrite an existing preset's content from a file.
- `helixgen device backup [--setlist <n>] [--dir <D>]` — pull a whole setlist to local `.sbe` files + `manifest.json` (offline backup).
- `helixgen device local-list [--dir <D>]` — list locally backed-up presets (works with the Helix disconnected).
- `helixgen device watch [--seconds N] [--filter <addr>]` — stream the device's live property/telemetry events (2001/2003).
- `helixgen device set-info <cid>... [--color <name|0-11>] [--notes <text>]` — set preset **color** and/or **notes** on one or more CIDs (batch-capable). Color is the `colr` content attr (int enum; names `auto, white, red, dark orange, light orange, yellow, green, turquoise, blue, violet, pink, off` — order inferred from the app menu, pass the raw index if a name renders unexpectedly). Notes are the Preset Info text, stored as the `preset.meta.info` property inside the content blob and written via a **non-activating** content round-trip. MCP mirror: `device_set_info`.
- `helixgen device install <preset.hsp> <name> --pos <N> [--auto-irs]` — **author a helixgen `.hsp` onto the device as a new, playable preset** (the `/tone` → on-your-amp path). **Transcodes** the `.hsp` straight into the device's native content format (`_sbepgsm`) via `device/transcode.py` and `/SetContentData`s it into the empty pool slot — **no template, any block chain, full fidelity** (models/params/IRs); model/param names bridge helixgen↔device via `device/modelmap.py` + `device/defs.py`. Synthesizes the **full signal graph** — dual-amp / dual-DSP, **intra-flow parallel splits**, **snapshots** (per-scene bypass + param deltas), and **footswitch/EXP assignments** all transcode faithfully onto the device's real 28-slot grid (hardware-validated byte-for-byte vs HX Edit's own import, 2.18.0). `--auto-irs` uploads any IRs the preset references that aren't already on the device (resolving each `irhash` to a local WAV via `mapping.json`, then `push-ir`). Each `push-ir` registers instantly under the preset's `irhash` (via the `HASH` chunk + 2001 subscription — see `push-ir` below), so the installed preset's cabs resolve immediately with no editor step. EXPERIMENTAL.

### Live device ops (mutate the ACTIVE tone)

These live-ops verbs mutate the ACTIVE tone (decoded + HW-validated 2026-07-14).

- `helixgen device snapshot <index>` — **recall a snapshot** (0-based, 0..7) on the live device (`/activateSnapshot`; absolute index) — changes the ACTIVE tone's snapshot immediately, like stepping the snapshot footswitch. MCP: `device_snapshot`.
- `helixgen device bypass <path> <block> <on|off>` — **bypass/enable a block** in the live edit buffer (`/BlockEnableSet [dsp, block, enable]`). Coordinates from `device blocks`. HW-confirmed via the `/setBlockEnable` echo. The toggle is *volatile* (audible at once, not written to the preset until you save, so `device blocks` won't reflect it). MCP: `device_bypass`.
- `helixgen device model <path> <block> <model>` — **swap a block's model** live (`/ModelSet [dsp, block, sub, modelId]`). `<model>` is a numeric model id or a model-id string like `HD2_AmpBritPlexiNrm` (see `list-blocks`). The device rejects a cross-category swap; the app's re-attach-controllers + push-defaults cascade is not replayed. MCP: `device_model`.
- `helixgen device reorder <setlist> <target> --to <N>` — **move a preset to a new position within a setlist** (`/ReorderContainerContent [container, [cids], newPos]`, decoded 2026-07-14, HW-validated). `<setlist>` is a setlist display name (resolved the way `device setlist rename/delete/duplicate` resolve setlists) or a literal container cid (`-2` = the pool, whose `cctp==PRESET` entries also resolve by their own names); `<target>` is a preset display name or a literal cid within it. Pass `setlists` as `<setlist>` to instead reorder the top-level setlist list itself (`<target>` is then a setlist name/cid) — the keyword is checked before name resolution, so a real setlist literally named "setlists" must be addressed by its container cid. **Numeric arguments are cid-first**: a purely-digit `<target>`/`<setlist>` is always parsed as a cid, never a display name. If an item is display-named that digit string, the cid reading wins with a stderr/result **warning** when the cid itself resolves in the container, and the command **errors** (pointing at the named item's real cid) when it doesn't. `--to` is bounds-validated against the container's current length before anything is sent. **This is a direct, immediate DEVICE-side write** — distinct from the local-manifest `device slots reorder`, which only edits the tone library's recorded order and takes effect on the device on the next `device sync` (which can then reorder things right back to the manifest's order). MCP: `device_reorder`.
- `helixgen device tuner [--seconds N] [--json]` — **live network tuner** (no Stadium app, no hardware-tuner engage needed). The Stadium runs an always-on background pitch detector and streams it on 2003 as `/dspEvent {eid_:10,mid_:796}` = a single **fractional-MIDI** float (int = note, frac×100 = cents, `-1` = silence). Prints a live note/cents/Hz readout with an in-tune meter; `--json` emits one reading per line. HW-validated (stream+decode); pitch math golden-tested. MCP mirror: `device_tuner` (sampling one-shot → `{signal, note, cents, hz, midi, samples}`).
- `helixgen device meters [--seconds N] [--json]` — **live network level meters** (no Stadium app needed), read-only. Same always-on `/dspEvent` burst as the tuner also carries two grid-level meter arrays, `{eid_:1,mid_:796}` and `{eid_:1,mid_:800}` — each a **128-float** array — which this decodes into a live bar readout; `--json` emits one reading per line (`{mid, peak, values}`). The semantic split between the two `mid_`s (input/output, path 1/2, …) isn't characterized, so both are shown by their raw id. MCP mirror: `device_meters` (sampling one-shot → `{meters: [{mid, peak, values}], samples}`).

### Global Settings + Global EQ

- `helixgen device settings list [--page <p>] [--values]` / `get <key>` / `set <key> <value>` — read/write the device's **Global Settings** over the network (no Stadium app). Every Global Settings page — Ins/Outs, Switches/Pedals, Displays, Preferences, Songs, Tempo/Click, MIDI, Date/Time — plus Tuner and Wireless is exposed as a device *property* in the `global.*` namespace (161 curated keys) and read/written via `/PropertyValueGet` / `/PropertyValueSet`. `list` browses the curated page→key catalog (offline; `--values` also fetches each key's live value + range from the device; `--page` narrows to one page); `get` reads one value with its device-supplied name/type/range/enum labels; `set` writes one — `<value>` may be a number or, for enum settings, a label (e.g. `set global.tuner.type Strobe`) or index, validated against the property's range/enum before sending. The device self-describes each key via `/PropertyDefWithKeyGet`, so the catalog is live, not hardcoded. Protocol RE + hardware-validation: `docs/superpowers/specs/2026-07-13-global-settings-re-findings.md`. **Global EQ** (`dsp.globaleq.*`) has its own verb — see `device globaleq` below (it IS property-based, just a variant value shape). MCP mirrors: `device_settings_list` / `device_settings_get` / `device_settings_set`.
- `helixgen device globaleq list` / `set <output> <band> <param> <value>` — write the device's **Global EQ** over the network (no Stadium app). The Stadium has three independent Global EQs, one per output layer: 1/4" (`qtr`), XLR (`xlr`), Phones (`pho`) — each a 7-band EQ (`lowcut`, `lowshelf`, `low`, `mid`, `high`, `highshelf`, `highcut`) plus an output level. Each param is a device property `dsp.globaleq.<out>.<band>.<param>` written via `/PropertyValueSet` with a **variant `{parm,valu}`** blob (byte-exact codec, HW-validated 2026-07-14). `list` prints the offline catalog; `set` writes one param (e.g. `device globaleq set qtr low gain 3.5`, or `set pho - level -2.0` for the output level). **Write-only over the network** — the device serves no `/PropertyValueGet` read-back for `dsp.globaleq.*`, so there is no `get`. Findings: `docs/superpowers/specs/2026-07-14-parity-capture-findings.md` §2. MCP mirrors: `device_globaleq_list` / `device_globaleq_set`.

### IR verbs (on the device)

- `helixgen device list-irs [--json]` — list the user IRs registered **on the device**: one line per IR, `<hash>  <mono|stereo>  <name>`; `--json` emits the raw metadata list. Read-only. Distinct from the local `helixgen list-irs`, which prints helixgen's own `mapping.json` (`irhash → wav-path`) without touching the device. The hash shown is what `device delete-ir` / `device rename-ir` accept to disambiguate duplicate names.
- `helixgen device push-ir <file.wav>` — import an impulse response onto the device **instantly**, exactly like the editor. Uploads the device-canonical processed IR (`helixgen.ir.write_stadium_ir`), which embeds a `HASH` chunk carrying helixgen's `irhash` — the device reads that and registers under exactly that hash. And `push_ir` subscribes to the device's **2001 change stream first**, which activates the device's watched-dir monitor so the file registers in ~0.1 s (without a 2001 subscriber, external uploads wait on the device's slow ~15-20 min scan). Confirms via the `/addContent` broadcast; result reports `device_hash`/`hash_match`. See [`helix-sftp-access.md`](helix-sftp-access.md).
- `helixgen device pull-ir <filename> <outfile>` — download an IR `.wav` by its on-device filename. EXPERIMENTAL.
- `helixgen device delete-ir <name-or-hash> [--yes] [--force-wedge]` — delete one user IR from the device **completely**: the registry entry (`/RemoveContent` on `-11`) plus its backing `.wav` (the device only garbage-collects the file lazily, which makes a quick re-import think it's "already on device"; removing the file closes that window). Presets that referenced it show a silent cab until it's re-imported. `--force-wedge` (32-hex hash only) additionally cleans the *wedged* state a delete→quick-re-import can leave (file + path index resolving, no registry entry) — never use it on a just-imported IR, whose listing may merely be lagging.
- `helixgen device rename-ir <name-or-hash> <new-name>` — rename a user IR on the device. Display-name only; the hash presets reference is untouched, so nothing breaks.
- `helixgen device ir-prune [--yes] [--force] [--ignore-warnings] [--only <name-or-hash>] [--json]` — delete device IRs **no preset references any more** (backlog #11). Diffs the device's user IRs against the `irmd` hashes referenced by every pool preset (non-activating `get_content` scan), by the **live edit buffer**, and by local tone-library `.hsp` files. Hardened to fail closed: every listing it trusts is strict (a timeout/partial listing aborts rather than reading as "no presets"), the pool listing is cross-checked against setlist references (a **dangling** reference — one pointing at a deleted pool preset — aborts with an actionable "remove the stale reference" error, not a misleading reboot hint), and execute mode re-scans + re-verifies the plan immediately before deleting (a disagreement aborts with nothing deleted). **Dry-run by default**; `--yes` executes. Two **independent** consents: `--force` also deletes IRs referenced only by a local off-device tone (*protected*); `--ignore-warnings` proceeds when a local tone's `.hsp` can't be read to verify its protection (executing over warnings). `--only` narrows to a single IR. MCP mirrors: `device_delete_ir` / `device_rename_ir` / `device_ir_prune` (`ignore_warnings` arg).

### Setlist management + sync

- `helixgen device setlist list|add <setlist> <tone.hsp> [--pos N]|remove <setlist> <tone>|create-local <setlist>` — **manage the local setlist manifest** (`~/.helixgen/setlists.json`, override `$HELIXGEN_SETLISTS`). The device stores a preset **pool** (container `-2`) plus named **setlists** that hold **references** into it, so one authored tone can belong to many setlists. The manifest records, per setlist, an ordered list of tone names backed by a `tones` path map; it also **absorbs the old slot ledger** (one file now). `add` registers a tone's `.hsp` (by its `meta.name`) and appends it to the setlist's membership; `remove` drops membership (keeping the tone in the pool if other setlists still use it); `create-local` makes an empty setlist in the manifest only. **Never hand-edit the file** — use these verbs (or the MCP tools / `tone` skill). `create-local` and `add`'s auto-create only touch the manifest — use `device setlist create` (below) to also create the setlist on the device.
- `helixgen device setlist create <name>` / `rename <old> <new>` / `delete <name> [--yes]` / `duplicate <src> <dst>` — **device-side setlist management** (backlog #8 **shipped**: `/CreateContent` under the setlists root with the setlist ctype, live-validated — no Stadium app needed). `create` makes an empty setlist on the device (and records it in the manifest); `rename` renames it on the device (and in the manifest, if tracked); `delete` removes the setlist container — its references die with it but the **pool presets they point at are never deleted** (never-orphan); `duplicate` copies `src`'s references into `dst` (auto-created when absent; must be empty otherwise) — references are pointers, so the pool presets are shared, not copied. MCP mirrors: `device_setlist_create` / `device_setlist_rename` / `device_setlist_delete` / `device_setlist_duplicate`.
- `helixgen device setlist import-hss <file.hss> [--list] [--setlist <name>] [--dry-run]` — **EXPERIMENTAL: import a Stadium-app `.hss` setlist-bundle export** (backlog #31, READ side). A `.hss` is a 24-byte Line 6 header + gzip + POSIX tar of `manifest.json` + 128 fixed `.N` slot files (empty = 1-byte `0x00` sentinel; filled = the preset's stored content blob), decoded via a hardware capture — findings spec §8. `--list` decodes the bundle fully offline (no device needed) and prints each slot's filled/empty state and preset name. Without `--list`, each filled slot is installed into the device **pool** (non-activating) and referenced into a device setlist (named `--setlist`, or the bundle's own name if omitted; created if absent) — reusing the same install + setlist-create + reference primitives as `device install`/`device sync`; `--dry-run` previews without writing. New references are **appended after whatever the destination setlist already has** (never a raw slot-index write), so importing into an already-populated setlist never collides with/overwrites its existing members. A filled slot whose payload doesn't look like recognized preset content (`helixgen.device.content`'s magic check) is skipped with a clear per-slot error instead of being sent to the device. Per-slot install/reference failures are reported without aborting the rest. **Container framing (header/gzip/tar/manifest/128-slot/empty-sentinel) is pinned against a real captured (empty) export; the FILLED-slot byte framing is an inferred assumption**, proven only against synthesized fixtures — no non-empty `.hss` export exists yet to confirm it (a byte-faithful *writer* remains out of scope until one does). Imported presets **are recorded in the tone library** as *pathless* tones (source `import-hss`) with membership in the destination setlist — load-bearing, so a later `device sync <setlist>` keeps their references instead of stripping them; having no local `.hsp`, they can't be restored by `device slots restore`. **Not idempotent on retry**: re-running after a partial failure duplicates the already-succeeded slots (pool presets + references) — delete the setlist + orphaned pool presets, or import into a fresh setlist, before retrying. MCP mirror: `device_import_hss`.
- `helixgen device sync <setlist> [--exclude-irs] [--repush]` / `helixgen device sync --all [--gc] [--exclude-irs] [--repush]` — **push the manifest's setlist(s) onto the device** (reference-based; **not** a destructive mirror). Resolves the named setlist under `-5` (errors clearly, pointing at `device setlist create <name>`, if the device doesn't have it). Then reconciles the **pool first** — installs tones missing from the pool, re-pushes ones whose `.hsp` content hash changed, skips unchanged ones (idempotent) — and **rebuilds the setlist's references** to manifest order, adding/removing/reordering as needed and **never orphaning** a pool preset another setlist still references. Uploads each tone's referenced IRs (unless `--exclude-irs`). `--all` reconciles every **synced** manifest setlist (local-only drafts are skipped; a targeted `sync <setlist>` marks that setlist synced); `--gc` (only with `--all`) deletes pool presets no setlist references any more. Install **transcodes** each tone's `.hsp` straight into device content (no template, full fidelity — dual-amp, parallel splits, snapshots, and footswitch/EXP assignments all synthesized). **`--repush`** (#25 residual) forces every in-scope tone already in the pool into the update bucket even when its recorded `.hsp` hash still matches, re-pushing its content via the same non-activating `SetContentData`-on-the-existing-cid path (the `device restore` primitive) a normal hash-triggered update uses — **after a helixgen transcoder upgrade**, `device sync <setlist> --repush` refreshes device content that a plain sync would skip as unchanged (hash-based change detection can't see a transcoder-output difference for an unchanged `.hsp`). Per-tone install/IR failures are reported in `errors[]` without aborting; result is `{ok, setlists, pool, references, gc, irs, errors}`. **The Stadium's network stack is flaky — if a sync drops or stalls, just re-run it (idempotent, auto-reconnecting); if it keeps dropping, reboot the Helix.** EXPERIMENTAL.

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
  `--force` pushes into an occupied destination slot (for **both** `.hsp` and
  `.sbe` sources) — it skips the emptiness check; the occupant is **not
  deleted**. The destination is an explicit `--pos`, else the recorded slot
  label, else the last observed `device.posi`. That observed posi can be
  stale (the device may have been reorganized since) — when in doubt,
  especially with `--force`, pass `--pos` explicitly.
- `helixgen device slots reorder <tone> --to <N> [--setlist S]` — move a tone
  within a setlist's order (default `user`). **Local only**; run `device sync
  <setlist>` to apply it to the device. For an immediate, direct DEVICE-side
  reorder that skips the manifest entirely, see `device reorder` above.
- `helixgen device setlist sync-on|sync-off <setlist>` — mark a named setlist as
  device-mirrored (marks all its members on-device) or a local-only draft.

**Sync is a managed-set mirror.** `device sync` installs/updates/reorders/**deletes**
only the tones helixgen manages (matched by name), auto-assigns `"auto"` slots to
free addresses, and **never touches untracked device presets** — a preset helixgen
didn't place is invisible to sync (not moved, not deleted, its slot not reused).

Presets are addressed by integer **CID**; a preset lives once in the **pool**
(container `-2`) and is referenced by **setlists** enumerated under the setlists
root `-5` (`-5` is the *root*, **not** a setlist — `factory`=-1; `user`,
`throwaway`, and any user-created setlist like `helixgen` are child setlists with
their own positive cids under `-5`); slot `posi` maps to the Helix
`1A`..`8D` label. MCP mirrors these as `device_*` tools (`device_setlist_list`,
`device_setlist_add`, `device_setlist_remove`, `device_sync_setlist`,
`device_sync_all`). The device's native content format (`_sbepgsm`) is a
separate schema from `.hsp`; see [`helix-protocol.md`](helix-protocol.md) and
`docs/superpowers/specs/2026-07-11-helix-device-v2-plan.md`.

**Pushing tones to the device is driven by the `device` skill**
(`.claude/skills/device/`), which runs after `tone` has authored the `.hsp`. It
centers on `device sync <setlist>` / `device_sync_setlist` (the pool-first,
reference-rebuilding, IR-uploading, idempotent path). The skill adds the
judgment those verbs need: manifest membership via `device setlist add/remove`,
the **setlist-must-exist-first** rule (a missing device setlist is one `device
setlist create <name>` away), the **template-free transcode** install (any
block chain, full fidelity, no template/coverage step), the **never-orphan**
guarantee, the **full-graph synthesis** (dual-amp, parallel splits, snapshots,
footswitch/EXP assignments all transcode), the fact that the single-tone **MCP**
`device_install_preset` uploads no IRs and records no ledger (use `device sync`
or the CLI `install --auto-irs` instead), and the **flaky-hardware** rule
(re-run a dropped sync; reboot the Helix if it persists). Read it before
scripting a setlist sync.
