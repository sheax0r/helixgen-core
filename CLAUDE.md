# helixgen

CLI that generates Line 6 Helix Stadium `.hsp` presets (and legacy `.hlx`) from
JSON tone specs. The library lives at `~/.helixgen/library/` (override with
`$HELIXGEN_LIBRARY`) and is built by ingesting real device exports.

User IRs (impulse responses) registered with `helixgen register-irs` live at
`~/.helixgen/irs/` by default (override with `$HELIXGEN_IRS`). The mapping
file `mapping.json` records `irhash → wav-path`. See `helixgen list-irs`.

**The project backlog lives at `docs/BACKLOG.md`** — check it before starting
new work (its "corrected mental models" preamble first); deferred work and
punted review findings get a numbered entry there, not a TODO comment.

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
- `helixgen device info [--json]` — the device's identity over the network: model (+ helixgen chassis key), numeric device id, serial, firmware version/build/date, SD storage free/total (`/ProductInfoGet`; read-only, never touches presets or the edit buffer). MCP mirror: `device_info`.
- `helixgen device read <cid> [--json]` — a preset's metadata (name/slot/parent).
- `helixgen device load <cid>` — load a preset into the edit buffer.
- `helixgen device create --from <src_cid> --setlist <name> --pos <N>` — copy a preset into a slot.
- `helixgen device save <name> --setlist <name> --pos <N>` — save the live edit buffer as a new preset (slot must be empty).
- `helixgen device rename <cid> <new_name>` — rename a preset.
- `helixgen device delete <cid> [--setlist <name>] [--yes]` — delete a preset.
- `helixgen device set-param <path> <block> <param_id> <value>` — set one edit-buffer param (`/ParamValueSet`).
- `helixgen device settings list [--page <p>] [--values]` / `get <key>` / `set <key> <value>` — read/write the device's **Global Settings** over the network (no Stadium app). Every Global Settings page — Ins/Outs, Switches/Pedals, Displays, Preferences, Songs, Tempo/Click, MIDI, Date/Time — plus Tuner and Wireless is exposed as a device *property* in the `global.*` namespace (161 curated keys) and read/written via `/PropertyValueGet` / `/PropertyValueSet`. `list` browses the curated page→key catalog (offline; `--values` also fetches each key's live value + range from the device; `--page` narrows to one page); `get` reads one value with its device-supplied name/type/range/enum labels; `set` writes one — `<value>` may be a number or, for enum settings, a label (e.g. `set global.tuner.type Strobe`) or index, validated against the property's range/enum before sending. The device self-describes each key via `/PropertyDefWithKeyGet`, so the catalog is live, not hardcoded. Protocol RE + hardware-validation: `docs/superpowers/specs/2026-07-13-global-settings-re-findings.md`. **Global EQ** (`dsp.globaleq.*`) has its own verb — see `device globaleq` below (it IS property-based, just a variant value shape). MCP mirrors: `device_settings_list` / `device_settings_get` / `device_settings_set`.
- `helixgen device globaleq list` / `set <output> <band> <param> <value>` — write the device's **Global EQ** over the network (no Stadium app). The Stadium has three independent Global EQs, one per output layer: 1/4" (`qtr`), XLR (`xlr`), Phones (`pho`) — each a 7-band EQ (`lowcut`, `lowshelf`, `low`, `mid`, `high`, `highshelf`, `highcut`) plus an output level. Each param is a device property `dsp.globaleq.<out>.<band>.<param>` written via `/PropertyValueSet` with a **variant `{parm,valu}`** blob (byte-exact codec, HW-validated 2026-07-14). `list` prints the offline catalog; `set` writes one param (e.g. `device globaleq set qtr low gain 3.5`, or `set pho - level -2.0` for the output level). **Write-only over the network** — the device serves no `/PropertyValueGet` read-back for `dsp.globaleq.*`, so there is no `get`. Findings: `docs/superpowers/specs/2026-07-14-parity-capture-findings.md` §2. MCP mirrors: `device_globaleq_list` / `device_globaleq_set`.
- `helixgen device save <name> --setlist <n> --pos <N>` — save the live edit buffer as a new preset (slot must be empty).
- `helixgen device pull <cid> <outfile.sbe>` — back up a preset's raw content blob.
- `helixgen device push <file.sbe> <name> --pos <N>` — install a local content file into a new slot (restore/clone).
- `helixgen device restore <file.sbe> <cid>` — overwrite an existing preset's content from a file.
- `helixgen device backup [--setlist <n>] [--dir <D>]` — pull a whole setlist to local `.sbe` files + `manifest.json` (offline backup).
- `helixgen device local-list [--dir <D>]` — list locally backed-up presets (works with the Helix disconnected).
- `helixgen device watch [--seconds N] [--filter <addr>]` — stream the device's live property/telemetry events (2001/2003).
- `helixgen device tuner [--seconds N] [--json]` — **live network tuner** (no Stadium app, no hardware-tuner engage needed). The Stadium runs an always-on background pitch detector and streams it on 2003 as `/dspEvent {eid_:10,mid_:796}` = a single **fractional-MIDI** float (int = note, frac×100 = cents, `-1` = silence). Prints a live note/cents/Hz readout with an in-tune meter; `--json` emits one reading per line. HW-validated (stream+decode); pitch math golden-tested. MCP mirror: `device_tuner` (sampling one-shot → `{signal, note, cents, hz, midi, samples}`).
- `helixgen device push-ir <file.wav>` — import an impulse response onto the device **instantly**, exactly like the editor. Uploads the device-canonical processed IR (`helixgen.ir.write_stadium_ir`), which embeds a `HASH` chunk carrying helixgen's `irhash` — the device reads that and registers under exactly that hash. And `push_ir` subscribes to the device's **2001 change stream first**, which activates the device's watched-dir monitor so the file registers in ~0.1 s (without a 2001 subscriber, external uploads wait on the device's slow ~15-20 min scan). Confirms via the `/addContent` broadcast; result reports `device_hash`/`hash_match`. See `docs/helix-sftp-access.md`.
- `helixgen device pull-ir <filename> <outfile>` — download an IR `.wav` by its on-device filename. EXPERIMENTAL.
- `helixgen device delete-ir <name-or-hash> [--yes] [--force-wedge]` — delete one user IR from the device **completely**: the registry entry (`/RemoveContent` on `-11`) plus its backing `.wav` (the device only garbage-collects the file lazily, which makes a quick re-import think it's "already on device"; removing the file closes that window). Presets that referenced it show a silent cab until it's re-imported. `--force-wedge` (32-hex hash only) additionally cleans the *wedged* state a delete→quick-re-import can leave (file + path index resolving, no registry entry) — never use it on a just-imported IR, whose listing may merely be lagging.
- `helixgen device rename-ir <name-or-hash> <new-name>` — rename a user IR on the device. Display-name only; the hash presets reference is untouched, so nothing breaks.
- `helixgen device ir-prune [--yes] [--force] [--only <name-or-hash>] [--json]` — delete device IRs **no preset references any more** (backlog #11). Diffs the device's user IRs against the `irmd` hashes referenced by every pool preset (non-activating `get_content` scan), by the **live edit buffer**, and by local tone-library `.hsp` files. Hardened to fail closed: every listing it trusts is strict (a timeout/partial listing aborts rather than reading as "no presets"), the pool listing is cross-checked against setlist references, unverifiable local tones surface as `warnings` (executing over warnings needs `--force`), and execute mode re-scans + re-verifies the plan immediately before deleting (a disagreement aborts with nothing deleted). **Dry-run by default**; `--yes` executes; IRs referenced only by a local off-device tone are *protected* and need `--force` too; `--only` narrows to a single IR. MCP mirrors: `device_delete_ir` / `device_rename_ir` / `device_ir_prune`.
- `helixgen device set-info <cid>... [--color <name|0-11>] [--notes <text>]` — set preset **color** and/or **notes** on one or more CIDs (batch-capable). Color is the `colr` content attr (int enum; names `auto, white, red, dark orange, light orange, yellow, green, turquoise, blue, violet, pink, off` — order inferred from the app menu, pass the raw index if a name renders unexpectedly). Notes are the Preset Info text, stored as the `preset.meta.info` property inside the content blob and written via a **non-activating** content round-trip. MCP mirror: `device_set_info`.
- `helixgen device install <preset.hsp> <name> --pos <N> [--auto-irs]` — **author a helixgen `.hsp` onto the device as a new, playable preset** (the `/tone` → on-your-amp path). **Transcodes** the `.hsp` straight into the device's native content format (`_sbepgsm`) via `device/transcode.py` and `/SetContentData`s it into the empty pool slot — **no template, any block chain, full fidelity** (models/params/IRs); model/param names bridge helixgen↔device via `device/modelmap.py` + `device/defs.py`. Synthesizes the **full signal graph** — dual-amp / dual-DSP, **intra-flow parallel splits**, **snapshots** (per-scene bypass + param deltas), and **footswitch/EXP assignments** all transcode faithfully onto the device's real 28-slot grid (hardware-validated byte-for-byte vs HX Edit's own import, 2.18.0). `--auto-irs` uploads any IRs the preset references that aren't already on the device (resolving each `irhash` to a local WAV via `mapping.json`, then `push-ir`). Each `push-ir` registers instantly under the preset's `irhash` (via the `HASH` chunk + 2001 subscription — see `push-ir` above), so the installed preset's cabs resolve immediately with no editor step. EXPERIMENTAL.
- `helixgen device setlist list|add <setlist> <tone.hsp> [--pos N]|remove <setlist> <tone>|create-local <setlist>` — **manage the local setlist manifest** (`~/.helixgen/setlists.json`, override `$HELIXGEN_SETLISTS`). The device stores a preset **pool** (container `-2`) plus named **setlists** that hold **references** into it, so one authored tone can belong to many setlists. The manifest records, per setlist, an ordered list of tone names backed by a `tones` path map; it also **absorbs the old slot ledger** (one file now). `add` registers a tone's `.hsp` (by its `meta.name`) and appends it to the setlist's membership; `remove` drops membership (keeping the tone in the pool if other setlists still use it); `create-local` makes an empty setlist in the manifest only. **Never hand-edit the file** — use these verbs (or the MCP tools / `tone` skill). `create-local` and `add`'s auto-create only touch the manifest — use `device setlist create` (below) to also create the setlist on the device.
- `helixgen device setlist create <name>` / `rename <old> <new>` / `delete <name> [--yes]` / `duplicate <src> <dst>` — **device-side setlist management** (backlog #8 **shipped**: `/CreateContent` under the setlists root with the setlist ctype, live-validated — no Stadium app needed). `create` makes an empty setlist on the device (and records it in the manifest); `rename` renames it on the device (and in the manifest, if tracked); `delete` removes the setlist container — its references die with it but the **pool presets they point at are never deleted** (never-orphan); `duplicate` copies `src`'s references into `dst` (auto-created when absent; must be empty otherwise) — references are pointers, so the pool presets are shared, not copied. MCP mirrors: `device_setlist_create` / `device_setlist_rename` / `device_setlist_delete` / `device_setlist_duplicate`.
- `helixgen device sync <setlist> [--exclude-irs]` / `helixgen device sync --all [--gc] [--exclude-irs]` — **push the manifest's setlist(s) onto the device** (reference-based; **not** a destructive mirror). Resolves the named setlist under `-5` (errors clearly, pointing at `device setlist create <name>`, if the device doesn't have it). Then reconciles the **pool first** — installs tones missing from the pool, re-pushes ones whose `.hsp` content hash changed, skips unchanged ones (idempotent) — and **rebuilds the setlist's references** to manifest order, adding/removing/reordering as needed and **never orphaning** a pool preset another setlist still references. Uploads each tone's referenced IRs (unless `--exclude-irs`). `--all` reconciles every **synced** manifest setlist (local-only drafts are skipped; a targeted `sync <setlist>` marks that setlist synced); `--gc` (only with `--all`) deletes pool presets no setlist references any more. Install **transcodes** each tone's `.hsp` straight into device content (no template, full fidelity — dual-amp, parallel splits, snapshots, and footswitch/EXP assignments all synthesized). Per-tone install/IR failures are reported in `errors[]` without aborting; result is `{ok, setlists, pool, references, gc, irs, errors}`. **The Stadium's network stack is flaky — if a sync drops or stalls, just re-run it (idempotent, auto-reconnecting); if it keeps dropping, reboot the Helix.** EXPERIMENTAL.

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
setlist), the **setlist-must-exist-first** rule (a missing device setlist is one
`device setlist create <name>` away — #8 shipped; no Stadium app needed), the
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

- `paths` is 1–2 entries (each maps to one DSP). Parallel splits inside a path use `split`/`join` entries (see "parallel splits" below).
- `block` matches the display_name from `list-blocks` (e.g. "Brit Plexi Brt") — case-sensitive. If ambiguous, use the model_id in brackets (e.g. "HD2_AmpBritPlexiBrt").
- `params` values are floats 0.0–1.0 for most knobs; some are ints/bools/Hz. Verify ranges with `show-block`.

### Optional: per-path input routing + input block params

Each path entry may carry an optional `"input"` field. The simple form is a
mode string:
- `"inst1"` — Instrument 1 jack only
- `"inst2"` — Instrument 2 jack only
- `"both"` — both jacks (stereo) — **default on paths[0]**
- `"none"` — input disabled — **default on paths[1]**

The object form adds the Input-block params (impedance / pad / trim / gate):

```json
"input": {
  "source": "inst1",
  "impedance": "1M",
  "pad": true,
  "trim": -6.0,
  "gate": {"enabled": true, "threshold": -55.0, "decay": 0.2},
  "link": false
}
```

- `source` — same vocabulary as the string form; optional (same defaults).
- `impedance` — `"FirstBlock"` / `"FirstEnabled"` (the auto modes), `"10K"`,
  `"22K"`, `"32K"`, `"70K"`, `"90K"`, `"136K"`, `"230K"`, `"1M"` (the device's
  full self-described ladder — no 3.5M on Stadium). Preset-level, per jack:
  applies to the jack(s) the source uses (with `"both"`, a per-jack object
  `{"inst1": ..., "inst2": ...}` is accepted). Omitted → the device default
  `"FirstEnabled"`; an omission never conflicts with another path's explicit
  value (explicit wins). Two paths giving the same jack **different explicit**
  values is an error.
- `pad` — bool (instrument sources only).
- `trim` — float dB, −24..6.
- `gate` — `true`/`false` shorthand, or `{"enabled", "threshold" (−96..0 dB),
  "decay" (0.01..1)}`. Giving the gate **object** implies `enabled: true`
  unless you set `"enabled": false` explicitly.
- `link` — StereoLink; `"both"` source only.
- With `"both"`, `pad`/`trim`/`gate.*` also accept per-channel values
  `{"1": x, "2": y}` (a scalar writes both channels).

`generate` always writes the **full** input-endpoint param set (defaults +
your overrides) and the used jacks' impedance — the chassis's gate/trim/pad
state and used-jack impedance never leak into an authored preset. (Scope:
an **unused** jack's impedance and an unused chassis flow's input *model*
keep their chassis values — only their endpoint params are normalized.)
`view` lifts non-default input params back into this object form
(all-default inputs stay the readable string).

Stadium-only; ignored with a warning for `.hlx` (legacy Helix) chassis.

### Optional: per-path output level/pan

```json
"output": {"level": -3.0, "pan": 0.4}
```

- `level` — float dB, −120..20 (the output block's `gain`).
- `pan` — float 0..1 (0.5 = center).
- Applies to the path's primary (lane-0 `b13`) output block. The output
  **destination** (Matrix/XLR/1/4"/Path-2 feed…) is not authored here — it
  round-trips verbatim via `structural` entries; an explicit `output` wins
  over a stale structural copy.

### Optional: parallel splits — split TYPE + merge mixer

A path's `blocks` may carry one or two `split`…`join` regions (lane-1 entries
between them form the B branch). The split takes a friendly `type` and
per-type params; the join is the merge mixer:

```json
{"split": {"type": "crossover", "params": {"Frequency": 800.0, "Reverse": false}}},
{"block": "Tape Echo Stereo", "lane": 1},
{"join": {"params": {"A Level": 0.0, "B Level": -2.0, "B Pan": 0.5,
                     "B Polarity": false, "Level": 0.0}}}
```

- Split types → params (validated; unknown names error and list the valid set):
  - `"y"` — `BalanceA`, `BalanceB` (0..1), `enable`
  - `"ab"` — `RouteTo` (0..1), `enable`
  - `"crossover"` — `Frequency` (25..15000 Hz), `Reverse`, `enable`
  - `"dynamic"` — `Threshold` (−60..0 dB), `Attack`/`Decay` (0.05..5 s),
    `Reverse`, `enable`
- A raw `model` string is still accepted (must agree with `type` if both are
  given); unknown models pass params through unvalidated.
- Join (merge-mixer) params — literal wire names **with spaces**: `"A Level"`,
  `"A Pan"`, `"B Level"`, `"B Pan"` (0..1), `"B Polarity"` (bool), `"Level"`
  (−60..12 dB). The device default for the master `"Level"` is **+3 dB** —
  omit it and the merged signal comes out 3 dB hot; write `"Level": 0.0`
  for unity.
- FX Loop / Send / Return block params (`Send`, `Return`, `Mix`, `DryThru`)
  are ordinary block params — author them like any other block.

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
  {"switch": "FS3", "block": "Compulsive Drive", "label": "DRIVE", "color": "red"},
  {"switch": "FS3", "block": "Tape Echo Stereo"},
  {"switch": "FS4", "block": "Brit Plexi Brt", "param": "Drive",
   "min": 0.45, "max": 0.7, "behavior": "momentary"},
  {"switch": "EXP1Toe", "block": "Teardrop 310 Mono"}
]
```

- `switch` — an assignable footswitch `"FS1"`–`"FS5"` or `"FS7"`–`"FS11"`, or
  `"EXP1Toe"` (expression-pedal toe switch). `"FS6"`/`"FS12"` are reserved
  (MODE / TAP-Tuner) and not assignable.
- `block` — must reference a block placed in `paths`.
- `behavior` — `"latching"` (default; toggle) or `"momentary"` (on while held).
- **Merge switch**: several entries may share one `switch` — the switch then
  toggles all of its targets at once (blocks and/or params). Each target
  (block, or block+param) may appear only once across all entries.
- **Param toggle**: add `param` plus **required numeric `min`/`max`** (raw
  param units — a Level is in dB, a knob 0..1) and the switch toggles that
  param between the two values instead of the block's bypass. A single-knob
  stomp is a param toggle; a multi-param change is a snapshot.
- **Scribble strip**: `label` (device shows ≤12 chars; longer warns) and
  `color` — one of `none auto red dkorange ltorange yellow green turquoise
  blue purple pink white`. Per switch: on a merged switch set label/color on
  one entry (or identically on all); conflicting values are a spec error.
  Only `FS1`–`FS5`/`FS7`–`FS11` have strips — label/color on `EXP1Toe` (or a
  pedal) warns and is not written.
- `curve` — controller response curve: `"linear"` (default) or `slow5`…`slow1`
  / `fast1`…`fast5`. Non-linear values are EXPERIMENTAL (vocabulary from the
  device's own enum table; persistence hardware-validated, audible response
  not yet characterized).
- `threshold` — flip point (float) for position switches like `EXP1Toe`;
  EXPERIMENTAL. Forces the explicit-bounds controller encoding.
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
- `min`/`max` — normalized 0..1 floats; default `0.0`/`1.0`. **Reverse sweep**
  = `min > max` (heel = max effect, toe = min) — corpus-real and supported.
- `curve` — per-target response curve, same vocabulary as footswitches
  (default `"linear"`; non-linear EXPERIMENTAL).
- One pedal may have many targets. One `(block, param)` pair may be driven by
  at most one controller (pedal OR footswitch param-toggle) across the spec.
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

### Optional: delay/reverb/FX-loop trails (`trails`)

Delay, reverb, and FX-Loop blocks may carry an optional `"trails"` boolean that controls
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
- Omitting `trails` leaves the device default (or whatever a decompiled
  `raw.harness` carried) untouched.
- **Delay, reverb, and FX-Loop blocks only** (FX-Loop = `HD2_FXLoop*`; the
  device manual documents Trails there too). Setting `trails` on any other
  block — including Send-/Return-only blocks — is a generate error.
- `view` lifts an existing `Trails` out of `raw.harness` into this clean
  `trails` field (same delay/reverb/FX-loop scope), so it round-trips as a
  first-class setting. If both `trails` and a `raw.harness` are present,
  `trails` wins.
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

- `helixgen set-param <preset> <block> <param> <value> [--path/--lane/--pos]` — set one param on one block; `<value>` is auto-coerced (bool → int → float → string). A **negative** value needs the `--` sentinel after any flags (`helixgen set-param t.hsp output level -- -3`). The block names `input` / `output` / `split` / `join` (`merge` = alias) are **signal-flow pseudo-blocks** addressing the path's endpoints / split / merge mixer (`--path` picks the DSP; `--pos` disambiguates two splits; `--lane` does not apply): input params use the recipe vocabulary (`impedance`, `pad`, `trim`, `gate`, `threshold`, `decay`, `link`), output params are `level`/`pan`, split/join params are the wire names (`BalanceA`, `Frequency`, `"A Level"`, …).
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

- `src/helixgen/` — `cli`, `ingest`, `hsp`, `chassis`, `library`, `spec` (recipe parser/validator), `mutate` (in-place `.hsp` edit verbs), `recipe` (author `.hsp` from a recipe), `view` (read-only `.hsp` → recipe projection), `generate` (shared low-level `.hsp` builders + legacy `.hlx`), `controllers`, `preferences`, `bootstrap`, `ir`, `irhash_cache`
- `src/helixgen/device/` — network device control (OSC-over-ZeroMQ client, `transcode`, `modelmap`, `defs`, setlist manifest)
- `mcp_server/` — the MCP server the plugin bundles; tool descriptions here are agent-facing behavioral contracts
- `.claude/skills/` — the three plugin skills: `setup` (device/prefs onboarding), `tone` (author a `.hsp` from a tone request), `device` (push/sync authored tones onto the hardware)
- `.claude-plugin/` — `plugin.json` + `marketplace.json`; bumping the version here on `main` is what triggers a release (see Releasing)
- `docs/` — `BACKLOG.md` (THE backlog), `superpowers/specs/` (design docs + review findings), `superpowers/plans/` (implementation plans), `features/` (per-feature deep dives), protocol references (`helix-protocol.md`, `helix-format-reference.md`, `helix-sftp-access.md`, `ir-hash-algorithm.md`)
- `tests/` — pytest suite (run with `PYTHONPATH=$PWD/src python -m pytest`); the golden-output contract (`tests/golden/`) and the 211-export real-device round-trip (`tests/test_decompile_acceptance.py`) pin `.hsp` fidelity
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — the user's personal `.hsp` exports
- `irs/` (gitignored) — paid commercial IR packs; character catalog at `irs/_catalog/`

## Development workflow

- **Worktrees, branched from fresh `origin/main`.** All non-trivial work happens
  in a git worktree whose branch starts from freshly-fetched `origin/main` —
  never commit directly on local `main`; it may be stale. Fetch again before
  picking a release version number (a concurrent PR once released 2.10.0
  mid-flight and collided with an in-progress bump).
- **Adversarial review before shipping.** Before merging a PR, dispatch at least
  one independent review subagent prompted to *break* the change (find bugs,
  regressions, spec violations — not summarize it). Confirmed findings are fixed
  or explicitly deferred to `docs/BACKLOG.md`. Major changes also get a committed
  review doc in `docs/superpowers/specs/` (see the PR #31 review for the shape).
- **Agent-facing surfaces ship in sync.** Any change to CLI-, MCP-, or
  skill-visible behavior updates, in the same PR, every surface that describes
  it: `.claude/skills/*`, this CLAUDE.md, the MCP tool descriptions in
  `mcp_server/`, and `docs/CLI.md`. Drift between code and these surfaces is a
  bug, not a docs chore.
- **Backlog discipline.** `docs/BACKLOG.md` is the single project backlog.
  Deferred work gets a numbered entry there — not a TODO comment, not a
  side file.
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
