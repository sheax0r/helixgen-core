# helixgen CLI

The `helixgen` CLI is the **only engine surface** — humans, scripts, and
agents (including the [Claude Code plugin](https://github.com/sheax0r/helixgen)'s
`/tone`, `/setup`, and `/device` skills) all drive this same CLI. Since 0.20.0
there is no MCP server: an agent starts at `helixgen --help`, and each verb's
`--help` is its behavioral contract (verbs agents consume support `--json`
for machine-readable stdout). Use it to:

- Hand-tweak a JSON recipe and generate from it
- Edit an existing `.hsp` surgically (one op, or an atomic batch via `patch`)
- Bulk-register an IR library
- Ingest your own `.hsp` exports to grow the block library
- Control a Helix Stadium over the LAN (`helixgen device …`)
- Wire helixgen into your own tooling

## Install

Requires **Python 3.11+**. helixgen is published to PyPI:

```bash
pip install helixgen              # core: authoring/editing/IR verbs
pip install 'helixgen[device]'    # + network device control (pyzmq, msgpack, paramiko)
```

Contributors install from a source checkout instead:

```bash
git clone https://github.com/sheax0r/helixgen-core
cd helixgen-core
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,device]"
```

## Quickstart

A fresh install has an **empty** block library at `~/.helixgen/library/` — you
must seed it before `generate` / `list-blocks` / `show-block` will find any
blocks. Seed it once from your own device exports with `helixgen ingest`, or
point `$HELIXGEN_LIBRARY` at an existing library. (The Claude Code plugin ships
bundled library data, so this step is CLI-only.)

```bash
# 1. Seed the library from your own exports
helixgen ingest ~/MyPresets/

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
- Each block has a `block` — a display name from `list-blocks` **or** the model_id, which is the stable, always-unambiguous handle — and optional `params`
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

**Registration copies into the library.** By default `register-irs` and
`ir-scan` **copy** each WAV into `~/.helixgen/library/irs/<pack>/` (pack = the
slugified source-folder name), scaffold a metadata sidecar JSON next to it, and
point `mapping.json` at the library copy (the original path is recorded in the
sidecar's `imported_from`); the WAV bytes stay gitignored while the sidecar +
`mapping.json` are committed. Pass `--no-copy` (both verbs) to register a WAV
in place with no metadata — the pre-library behavior. `ir-scan` is
content-addressed idempotent (a re-scan of the same WAV is a no-op); use
`helixgen library ir-backfill` to copy in + scaffold metadata for IRs that were
registered `--no-copy` or predate the library layout.

**Where `mapping.json` lives.** The default is now
`~/.helixgen/library/irs/mapping.json` (was `~/.helixgen/irs/mapping.json`). On
first use a pre-existing legacy `~/.helixgen/irs/mapping.json` is auto-bridged
up to the library location — entries preserved, relative values absolutized —
and the legacy file renamed `mapping.json.migrated-legacy`. `$HELIXGEN_IRS`
still overrides the whole IR dir and, when set, is used verbatim (the bridge is
skipped).

Reference an IR by basename in a spec:

```json
{"block": "IR",
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

- `helixgen list-blocks [--category amp|cab|drive|delay|reverb|modulation|filter|eq|dynamics|pitch|volume|send]` — list blocks, optionally filtered. Each line is `<display_name>  [<model_id>]`, and **either one identifies the block anywhere a block is named** — recipe `block` values, snapshot / footswitch / expression / MIDI targets, `show-block`, `add-block`, `swap-model`, `set-param`, `patch` ops.
  - **Display names are the editor's own, and unique for every model the editor names** (hgc-3ll) — which today is every block in the shipped library. They resolve at READ time from the vendored `model_names` table in `_param_ui.json` (regenerated by `tools/build_param_meta.py` from the app bundle's `P35ModelUIDefs.json`), so an existing `~/.helixgen/library` is corrected the moment you upgrade — no re-ingest, no migration, block files untouched. Before this the library stored the DEVICE's own short `@name`: it truncated (`ressor LAStudio Comp Mono`) and collided (`Stereo` named six models, one of them `VIC_DynPlateStereo`, the most-used reverb in the factory setlist, which could not be referenced at all). 333 blocks now carry 333 distinct names, up from 320.
  - Where two models genuinely share the editor's name, the qualifier is deterministic and baked into the vendored table: the plain amp keeps the bare name and its preamp-only sibling reads `Woody Blue (Preamp)`; a legacy stompbox model reads `Ping Pong (Legacy)`; an Agoura-namespace reissue reads `EV Panama Blue (Agoura)`. A mono/stereo pair is spelled `Chorus Mono` / `Chorus Stereo`.
  - **The model_id is the stable handle**: it never changes, never needs disambiguating, and survives a future name correction — prefer it in anything you script. An ambiguous *name* errors with every candidate model id listed.
  - A block the vendored table has never heard of — a model a firmware update added, before the asset is regenerated — keeps the name its library file carries (that is where a name of your own survives), unless that name is a bare `Mono` / `Stereo` / empty, which is dropped in favour of one derived from the model id. Such a block is the ONE way two display names can still collide; `rebuild_index` (run by every `ingest`) warns naming both model ids when it happens.
  - **Names that changed still resolve.** A pre-fix library file's old name becomes a **legacy alias**, so a recipe written against `Tape Echo Stereo` or `With Pan` keeps working after the upgrade. Aliases never outrank a real display name (`Woody Blue` is the amp, though it is also the preamp's legacy alias), and a degenerate old name (`Stereo`) is not bridged.
- `helixgen show-block "<name-or-model-id>"` — print a block's exact param names, types, real min/max, device defaults, **units** and enum labels. **Run this before writing a spec** — param names are case-sensitive and the generator rejects unknown ones. Ranges/units/labels resolve at read time from the vendored device model definitions (`_defs_data.json`) plus the editor's control definitions (`_param_ui.json`, regenerated by `tools/build_param_meta.py`); the block-library files themselves carry only one sighted sample per param. Reading the line format:
  - `Level  float -40..10 dB (default -10, sighted 2.7)` — the range is in **dB**. Never assume 0.0-1.0: an amp whose channel volume is `Level` in dB and one whose channel volume is `ChVol` in 0..1 sit side by side in the library, and writing `0.5` into the dB one is a +10.5 dB shove into the chain.
  - `Drive  float 0..1 (default 0.76, displays 0..10)` — write the raw 0..1 value; `displays` is what the hardware screen shows for that range. Whenever a `displays` segment is present the unit rides on IT, not on the raw range (`Time  float 0..2.5 (default 0.279, displays 0..2500 ms)` — `Time` is written in seconds). A unit on the raw range means raw and displayed are the same number (`-40..10 dB`).
  - `ChVol  ("Level" on screen)  float 0..1 …` — the param key differs from the name the hardware prints on it. Write the key; expect the user to say the screen name.
  - `Mic  int 0..11 (default 11 "67 Cond", sighted 4 "30 Dynamic")` plus an indented `values:` line — an enum. Labels are aligned to `min`, so a `SyncSelect1` of 1..19 has 1 = `1/1`.
  - `default` is the **device** default; `sighted` is the single value the ingested preset happened to carry — one sample, not a range, and not a recommendation. Printed only when it differs from the default.
  - `[internal]` marks a param the editor exposes no control for (`IrData`, `AmpCabPeak*`, `AmpCabZFir`): plumbing, not a knob — leave it alone. A tempo-syncable param's companions (`TempoSync1`/`SyncSelect1`) are NOT internal — the editor attaches them to the host knob, and presets set them.
  - `--json` carries the same facts per param — `{type, sighted, min, max, default, unit, scale, display_min, display_max, display_name, enum_labels, sighted_label, default_label, internal}`, each key present only when known. **Two `--json` contract changes:** `observed_range` is gone (it was always `[v, v]` from a single preset — a range it never was), and `default` now means the DEVICE default; the library's sighted value moved to `sighted`.
- `helixgen generate <spec.json> [-o <out.hsp>]` — generate a preset. `-o` is now **optional**. Default (no `-o`): writes into the tone library at `library/tones/<variant-slug>.hsp` and authors per-tone metadata JSON — name the tone with `--artist`/`--song` (paired) or `--descriptor` (mutually exclusive with artist/song), plus an optional `--guitar` (resolved to a guitar **profile** — see "Guitar profiles / resolution" under Library commands — and its `short_name` appended to the display name + slug); with no naming flag, the recipe's bare `name` becomes the descriptor. A slug collision (target `.hsp` already exists) errors with a rename suggestion — never overwrites. Explicit `-o <out.hsp>` preserves the legacy behavior exactly: writes there, auto-registers, naming flags ignored, **no metadata JSON written**. Output extension `.hsp` writes a Stadium-format file; `.hlx` writes pretty JSON for the original Helix.
- `helixgen patch <preset.hsp> <ops.json|-> [--json]` — apply a JSON **list** of ops (`set_param`, `set_enabled`, `add_block`, `remove_block`, `swap_model`) to the `.hsp` in one atomic invocation: all ops are applied in memory and the file is written once at the end, so an invalid op anywhere in the list leaves the file untouched. `-` reads the ops from stdin. Preferred over repeated single-op verbs for multi-edit sessions.
- `helixgen set-param <preset> <block> <param> <value> [--snapshot NAME_OR_INDEX] [--path/--lane/--pos]` — surgical edit of one param, in place. Besides library blocks, accepts the signal-flow pseudo-blocks `input` / `output` / `split` / `join` (`merge` alias) — e.g. `helixgen set-param t.hsp input impedance 1M`, `helixgen set-param t.hsp output level -- -3`, `helixgen set-param t.hsp join "A Level" -- -2`. **Negative values need the `--` sentinel** (else the shell-style parser reads `-3` as an option); put any `--path`/`--lane`/`--pos` flags *before* the `--`. **`--snapshot <name-or-0-based-index>`** (names win over a digit index; the same resolver backs `enable`/`disable --snapshot`, which therefore also take an index) writes the value into that ONE snapshot's slot of the param's 8-slot per-snapshot overrides array instead of the base — the param must already carry a base value (untouched slots densify to it; the base re-syncs to the active snapshot), and the preset must define snapshots (`preset.snapshots` meta — otherwise the transcoder would silently drop the array, so `set-param` errors instead). Snapshot overrides on library-block params round-trip through `view`; overrides on the `output` pseudo-block round-trip too, surfacing as the recipe's snapshot-level `output` field (#76 — see `docs/recipe-reference.md`); both kinds are realized on the device by `device install`/`sync`. Once a param's per-snapshot array varies, the device applies it on every snapshot — a later plain base edit of that param is inaudible on-device, and `set-param` warns when this happens (use `--snapshot`, or edit all 8 slots). On pseudo-blocks only `output` supports `--snapshot` (per-snapshot level/pan — the `device normalize` actuator). The companion surgical verbs are listed below; CLAUDE.md "Surgical edits" carries the mental model.
- `helixgen enable <preset> <block> [--snapshot NAME-or-INDEX] [--path/--lane/--pos]` — un-bypass a block at base level, or (with `--snapshot`) enable it in that one snapshot (name or 0-based index; names win — the same resolver as `set-param --snapshot`).
- `helixgen disable <preset> <block> [--snapshot NAME-or-INDEX] [--path/--lane/--pos]` — bypass a block at base level, or (with `--snapshot`) bypass it in that one snapshot.
- `helixgen add-block <preset> <block> [--path N] [--after NAME]` — insert a block (append to `--path`, default 0, or after a named block).
- `helixgen remove-block <preset> <block> [--path/--lane/--pos]` — delete a block.
- `helixgen swap-model <preset> <old> <new> [--path/--lane/--pos]` — replace a block with another of the **same category**; carries over params the target shares, warns on any it has to drop (surface those warnings to the user).
- `helixgen view <preset.hsp> [-o recipe.json]` — read-only projection of a `.hsp` back into the recipe shape (replaces the old `decompile`; the `-o` dump is non-authoritative). Prints JSON by default.

`--path`/`--lane`/`--pos` disambiguate when a block name appears more than
once in the preset (e.g. dual-cab, both lanes of a split); block addressing
is `(path, lane, pos)` — there is no `--index`. On a `patch` op the same
fields are `"path"`/`"lane"`/`"pos"` (plus `"snapshot"` and the signal-flow
pseudo-blocks).
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash and register. Use `--force` to overwrite existing mappings. By default each WAV is **copied** into `library/irs/<pack>/` with a scaffolded metadata sidecar and `mapping.json` points at the copy; `--no-copy` registers in place with no metadata.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg (same copy-into-library default + `--no-copy` escape hatch as the direct form).
- `helixgen irhash <wav-or-dir>... [--json]` — compute Stadium hashes statelessly (nothing written to `mapping.json`); directories are recursed for `*.wav`.
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>] [--no-copy] [--json]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache. By default each newly-hashed WAV is **copied** into `library/irs/<pack>/` with a scaffolded metadata sidecar and `mapping.json` points at the copy (content-addressed idempotent; a re-scan of the same content is a no-op); `--no-copy` registers in place with no metadata.
- `helixgen list-irs [--json]` — print `<hash>  <wav-path>` for every registered IR.
- `helixgen ir-cache --stats | --clear | --prune` — inspect/maintain the IR-hash **cache** (a pure-local perf layer that memoizes expensive Stadium-hash computes, keyed by absolute path + mtime + size; **not** `mapping.json`). `--stats` prints entry count, path, and size; `--clear` deletes the cache file; `--prune` drops entries whose backing WAV is gone. Default location `~/.helixgen/cache/irhash.json`, anchored under `$HELIXGEN_HOME` (which relocates it) — override the file with `$HELIXGEN_IRHASH_CACHE`, or the cache dir with `$HELIXGEN_CACHE` (both win over `$HELIXGEN_HOME`). All IR-hashing paths (`register-irs`, `ir-scan`, `irhash`) share it transparently.
- `helixgen controllers [--json]` — the device's assignable controllers (FS/EXP) with English names + positions.
- `helixgen analyze-audio <capture.wav> [--json]` — offline audio-quality metrics from a WAV capture (backlog #62 phase 3): integrated/momentary/short-term LUFS per ITU-R BS.1770 (K-weighting, 400 ms blocks / 75% overlap, −70 LUFS absolute + −10 LU relative gates), crest factor / peak / true-peak / RMS in dB, a clipping flag, spectral centroid, and FFT band energies over the 5-band guitar vocabulary (low 60–200 Hz, low_mid 200–500, mid 500–1200, high_mid 1200–4000, high 4000–10000 — **provisional edges**, pending reconciliation with the IR catalog's measured-tag pass). **`crest_db` / `peak_dbfs` / `rms_dbfs` are computed over the BS.1770-gated blocks**, not the whole file (hc-57h): ungated, a capture's leading/trailing silence reads as program material and skews them (5 s of silence in front of a real capture moved `crest_db` 12.45 → 15.46 dB while the gated LUFS correctly stayed at −16.77); a `notes` entry says so whenever the gate excluded anything, and the whole file is used when no gate is available (too short / silence). `true_peak_dbtp` and `clipped` stay whole-file — an over in the padding is still a real over. Undefined metrics (silence, sub-400 ms files) come back `null` with a `notes` entry, not an error; non-finite samples (NaN/Inf) are zeroed and counted in `notes`, so `--json` is always strictly valid JSON. Needs numpy (`pip install 'helixgen[analyze]'`); accepts any PCM / IEEE-float WAV, any sample rate, mono or stereo. Measurement caveats (backlog #84): the WAV is decoded whole-file into memory as float64 (~2.7 GB peak for an hour of 48 kHz stereo — keep captures to minutes; no streaming mode), and the momentary/short-term LUFS **maxima** are computed on a 100 ms hop, so a peak straddling two hop positions can under-read by a fraction of a dB (integrated LUFS is unaffected). EXPERIMENTAL `--record N -o <out.wav> [--input <device>] [--rate] [--channels]` records the capture first from an audio input (the Stadium's USB return) via sounddevice (`pip install 'helixgen[capture]'` + PortAudio) — untested against real hardware, and NOT the capture path proven on hardware: that is `helixgen.audio_capture` (sox), which `device normalize --measure-via capture` drives. Converging the two is backlog #104. The capture options `--input`/`--rate`/`--channels` apply only to `--record`; passing any of them without `--record` is a usage error (they used to be silently ignored). Complements `device measure` (network meters, loudness only): this tier is the one that can say what a tone *sounds* like, not just how loud it is.

**Machine-readable output:** verbs whose output agents/scripts consume take `--json` (`list-blocks`, `show-block`, `list-irs`, `irhash`, `patch`, `controllers`, `analyze-audio`, and the `device` read verbs); `view` prints JSON by default. `tests/test_cli_parity.py` pins the help-as-contract phrases and `--json` shapes.


## Library commands (`helixgen library …`)

Manage the artifact library: tones (`library/tones/*.json` — one JSON per
**logical tone**, an artist+song or a descriptor, grouping one or more
**variants**, each a real `.hsp` targeting a guitar), **guitar profiles**
(`library/guitars/*.json`), and **per-IR metadata** (`library/irs/**/*.json`
sidecars). See "Tone naming and the library" and "Guitar profiles" in
CLAUDE.md for the naming schema, the logical-tone/variant model, and the
guitar-profile schema. Every library-mutating verb (`import`, `migrate`,
`doc`, `ir-backfill`, and `generate`'s default no-`-o` path) auto-commits the
home repo afterward — advisory, gated by the `git_commit_tones` preference,
same posture as tone auto-registration.

**Guitar profiles / resolution.** A `--guitar <label>` (on `generate` and
`library import`) resolves to a guitar profile by slug / name / short_name
(case-insensitive, most-specific tier first): a match uses that profile's slug
+ short_name; profiles exist but none matches → error listing the known
guitars; the label matches 2+ distinct profiles → error to disambiguate by the
exact slug; **no** profiles exist yet → literal `slugify(label)` fallback with
a stderr notice (pre-migration compatibility). Profiles are seeded from
`preferences.instruments` by `library migrate`.

A tone `<name>` is resolved, in this order, as: the logical slug, the
metadata filename (`<slug>.json`), or any variant's `preset_name`; an unknown
or ambiguous name exits 1. This resolution order is shared by `library show`,
`library doc`, and the top-level `describe`.

- `helixgen library list [--tones|--guitars|--irs] [--json]` — list the
  library's tones, guitar profiles, and per-IR metadata, grouped by section
  (or narrowed to one with a flag). Guitar rows show slug / name / short_name /
  type; IR rows show the hash prefix, library-relative wav, and character tags.
  `--json` emits `{"tones": [...], "guitars": [...], "irs": [...]}` — narrowed
  to only the requested key(s) when a section flag is given.
- `helixgen library show <name> [--json]` — one tone's — or one guitar
  profile's — metadata: a compact human summary, or the exact on-disk JSON
  with `--json`. `<name>` resolves as a TONE first (logical slug, metadata
  filename, or any variant's `preset_name` — identity, tags, description
  presence, each variant's key/preset_name/hsp path, plus a summary of the
  variant's `normalized` record when `device normalize --yes` has written
  one — date, non-zero-trim count or "in band", scope; the record's full
  per-target measurement telemetry is in the `--json` dump); if no tone
  matches it is
  tried as a GUITAR profile (slug / name / short_name — name, type, pickups,
  construction, genres, character presence, and the control inventory).
  When a name resolves as a tone AND also matches a guitar profile, the tone
  is shown (tone-first order) with a stderr note naming the shadowed profile
  — address the guitar by a label only it matches to see the profile.
- `helixgen describe <tone>` — human-oriented write-up: header ("Artist -
  Song" or the descriptor), a variants table (guitar key, preset_name,
  guitar_settings, and a brief `normalized` summary when `device normalize
  --yes` has recorded one — e.g. `normalized 2026-07-16, 3 targets, 1 trim,
  max chain-out -0.2 dBFS (snapshots)`; over 0 dBFS = in-chain clipping),
  then the full `description_md` verbatim below a
  blank line. The longer-form counterpart to `library show`'s compact summary.
- `helixgen library doc <name> (--from-file <path> | -) [--variant <guitar>]`
  — set a tone's markdown write-up. Content comes from exactly one of
  `--from-file PATH` or a literal `-` argument (reads stdin) — giving neither
  or both is an error. Without `--variant`, sets the logical tone's
  `description_md` (what `describe` prints verbatim); with `--variant
  GUITAR_SLUG`, sets that variant's `notes_md` instead (exits 1 if the tone
  has no such variant). Bumps the tone's `updated` date and auto-commits.
- `helixgen library validate [--json]` — shape + cross-link checks split
  into **problems** (exit 1) and non-fatal **warnings** (exit unaffected).
  Across every tone: each variant's `.hsp` exists, its `preset_name` is
  registered in the setlist manifest, and its guitar key is a known
  guitar-profile slug (or the special `generic`) — now checked **exactly**
  against `library/guitars/*.json`, falling back to the variant keys already
  in use ONLY when no profiles exist yet (so pre-migration tones made with
  `generate --guitar` aren't falsely flagged). IR sidecars are cross-checked
  too: each `irhash` is registered in `mapping.json` and its `wav` exists.
  Warnings flag `guitar_settings` control keys that aren't controls on the
  target guitar's profile (case-insensitive; skipped when that guitar has no
  profile — it may lag) and IR tags outside the controlled vocabulary. Each
  problem line is prefixed with its tone's logical slug; a `tones/*.json` that
  isn't valid JSON — or that parses but is shape-invalid (the same
  deserialization check the loaders warn-and-skip on) — is a problem prefixed
  with its filename. Exits 1 if any
  problems are found, 0 if clean (warnings never change the code). `--json`
  emits `{"problems": [...], "warnings": [...]}` (both empty when clean).
- `helixgen library add-guitar <name> [--short-name SHORT] [--type
  guitar|bass]` — scaffold a new guitar profile at
  `library/guitars/<slug>.json` (schema 1: name, short_name, type; every
  other field null/empty for the setup skill — or a hand edit — to enrich)
  and auto-commit the home repo like every other library write. This is the
  core write path for new profiles — a profile JSON written directly by a
  skill would otherwise only get committed on core's next library write. A
  profile already at `slugify(name)` is refused (exit 1); edit the existing
  JSON instead (`library validate` checks it).
- `helixgen library import <file.hsp|dir> [--artist --song | --descriptor]
  [--guitar] [--keep-source]` — import an external `.hsp` (or every `*.hsp`
  in a directory) into the library. By default the source is **moved** into
  `library/tones/` under the resolved naming schema; `--keep-source`
  **copies** instead. A sibling `.md` (same stem) is folded into
  `description_md`; a missing `.md` leaves it `null` with a warning. Naming
  flags use the same identity rules as `generate` (exactly one of
  `--artist`+`--song` or `--descriptor`; with neither, the `.hsp`'s own
  `meta.name` becomes the descriptor) — for a **directory** import, per-file
  identity flags aren't allowed (each file is self-named from its own
  `meta.name`); `--guitar`/`--keep-source` still apply to all. A target slug
  that already exists is refused (exit 1) — the existing `.hsp` is never
  overwritten. A directory import is **atomic on naming collisions** (the
  whole batch is pre-validated and refused, moving nothing, on any collision)
  but **not** atomic on per-file errors during the move pass — an
  unexpected per-file error is recorded and the run continues, the manifest
  is always saved, and the command exits nonzero if any file failed; a
  failure after a file was placed (manifest registration) names the exact
  recovery command (`helixgen register <placed .hsp>`).
- `helixgen library migrate [--dry-run | --plan <plan.json>]` — one-shot,
  idempotent migration of a pre-library `~/.helixgen` into the tone library:
  moves each manifest tone's `.hsp` into `library/tones/<slug>.hsp` under the
  new naming schema, folds a sibling `.md` into `description_md`, writes the
  per-tone metadata JSON, and re-keys the manifest; each mapped IR WAV is
  **copied** (never moved) into `library/irs/<pack>/` with a scaffolded
  metadata sidecar and `mapping.json` rewritten to the library copy. It also
  **seeds a guitar profile** (`library/guitars/<slug>.json`) from each
  `preferences.instruments` entry, then **removes** the deprecated
  `instruments` / `preset_output_dir` keys from `preferences.json`, and
  reconciles `default_guitar` (warning — stderr + a `default_guitar_unresolved`
  summary field — if it no longer names a profile after seeding). `--dry-run`
  prints the inferred plan as JSON and mutates nothing; `--plan FILE` executes
  a (possibly agent- or user-edited) plan instead of re-inferring one; with
  neither flag, plans and runs in one go. A per-tone/IR error is recorded and
  the run continues; a tone whose `.hsp` already sits at its destination but
  whose metadata/manifest bookkeeping is incomplete (a prior run died mid-tone)
  is self-healed on re-run — file untouched, bookkeeping recreated; a slug
  collision (two tones mapping to one destination) is
  recorded with a rename suggestion and neither tone is moved. Output is a JSON
  summary of moves/skips/errors/collisions (including an `instruments` section).
- `helixgen library ir-backfill [--json]` — for every `mapping.json` entry
  whose WAV lives outside `library/irs/` or lacks a sidecar JSON: **copy** it
  into `library/irs/<pack>/` (never moved — paid packs stay in place), scaffold
  a metadata sidecar next to it, and rewrite `mapping.json` to the library
  copy. Idempotent — an entry already in-library WITH a sidecar is skipped, so
  a re-run is all skips. `--json` emits `{"backfilled": [...], "skipped": [...],
  "errors": [...]}`. Use once after adopting the library layout so
  already-registered (or `--no-copy`) IRs get metadata; the skill then enriches
  each sidecar's provenance and character tags.


## Device commands (`helixgen device …`)

With the `device` extra (`pip install 'helixgen[device]'` → pyzmq+msgpack)
helixgen talks to a **Stadium** over the LAN directly (OSC-over-ZeroMQ; no
editor app). Addressing precedence (0.24.0, workspace #74): `--ip` wins,
else `$HELIXGEN_HELIX_IP`, else the device record persisted by
**`helixgen device discover`** — there is **no built-in default IP**
anymore (the old baked-in `192.168.x.x` literal was the maintainer's own
DHCP lease: a guaranteed-wrong default for anyone else that failed as a
long connect stall). With none of the three available, verbs **fail fast**
with an instructive error naming `device discover`. An **empty or
whitespace-only `--ip`** (typically an unset shell variable that expanded to
nothing) is **rejected at parse time** with a clear message and a nonzero
exit — it is a mistake, not a request to fall back to the record; omit the
flag to fall back, or pass a real address (behavior change, backlog #77).
`--port` defaults to
the RPC control port **persisted by `device discover`** for the resolved
device — 2002 unless discovery saw the device advertise a nonstandard port
(backlog #77) — so a nonstandard-port device is reached automatically
without re-passing `--port` every verb; an explicit `--port` always wins.
(The telemetry verbs `tuner`/`meters`/`measure` stream on the fixed PUB port
2003 and use `--port` for their reachability preflight — see those verbs.)
Protocol reference:
[`helix-protocol.md`](helix-protocol.md).

#### `device discover` — find + persist the Stadium's address (0.24.0)

```
helixgen device discover [--timeout N] [--probe/--no-probe] [--json]
helixgen device discover --forget SERIAL-OR-IP [--json]
```

Run **once** (and again whenever the device's DHCP lease changes). Two
mechanisms, both verified on hardware (Stadium XL, fw 1.3.2, 2026-07-16):

1. **mDNS/Bonjour (primary).** The Stadium advertises the DNS-SD service
   `_stadiumserver._tcp.local.` and answers a one-shot multicast PTR query
   itself with PTR + SRV + A in a single datagram (instance `p35x1`, target
   `p35x1.local.`; the SRV port is 2001 — the change-stream port, not the
   RPC port). Pure stdlib — no zeroconf dependency. `--timeout` is the
   listen window (default 3 s; values below 0.5 s are floored to 0.5 s).
2. **Local-subnet TCP probe (fallback, `--probe`, default on).** For
   networks that block multicast: a bounded concurrent TCP connect-probe of
   the machine's **own subnet only** on RPC port 2002 (the device ignores
   ICMP). The range is derived from the interface's own **netmask**, not
   assumed to be a /24 — on a /22 (`192.168.4.98 netmask 0xfffffc00`) the
   probe sweeps `192.168.4.0/22`, all four /24s, where the old /24
   assumption silently missed three quarters of the network and still
   reported "no Helix Stadium found" (hc-3qw). Wider networks are **capped
   at 1024 addresses** (`MAX_PROBE_HOSTS`) around the machine's own
   address, so a /16 probes the enclosing /22 rather than 65k hosts; the
   fallback and failure messages both name the range actually probed. The
   netmask is read from `ip -o -4 addr` / `ifconfig`; where neither exists
   (Windows) the range falls back to the /24. Short per-connect timeouts,
   bounded concurrency, never probes beyond the local subnet — and it
   refuses to scan at all when the range is not RFC 1918-private (10/8,
   172.16/12, 192.168/16): connect-scanning a public subnet would be a port
   scan of strangers, not LAN discovery (backlog #77). `--no-probe`
   disables it.

**Known limitations (backlog #77):** both mechanisms look at the
**default-route interface** — with a VPN up that is usually the tunnel, so a
LAN-attached Stadium can be missed; disconnect the VPN for the one-shot
`discover`, or bypass discovery entirely with `--ip` / `$HELIXGEN_HELIX_IP`.
And the mDNS listener hears **unicast replies only** (it never joins the
224.0.0.251 multicast group): the Stadium honors the query's QU bit and
replies unicast (verified live, fw 1.3.2), but firmware that replied only
via multicast would be invisible to mDNS and fall through to the probe.

Every candidate is **confirmed** with the read-only `/ProductInfoGet`
handshake before being trusted; confirmed devices are persisted (ip, serial,
model, firmware) into the library-foundations per-device records
`~/.helixgen/devices/<serial>.json` — the same files sync observations live
in; discovery fields round-trip through sync rebuilds. Discovery is
read-only on the device: no lock scope is taken. When the mDNS SRV record
advertises a **nonstandard** stream port, the derived RPC port (one above
the advertised stream port — the observed 2001→2002 offset) is persisted too
(`port`) and every later verb reuses it as the `--port` default; a standard
device leaves the record portless (2002 implied). backlog #77.

**Why discover-once + direct-IP:** community prior art on the Stadium
desktop app is that its *discovery* layer is flaky while *direct-to-IP*
sessions are stable. helixgen therefore uses discovery exactly once to find
the device, persists the result, and keeps every session direct-to-IP.

**Multiple devices:** all found devices are listed and persisted; the
resolver deterministically picks the most recently discovered
(`ip_updated_at` desc, then serial desc) and warns when several records
disagree — pass `--ip` on any verb to target another. `--json` emits the
confirmed rows (`ip`, `serial`, `model`, `firmware`, `via` = `mdns|probe`,
`record` path, `default`).

**Pruning a stale record (`--forget SERIAL-OR-IP`):** removes the persisted
`~/.helixgen/devices/<serial>.json` record whose serial or `ip` matches the
argument, instead of discovering — use it when a device left the network for
good and you no longer want its address resolved. Matches serial or IP
exactly, never touches the network, and exits nonzero with a clear message
(not a traceback) when nothing matches or no records exist yet; `--json`
emits the list of removed record paths. backlog #77.
**Stadium-only**; these verbs **mutate the device** — prefer an empty/expendable
slot when testing. CLAUDE.md carries the concise verb list + the mental-model
rules (read-vs-mutate verb awareness, flaky-network, tone-library); this is
the full per-verb reference.

**`--setlist` accepts real setlist names (0.21.0).** Every preset verb that
takes `--setlist` (`list`/`backup`/`create`/`save`/`push`/`install`/`delete`/
`slots restore`) accepts `user` (the preset **pool**, container `-2`, where
every user preset actually lives — the default), `factory` (`-1`, read-only),
or a **device setlist display name** (case-insensitive, e.g. `Throwaway`,
`helixgen`) — the same names `device reorder`/`device sync` already took.
Setlists hold **references** to pool presets, so with a named setlist the
read verbs operate on its references and the write verbs put the preset
content in the pool + add a reference at `--pos`. The old closed
`user|factory|throwaway` choice is gone — the `throwaway` token used to map
to the setlists *root* (`-5`), which never worked (empty listings, rejected
writes); it now just names the setlist actually called "Throwaway".

### Device locks (machine-local, advisory — 0.22.0; detached leases + dangling-token checks 0.33.0)

Every device-**mutating** verb auto-acquires a **machine-local advisory
lock** for its duration, so concurrent helixgen processes on the same
machine (including agents nobody is orchestrating) never collide on the
device. Read-only verbs acquire nothing. Locks are **lease files** —
`~/.helixgen/locks/<device-ip>/<scope>.lock` (root override
`$HELIXGEN_LOCKS`; the default follows `$HELIXGEN_HOME` like every
other home subarea, and `locks/` is gitignored in the home repo), JSON `{pid, pid_start?, hostname, acquired_at, ttl_seconds, label,
token?, kind, nonce}` (`kind` is `auto` | `session` | `pid` | `detached`;
`pid` is **`null`** for a detached lease — 0.33.0. `pid_start` is the owner
process's start time as `ps` reports it, recorded at acquisition: pid numbers
are recycled, so lease identity is the **`(pid, pid_start)` pair** and a pid
whose current start time differs from the recorded one is a *different*
process, i.e. the owner is dead) — created atomically; the file is the source of truth
(no fcntl handle is held across processes, so shell-agent flows where every
CLI call is a fresh pid work). **Limitations (by design):** advisory —
nothing stops a `--no-lock` caller — and machine-local — direct-protocol
clients on **other hosts** and the **Stadium desktop editor are NOT
covered**. **Mixed versions:** the locking protocol arrived in **0.22.0** —
**pre-0.22.0 helixgen clients take no leases and ignore existing ones**, so
running an older client against the device concurrently with a ≥0.22.0
client (or another old one) is unsafe: it collides as if no locks existed.
Upgrade every helixgen on the machine to ≥0.22.0 before relying on locks for
parallelism.

**Scopes** (granular, so safe parallelism is possible):

| scope | covers |
|---|---|
| `editbuffer` | live-ops on the ACTIVE tone |
| `library` | pool / setlist / preset-content writes |
| `irs` | device IR writes |
| `globals` | Global Settings / Global EQ writes |
| `all` | exclusive: conflicts with everything (session lease for a whole run) |

A scope conflicts with itself and with `all`; different granular scopes
never conflict (e.g. one agent can run live-ops while another pushes IRs).

**Verb → scope table** (auto-acquired for the verb's duration; released on
exit, even on failure):

| scope(s) | verbs |
|---|---|
| `editbuffer` | `load`, `snapshot`, `bypass`, `model`, `set-param`, `normalize` (recalls snapshots / loads presets while measuring — even its dry-run) |
| `library` | `create`, `save`, `rename`, `delete`, `set-info`, `push`, `restore`, `reorder`, `setlist create/rename/delete/duplicate`, `setlist import-hss` (not `--list`/`--dry-run`) |
| `library` + `irs` | `sync` (`--exclude-irs` drops the `irs` scope), `install` (with or without `--auto-irs`: the IR presence check's wedge discriminator may issue a state-neutral rename nudge, an IR-container write — #93), `slots restore` (same reason, for `.hsp` sources) |
| `irs` | `push-ir`, `delete-ir`, `rename-ir`, `ir-prune` (only with `--yes`; dry-run takes nothing) |
| `globals` | `settings set`, `globaleq set` |
| *(none — offline)* | the local-manifest / offline verbs: `to-hsp` (with a `.sbe` SOURCE), `add`, `unsync`, `library`, `slots list` (without `--verify`), `slots reorder`, `setlist list/add/remove/create-local/sync-on/sync-off`, `local-list`, `settings list` (without `--values`), `globaleq list` (write-only verb group: nothing is read back), `setlist import-hss --list`/`--dry-run`; plus `lock`, `unlock` and `discover` — networked but deliberately exempt, so recovery is never locked out |
| *(none — but networked; see "A dangling token fails loudly")* | every read/list verb: `info`, `list`, `setlists`, `read`, `to-hsp` (with a CID SOURCE), `blocks`, `params`, `active`, `list-irs`, `settings list --values`, `settings get`, `slots list --verify`, `setlist export-hss`, `backup`, `pull`, `pull-ir`, `watch`, `tuner`, `meters`, `measure`, `ir-prune` (dry run) |

Read/list verbs take **no lease**, but since 0.33.0 the **networked** ones do
*verify* a presented `$HELIXGEN_LOCK_TOKEN` (see below) — as does a dry-run
mode that still reads the device (`ir-prune` without `--yes`). No token → no
check, exactly as before.

**Session leases — `device lock` / `device unlock`:**

- `helixgen device lock --scope <editbuffer|library|irs|globals|all> --label
  <text> [--ttl 900]` (scope repeatable; default `all`) — hold scope(s)
  ACROSS calls. Prints `HELIXGEN_LOCK_TOKEN=<token>`; **export it** and
  every covered verb passes through the lease instead of deadlocking against
  it — and **any** verb you run with the token exported (read-only ones
  included, 0.33.0) renews every lease that token owns. Calls from the
  **same shell** as the `lock` also pass through without the token (the
  lease records the invoking shell's pid). Re-locking your own scope renews it in place (idempotent) — and
  **switches its kind**: `--detach` over a session lease drops the pid,
  `--pid` re-binds it to the pid you name, a plain re-lock re-binds it to the
  invoking shell. A narrow re-lock under your own covering `all` lease
  converts *that* lease the same way, reported as `renewed 'all'` — never a
  silent passthrough that keeps the old kind.
- `helixgen device lock --scope all --pid $PPID --label "<who>"` — a
  **pid-bound** lease (0.33.0, #97b; `kind: "pid"`). **This is the lease an
  agent should take.** An agent's every tool call is a fresh shell, and a
  plain session lease records *that* shell's pid — when it exits, the lease is
  reclaimable after the 120 s grace and a contender takes the device
  mid-workflow (observed twice on hardware 2026-07-27). `--pid` binds the
  lease to a process **you** name that spans the whole workflow and dies with
  it: from a tool call, `$PPID` is the long-lived agent (`claude`) process.
  Liveness is then **decidable**, so:
  - the **120 s dead-pid grace does not apply** (a `session` lease's pid may
    be a short-lived wrapper; an explicit `--pid` is a deliberate choice, so
    its death is conclusive and the lease is reclaimable at once);
  - the **TTL demotes to a backstop**: it keeps the ordinary **900 s**
    `DEFAULT_SESSION_TTL` and every token-carrying verb renews it, so only an
    IDLE stretch longer than the TTL loses the lease — and where liveness
    cannot be probed at all (a lease recorded on another host, or Windows)
    the TTL is the only reclaim path there is.

  Pid liveness is **POSIX-only**: on Windows the probe is refused outright
  (`os.kill(pid, 0)` terminates the target there), so a `--pid` lease is
  TTL-bounded exactly like a detached one and a dead `--pid` is **not**
  refused at acquisition.

  Passthrough for a `--pid` lease is **by token**, not by shell: the recorded
  pid is the process you named, not the calling shell, so the same-shell
  passthrough that a plain session lease gets does not apply — export
  `HELIXGEN_LOCK_TOKEN` (that is the mechanism anyway, since an agent's every
  tool call is a fresh shell).

  A `--pid` whose process is **not alive** at acquisition is refused (a lease
  for a dead owner is stale the moment it is written). Mutually exclusive with
  `--detach`. Release with `device unlock` at the end of the workflow.
- `helixgen device lock --detach --label <text> [--ttl 300]` — a **detached**
  lease (0.33.0, #97): identical to the above except it records **no pid**
  (`kind: "detached"`), so it does **not** die with the shell that took it.
  Use it when **no process spans the workflow at all** (cron, CI); when one
  does, prefer `--pid`. Trade-off:
  with no pid to probe, the **TTL is the only automatic reclaim path**, so the
  default TTL is **300 s** rather than 900, and **`--ttl 0` is refused** with
  `--detach` (no pid *and* no expiry = a lease only `unlock --force` clears).
  Every verb you run with `$HELIXGEN_LOCK_TOKEN` exported renews it —
  **read-only verbs included** (0.33.0: a read is proof the session is
  active, so it renews the lease it opens) — so an active workflow keeps its
  lease and an abandoned one expires by itself. Release with `device unlock`
  (token) — nothing else will.
- `helixgen device lock --status [--json]` — inspect the device's leases:
  scope, label, owner, host, age, TTL, live/stale, ours. The owner names the
  lease **kind**: `detached` (no pid at all), `pid <n> alive` / `pid <n> dead`
  for a **pid-bound** lease on this host, plain `pid <n>` for a session lease
  or a pid recorded on another host (unprobeable — no liveness is claimed).
  `--json` carries the same distinction as `kind` plus `pid_alive`
  (`true`/`false`, or `null` when liveness is not decidable). Read-only,
  exit 0.
- `helixgen device unlock [--scope <s>]... [--force]` — release your leases
  (all of them without `--scope`). An explicit `--scope` you don't own is an
  error unless `--force` (which breaks even a live foreign lease —
  dangerous). Foreign leases are otherwise reported and left alone. A scope
  re-acquired by another owner in the instant between the ownership check and
  the unlink is treated the same way — never clobbered (errors for an explicit
  `--scope`, kept without error for a bare `unlock`).

**Contention:** a blocked acquire waits up to `$HELIXGEN_LOCK_TIMEOUT`
seconds (default **30**; `0` = fail fast) with polling backoff, then exits
non-zero naming the holder the same way `--status` does — label, owner
(`detached` / `pid <n>, alive` / `pid <n>, dead` / `pid <n>`), host, age, TTL
— so a contender can tell a live agent it should wait for from a corpse. **Staleness:** a lease
whose TTL expired or whose recorded pid is dead (same host) is reclaimed
with a stderr warning (stale-breaks are serialized through a break-mutex
file and re-verified under it, so a renewed/re-acquired lease is never
broken); a **live lease is never broken** implicitly. Lock *acquisition*
itself (the scan→create→verify step) is serialized per device through a
stale-breakable `.acquire.lock` meta-lock so two racers taking conflicting
scopes can't both commit; like the break-mutex it is reclaimed if its holder
dies and is invisible to `device lock --status`. Escape hatch: every
**mutating** verb takes `--no-lock` (dangerous — you're opting out of
collision protection), which since 0.33.0 also skips the dangling-token
check below. Read-only verbs take no lease and carry **no `--no-lock`** —
the only deliberate opt-out for a read is `unset HELIXGEN_LOCK_TOKEN`. (The
one seam: `ir-prune`'s dry run is guarded as a read but *is* a mutating verb
narrowed by `--yes`, so it does carry the flag.)

Fine print: `--ttl 0` = no TTL expiry (reclaim then relies on pid-liveness
or `device unlock`) — and `0` is its **only** spelling: a *positive* TTL
under **10 s** is refused outright (renewal skips a lease within 2 s of
expiry, so a shorter one would lapse mid-workflow however actively you used
it), as is a non-finite one and a **negative** one (`--ttl -5`, the typo for
`--ttl 5`, used to slip past the too-short guard and silently produce the
no-expiry lease). A
**session** lease whose recorded pid is dead gets a
**120 s grace** (from its last acquisition/renewal) before pid-death makes
it stale — so run `device lock` from your long-lived shell, not via a
wrapper script (the wrapper's pid dies immediately; the lease then only
survives while token-carrying calls keep renewing it), or name a process that
outlives the call with `--pid` (**no** grace: pid death is conclusive there),
or take `--detach`, which
records no pid at all and is bounded by its TTL alone. Pid-liveness is
POSIX-only:
on Windows it is disabled (probing would kill the probed process) and only
TTL staleness applies. Lease files are `0600` (the token is a private
capability).

**A dangling token fails loudly — read-only verbs included (0.33.0, workspace
#97).** Setting `$HELIXGEN_LOCK_TOKEN` is an explicit declaration of "I am in
a held session". If that token opens **no live lease at all** on the device
(the lease was reclaimed by a contender or its TTL lapsed), the verb
**errors** naming the current holder instead of proceeding unlocked. This
applies to **read-only** verbs too — `measure`, `meters`, `tuner`, `blocks`,
`params`, `active`, `watch` (editbuffer); `info`, `list`, `setlists`, `read`,
`pull`, `backup`, `setlist export-hss`, `slots list --verify` (library); `list-irs`,
`pull-ir` (irs); `settings list --values`, `settings get` (globals) — plus
`ir-prune`'s dry run — because a read taken while
someone else is driving the device is no more trustworthy than a write (the
2026-07-27 workflow was denied `device snapshot 0` and then handed a
well-formed `device measure` of whatever snapshot happened to be active).
Verbs invoked with **no token** are unchanged: unlocked reads stay free and
take no lease. `device lock` / `device unlock` / `device discover` and the
offline verbs are exempt (including `settings list` without `--values` and
`setlist import-hss --list`/`--dry-run`), so recovery is never locked out.
A scope simply **outside a narrow lease** is *not* a lost session: holding
`--scope library` and running an `editbuffer` verb acquires that scope
transiently, exactly as before (a granular lease stays granular). The one
exception is a **read** of a scope a live foreign lease holds *right now* —
that errors too, whatever else your token opens. A mutating verb contends
for the scope and is refused visibly; a read has no such fallback, and
would otherwise hand back well-formed data for a scope someone else is
driving. This is also how a MULTI-scope lease that lost one scope surfaces
once its lease file is gone or re-acquired: there is no way left to tell "I
lost it" from "I never held it", and the read is untrustworthy either way.
While the **expired lease file is still on disk**, though, it is proof the
scope was yours and lapsed — that case errors for mutating verbs too, not
only reads. A live lease within **2 s of expiry** counts as lost as well:
nothing renews a lease that close to the boundary, so entering a verb on it
would mean running unlocked seconds later. Losing a lease *during* a call
(a long `measure`/`watch`/`normalize`) can't be caught by the entry check —
the background heartbeat notices and prints a `lapsed DURING this call`
warning to stderr; treat it exactly like a lock error. That warning fires on
losing **any one** of the leases held at entry, not just the last one, so a
multi-scope session that drops a single scope mid-call still says so.

Leases are keyed by **device address**, and so is this check: a token whose
only live lease sits under a *different* address — a second Stadium, or the
same one reached as `helix.local` here and `10.0.0.4` there — is **not** a
lost session and does not error (a scope a live *foreign* lease holds right
now is still a refusal, as above). Verbs against that other address behave as
they did unlocked (they take no lease there, since the lease you hold is
keyed elsewhere); to actually hold it, take a lease under the address you
will be using, and keep using that same spelling for the whole session. Reads
say so on stderr — `your $HELIXGEN_LOCK_TOKEN holds a lease under device
address '<other>', not '<this>' … this read is UNLOCKED` — because a token
that holds nothing here is the #97 failure one address-spelling apart. When
that refusal *does* fire (a foreign holder on the scope you are reading), the
error names the other address too and never claims your session was reclaimed:
it is alive, just keyed elsewhere, so `device unlock` would throw away a live
lease.

**Operating rule for an agent driving multi-call device work (workspace #97):**

1. Take a **pid-bound** lease up front — `device lock --scope all --pid $PPID
   --label "<who>"` — and export the printed `HELIXGEN_LOCK_TOKEN`. A plain
   session lease dies with the tool call that took it; `$PPID` is your
   long-lived agent process, which does not. (No such process — cron, CI? Use
   `--detach`.)
2. `device unlock` when the workflow ends (including on failure). Otherwise
   only your process's death (pid lease) or the TTL clears it.
3. **Treat any lock error as "stop and re-establish state", never "retry the
   failed call and continue".** Once a lease is lost, the device may have been
   driven by someone else: re-take a lease and **re-read** whatever you were
   about to act on (active preset, snapshot, block state) before acting. A
   blind retry of the failed call can succeed against a device that has since
   moved.
4. Every verb that **takes or checks a device scope** — the mutating verbs and
   the read-only ones in the table above — renews **every** lease that token
   owns, not only the scopes that verb touches, so a stretch
   of `library` verbs cannot let a sibling `editbuffer` lease of the same
   session age out. A verb that runs LONGER than the TTL (`normalize`, a long
   `--seconds` window) keeps renewing **in flight**, from a background
   heartbeat, so an *active* workflow cannot time out either way. A workflow
   that goes **idle** for longer than the TTL still
   loses it: size `--ttl` to cover your longest gap. **The exempt verbs count
   as idle** — `device lock --status`, `device unlock`, `device discover` and
   every offline verb (`slots list` without `--verify`, `settings list`
   without `--values`, `library`, `local-list`, …) renew nothing at all, so
   polling `device lock --status` is *not* a keepalive.
5. `device unlock` releases the lease but cannot unset `$HELIXGEN_LOCK_TOKEN`
   in your shell — a token that opens nothing makes every later device verb
   refuse, so unset it (the verb says so on stderr) or take a fresh lease.

### Preset + edit-buffer verbs

- `helixgen device list [--setlist <user|factory|NAME>] [--json]` — presets in the pool (`user`, default) or factory; with a named setlist, its **references** (each row: position, the reference's own cid, `rcid=` the pool preset it points at, name).
- `helixgen device setlists [--json]` — the device's setlist containers.

**Every verb below that creates a preset via `/CreateContent`** (`save`, `push`, `install`, and `slots restore` — **not** `device create`, which copies an existing preset with `/AddContentsToContainer` and gets none of this) resolves the reply the same way (#38, root-caused 2026-07-19). A **non-zero status code** is **not** a failure — field 3 of that reply is the device's edit-buffer dirty flag, not an error code — and neither is **no `/status` reply at all** (a dropped frame on the flaky Stadium stack says nothing about whether the write landed). The client **confirms by re-listing** the container (bounded retries, strict, under a 2001 subscription) and proceeds with the re-listed cid; only content **genuinely absent** after those retries raises, and that path deletes **nothing** (the old self-cleaning cleanup is exactly what destroyed creates that had landed). **Read the error before retrying** — it distinguishes "genuinely absent" (safe to retry) from "the listing could not be read at all" and "listed but no cid reported", where the content may well be there and a blind retry duplicates it. On the **preset** paths (unlike `setlist create`, whose deliverable is an empty container) anything `device list` then shows at that slot is an **EMPTY stub**, not a saved tone: the create landed but the content write never ran, so delete it before retrying.

- `helixgen device info [--json]` — the device's identity over the network: model (+ helixgen chassis key), numeric device id, serial, firmware version/build/date, SD storage free/total (`/ProductInfoGet`; read-only, never touches presets or the edit buffer).
- `helixgen device active [--json]` — the device's **ACTIVE preset**: cid, name, and pool slot (reads the live property `server.active.preset.id` — it tracks the player's own panel selection as well as network loads — then resolves the cid via the read-only `/GetContentRef`; live-verified 2026-07-15, fw 1.3.2). This is how an agent saves/restores the player's selection: note the cid, do your work, `device load <cid>`.
- `helixgen device read <cid> [--json]` — a preset's metadata (name/slot/parent).
- `helixgen device load <cid>` — load a preset into the edit buffer.
- `helixgen device create --from <src_cid> [--setlist <dest>] --pos <N>` — no positionals; both options required. Into the pool (default): a **copy**, auto-named by the device after the source (`"<Name> (1)"` style — live-verified; rename with `device rename`). Into a **named setlist**: no copy — a **reference** to the source pool preset is added at `--pos` (the printed cid is the reference's own).
- `helixgen device save <name> [--setlist <dest>] --pos <N>` — save the live edit buffer as a new preset (slot must be empty; the emptiness check is strict — backlog #40 — a listing timeout aborts the save rather than reading the slot as empty). With a named setlist: saved into the pool (lowest empty slot) + referenced at `--pos`.
- `helixgen device rename <cid> <new_name>` — rename a preset.
- `helixgen device delete <cid> [--setlist <dest>] [--yes]` — delete a pool preset; with a **named setlist**, remove only the setlist's reference (`<cid>` may be the reference's cid or the referenced pool cid) — the pool preset is never touched.
- `helixgen device set-param <path> <block> <param_id> <value>` — set one edit-buffer param (`/ParamValueSet`). `<block>` is the `device blocks` coordinate — the DSP **grid slot**, sent to the wire unchanged (0.21.0 erratum, HW-proven 2026-07-15: the old `(key-1)/2` translation of the block's list position only coincided with the true slot for contiguous chains, which is why the output block was unaddressable). Discover `<param_id>` with **`device params <path> <block>`** — never guess pids. `<value>` is in the param's **raw units** (dB/Hz/enum-int, exactly as `device params` reports), not normalized. Proven live example: `helixgen device set-param 0 13 2 3.0` (output block at grid slot 13, `gain` pid 2, 3 dB). **The value is validated against the range `device params` advertises for that pid** (bounds inclusive) and the write is refused when it falls outside — the device clamps nothing, so a typo'd value would otherwise land and be faithfully applied (HW-measured 2026-07-30, fw 1.3.2: `set-param 0 13 2 25` on a gain advertised `f [-120..20]` read back as 25.0 and added +5 dB of real level). Validation costs one edit-buffer read per write; a pid the defs advertise no min/max for is not checked. **`--force`** writes out of range anyway — the advertised range is the *vendored model defs'*, which can be narrower than the firmware's. That hardware headroom is deliberately **not** used to lift the +20 dB output-level ceiling (see `docs/BACKLOG.md` "hardware output gain exceeds the +20 schema cap").
- `helixgen device params <path> <block> [--json]` — one edit-buffer block's params: numeric **pid**, name (from the vendored model defs), **current value** (RAW units), type, range, default. The pid-discovery surface for `device set-param`. Read-only.
- `helixgen device blocks [--json]` — list the **live edit buffer's blocks** with their `(path, block)` coordinates — `block` is the DSP **grid slot** (0-27, not necessarily contiguous: outputs sit at 13/27, the hidden second input at 14), model name, and saved base on/off. Read-only. These are the coordinates `device bypass`/`device model`/`device set-param`/`device params` address.
- `helixgen device pull <cid> <outfile.sbe>` — back up a preset's raw content blob.
- `helixgen device to-hsp <file.sbe|cid> -o <out.hsp> [--name N] [--author A] [--library D] [--no-verify]` — **the reverse transcoder: device content (`_sbepgsm`) → a helixgen `.hsp`.** The inverse of `install`, so a preset authored **on the hardware or in HX Edit** stops being an opaque blob and becomes a real `.hsp` that `view`, `patch` and the surgical edit verbs all handle and that `install`/`sync` push straight back. SOURCE is either a local `.sbe` path (**wholly offline** — no device, no lease, no dangling-token check) or a device **CID** (an integer; read via the non-activating `/GetContentData`, so the live tone is never disturbed, and the preset's device name becomes `meta.name` — the content blob itself carries no name). Fidelity covers models, params, snapshots (per-scene bypass + param deltas), footswitch/EXP assignments, IR references, base bypass, input routing + impedance, output level/pan, scribble strips, **every model slot of a dual-cab block** (the B cab's own mic/level/EQ — bead hgc-q38), and the full signal graph (dual-DSP, parallel splits, dual-amp). Every mapping is the inverse of a specific piece of `device/transcode.py` + `device/bridge.py` — which is what makes `.sbe → .hsp → .sbe` a free oracle. Design + the full residual-diff analysis: [`docs/superpowers/specs/2026-08-12-sbe-to-hsp-reverse-transcoder-design.md`](superpowers/specs/2026-08-12-sbe-to-hsp-reverse-transcoder-design.md).
  - **`--library`** (default `~/.helixgen/library`) supplies the helixgen **param-name vocabulary**, and the per-block param **order** in it is load-bearing, not cosmetic: the forward transcoder allocates `cg__` target ids while walking a block's params in `.hsp` dict order, so emitting them in device-pid order instead shuffles every downstream id. A model with no library block falls back to the device's own param names — still round-trips, but reads less like a helixgen export and `show-block` won't know the names.
  - **`--verify`** (default on) re-transcodes the `.hsp` and reports whether it reproduced the source bytes. Content **helixgen itself installed** comes back **byte-exact** (61 of the 66 real device blobs in the development corpus). Content the **device re-saved** differs in device-side serialization conventions only — `pm__` key order, the `snps` array's `si__` ordering, msgpack map-key order inside `ctrl` records, `cg__` target/source/controller id numbering (plus the block `id__`/`bmap` permutation a hardware reorder leaves behind), and the `hist`/`selb`/`self` edit-buffer scratch keys. None of that is tone: the conversion is a **canonicalization** that reaches a **fixed point after one pass** — with one stated exception, a preset whose row-0 blocks sit on non-contiguous grid slots, which `install` compacts (warned about on stderr). `tests/test_untranscode.py` pins both the fixed point and an **id-agnostic semantic-equivalence projection** (blocks by grid slot, params by pid, snapshot scenes and controller assignments resolved through the target graph) across the whole corpus.
  - **NOT yet reversed:** Command Center commands (#16) and MIDI CC controller bindings (#33) are **dropped, with a warning naming how many**. Both are EXPERIMENTAL in the forward direction and neither appears in the validation corpus. Per-block `harness` state is emitted only for the input/output endpoints (matching a real export); a user block's `hrns` params and its `hrns.enbl` are not carried, because the forward path ignores `.hsp` `harness` entirely and synthesizes a canonical `hrns` — so round-trip fidelity is unaffected either way. **Everything else that cannot be carried is reported on stderr, one line per loss** (a snapshot or controller assignment on a dual-cab's B model slot — the models and params of every slot ARE carried, only the `cg__` target has no forward spelling, bead hgc-3yc — a disabled DSP path, a row-1 input that would revert to `InputNone`, a non-contiguous grid run that a re-install would compact, a controller source with no `.hsp` id, a snapshot target missing from some snapshots' `tamv`). Silence means nothing was dropped.
  - Float values come back **widened from float32**: the device only ever stored `0.15` as `0.15000000596046448`, so that is what the `.hsp` carries. It re-encodes to the identical float32, so the round trip holds; it just looks noisy in `view`.
- `helixgen device push <file.sbe> <name> [--setlist <dest>] --pos <N>` — install a local content file into a new slot (restore/clone; the slot-emptiness check is strict — backlog #40 — a listing timeout aborts rather than reading the slot as empty). Named setlist: pooled + referenced at `--pos`. The `.sbe` is recorded as the tone's local source, and every consumer reads it as **device content, not as a `.hsp`**: `ir-prune` decodes it for IR references, and `device sync` re-pushes those bytes **verbatim** (the blob already IS the device's stored-content format, so there is nothing to transcode). Before that, a `.sbe`-sourced tone failed `not a .hsp file (missing rpshnosj magic)` on every sync, forever.
- `helixgen device restore <file.sbe> <cid>` — overwrite an existing preset's content from a file.
- `helixgen device backup [--setlist <user|factory|NAME>] [--dir <D>]` — pull the pool (default) — or the pool presets a named setlist references, in setlist order — to local `.sbe` files + `manifest.json` (offline backup).
- `helixgen device local-list [--dir <D>]` — list locally backed-up presets (works with the Helix disconnected).
- `helixgen device watch [--seconds N] [--filter <addr>]` — stream the device's live property/telemetry events (2001/2003).
- `helixgen device set-info <cid>... [--color <name|0-11>] [--notes <text>]` — set preset **color** and/or **notes** on one or more CIDs (batch-capable). Color is the `colr` content attr (int enum; names `auto, white, red, dark orange, light orange, yellow, green, turquoise, blue, violet, pink, off` — order inferred from the app menu, pass the raw index if a name renders unexpectedly). Notes are the Preset Info text, stored as the `preset.meta.info` property inside the content blob and written via a **non-activating** content round-trip.
- `helixgen device install <preset.hsp> <name> --pos <N> [--setlist <dest>] [--auto-irs]` — **author a helixgen `.hsp` onto the device as a new, playable preset** (named `--setlist`: pooled at the lowest empty slot + referenced at `--pos`) (the `/tone` → on-your-amp path). **Transcodes** the `.hsp` straight into the device's native content format (`_sbepgsm`) via `device/transcode.py` and `/SetContentData`s it into the empty pool slot (the slot-emptiness check is strict — backlog #40 — a listing timeout aborts rather than reading the slot as empty) — **no template, any block chain, full fidelity** (models/params/IRs); model/param names bridge helixgen↔device via `device/modelmap.py` + `device/defs.py`. Synthesizes the **full signal graph** — dual-amp / dual-DSP, **intra-flow parallel splits**, **snapshots** (per-scene bypass + param deltas), and **footswitch/EXP assignments** all transcode faithfully onto the device's real 28-slot grid (hardware-validated byte-for-byte vs HX Edit's own import, 2.18.0). `--auto-irs` uploads any IRs the preset references that aren't already on the device (resolving each `irhash` to a local WAV via `mapping.json`, then `push-ir`). Each `push-ir` registers instantly under the preset's `irhash` (via the `HASH` chunk + 2001 subscription — see `push-ir` below), so the installed preset's cabs resolve immediately with no editor step. **A *wedged* IR (backing file resolves, no `-11` registry entry) is detected via a confirmed listing refresh, reported missing, and re-pushed — the re-push removes the orphaned file and re-imports (self-heal, #93). Only when the refresh can't be confirmed (empty or failed `-11` listing, the single-wedged-IR case) does the wedge still read as already-present, leaving that cab silent with a stderr warning — `device delete-ir --force-wedge` is the sure clear then; see the `device list-irs` entry.** EXPERIMENTAL.

### Live device ops (mutate the ACTIVE tone)

These live-ops verbs mutate the ACTIVE tone (decoded + HW-validated 2026-07-14).

- `helixgen device snapshot <index>` — **recall a snapshot** (0-based, 0..7) on the live device (`/activateSnapshot`; absolute index) — changes the ACTIVE tone's snapshot immediately, like stepping the snapshot footswitch.
- `helixgen device bypass <path> <block> <on|off>` — **bypass/enable a block** in the live edit buffer (`/BlockEnableSet [dsp, grid_slot, enable]`; the `device blocks` coordinate — the DSP grid slot — goes on the wire unchanged; 0.21.0 erratum to the 2026-07-14 `(key-1)/2` finding, which only held for contiguous chains. The device echo alone is NOT proof a toggle landed — it happily echoes a toggle of an empty slot; the meters are ground truth). The toggle is *volatile* (audible at once, not written to the preset until you save, so `device blocks` won't reflect it).
- `helixgen device model <path> <block> <model>` — **swap a block's model** live (`/ModelSet [dsp, grid_slot, sub, modelId]`; grid slot unchanged, like `bypass`). `<model>` is a numeric model id or a model-id string like `HD2_AmpBritPlexiNrm` (see `list-blocks`). The device rejects a cross-category swap; the app's re-attach-controllers + push-defaults cascade is not replayed.
- `helixgen device reorder <setlist> <target> --to <N>` — **move a preset to a new position within a setlist** (`/ReorderContainerContent [container, [cids], newPos]`, decoded 2026-07-14, HW-validated). `<setlist>` is a setlist display name (resolved the way `device setlist rename/delete/duplicate` resolve setlists) or a literal container cid (`-2` = the pool, whose `cctp==PRESET` entries also resolve by their own names); `<target>` is a preset display name or a literal cid within it. Pass `setlists` as `<setlist>` to instead reorder the top-level setlist list itself (`<target>` is then a setlist name/cid) — the keyword is checked before name resolution, so a real setlist literally named "setlists" must be addressed by its container cid. **Numeric arguments are cid-first**: a purely-digit `<target>`/`<setlist>` is always parsed as a cid, never a display name. If an item is display-named that digit string, the cid reading wins with a stderr/result **warning** when the cid itself resolves in the container, and the command **errors** (pointing at the named item's real cid) when it doesn't. `--to` is bounds-validated against the container's current length before anything is sent. A **total reply timeout** (no `/error`, no `/status`, no update frame) raises instead of silently re-listing as if the move succeeded; a partial reply still falls back to a bookkeeping re-list because a reqid-correlated frame proves the device processed the write. **This is a direct, immediate DEVICE-side write** — distinct from the local-manifest `device slots reorder`, which only edits the tone library's recorded order and takes effect on the device on the next `device sync` (which can then reorder things right back to the manifest's order).
- `helixgen device tuner [--seconds N] [--json]` — **live network tuner** (no Stadium app, no hardware-tuner engage needed). The Stadium runs an always-on background pitch detector and streams it on 2003 as `/dspEvent {eid_:10,mid_:796}` = a single **fractional-MIDI** float (int = note, frac×100 = cents, `-1` = silence). Prints a live note/cents/Hz readout with an in-tune meter; `--json` emits one reading per line. HW-validated (stream+decode); pitch math golden-tested. Reachability is **preflighted** (one cheap TCP probe of the `--port` control port, #64c) — an unreachable/powered-off device fails fast with a clear error instead of streaming silence for the whole window (the SUB socket connects lazily and can't tell a dead host from a quiet one).
- `helixgen device meters [--seconds N] [--json]` — **live network level meters** (no Stadium app needed), read-only. Same always-on `/dspEvent` burst as the tuner also carries two grid-level meter arrays, `{eid_:1,mid_:796}` and `{eid_:1,mid_:800}` — each a **128-float** array — which this decodes into a live bar readout; `--json` emits one reading per line (`{mid, peak, values}`). HW-characterized 2026-07-14: the grids are **live per-node audio envelopes** at ~10 Hz per mid (linear amplitude, >1.0 legal) — mid 796 carries the path chain nodes (cells 0–1 = instrument input), mid 800's populated cells are the output-send pairs (= chain-out level); all taps sit **upstream of the output block's `gain`**. Full per-layout cell map still open (backlog #62). Same reachability preflight as `tuner` (one TCP probe of the `--port` control port; fail-fast on an unreachable device, #64c).
- `helixgen device measure [--seconds N=10] [--min-playing N=40] [--source input|loop] [--json]` — **measure how loud the ACTIVE tone is while the player plays**, read-only. Reduces the playing-gated telemetry (real pitch + non-silent input; hum/silence ignored — single-coil hum defeats level-only gating but reads `-1` on the pitch stream) to robust dB stats: instrument input, chain-out (median + p75), and the input-invariant **chain gain** (out/in) — the number to compare across snapshots/presets when level-matching. **`--source loop`** (workspace #82, core half): when a **front-of-chain looper** replays a recorded signal, the input-jack gate is structurally silent (no pitch, no input level — every sample would gate out), so loop mode gates on **chain-out level** instead (`measure.is_playing_loop`, floor `LOOP_OUTPUT_FLOOR`); `gain_db` is `null` (no input reference) and the number to compare across targets is the raw **`output_db`** — the looped source is identical across targets by construction. The `--json` result carries a `source` field. Tell the player to play steadily; exits 1 (JSON `ok:false` + `reason`) when the window had too little actual playing (~10 gated samples/sec of playing; default needs ~4 s). The reported `seconds` is the window **actually sampled** — a Ctrl-C'd partial window reports its true elapsed time, not the requested `--seconds` — and `playing_seconds` derives from the window's **observed** sample rate rather than assuming the nominal 10 Hz (#64d). Same reachability preflight as `tuner`/`meters` (one TCP probe of the `--port` control port; fail-fast instead of a full silent window ending in "no meter data", #64c). Loudness-feedback spec phase 1; `device normalize` (below) is the closed loop built on it.
- `helixgen device normalize [<preset.hsp> | --setlist <name>] [--target-db X] [--measure-via meters|capture] [--capture-input NAME] [--capture-channels 1] [--capture-remix SPEC] [--capture-skip 2.5] [--capture-dir DIR] [--seconds N=10] [--min-playing N=40] [--tolerance-db 1.0] [--source input|loop] [--yes] [--json]` — **level-match snapshots or a whole setlist by measuring while the player plays** (loudness spec phase 2, backlog #62). **Every one of `--target-db` / `--seconds` / `--tolerance-db` / `--source` / `--measure-via` / `--capture-input` falls back to the `normalization` block in `preferences.json` when you don't pass it** (he-xth): the resolution is *flag > preferences > the option's own default*, decided by click's parameter source, so the advertised defaults in `--help` stay true. `--json` reports `settings_from`, naming each setting's origin (`flag` / `prefs` / `default`) — a run whose target came from a profile is otherwise indistinguishable from one that anchored itself. `normalization.mode` also picks the source (`looper` implies `--source loop`), and a `sample`/`looper` run says so when the rig is NOT calibrated at all (the stimulus then plays at whatever the system volume currently is — snapshots within one preset still balance against each other, but an ABSOLUTE target and cross-session comparison both depend on source level), or warns when an existing calibration is stale (older than 90 days, or taken with a guitar that isn't your `default_guitar`). Set the block up with **`device calibrate`** (below). **A profile scaffolded by `preferences.scaffold_default` carries `target_db: 17.5`** (note: nothing in the CLI calls that today — the `setup` skill writes the block, so an existing profile keeps whatever it has until that skill brings it up to date) with its provenance in `target_source` — the shipped reference, measured on the factory *Stadium Rock Rig* (17.51 dB total, 2026-07-29, Stadium XL, meters metric). It is a CONSTANT, not a per-rig measurement: the factory presets are identical across Stadiums, and separate runs only land on a common level when they all use the same number. Left unset, the run anchors on its own first target instead, which level-matches WITHIN one preset and leaves separate runs nowhere near each other. **`sample` is the DEFAULT mode and the stimulus ships INSIDE the package** (`helixgen/assets/helix-cal-loop.wav` — 10 guitar-DI notes, exactly 5.00 s at 48 kHz, CC0), so a caller who has configured nothing still replays a fixed loop rather than being asked to hand-play a window per target on every run; `normalization.sample.path` overrides it. When the loop cannot be played at all (no `sox`), the run says so and measures your playing instead of failing. **This verb PLAYS the stimulus itself, at the CALIBRATED volume** (uncalibrated, it pins a deterministic 50 rather than inheriting the system slider — a machine left at 100% drives the jack far hotter than any guitar and silently invalidates the whole run) (`normalization.sample.volume`, set before each window and restored after — without that, every run measures at whatever the system volume happens to be, which is the one variable the calibration exists to pin down; a volume that cannot be set warns rather than silently drifting) — one pass of `normalization.sample.playback_cmd` around each measurement window (per window, not per run: the recalls and preset loads between targets are not measured, and playing through them buys nothing while making each window a different stretch of the loop). The stimulus and its player are preflighted BEFORE the first target, a `sample` profile with no `sample.path` is a usage error naming `device calibrate`, and `--no-stimulus` opts out when you are driving playback yourself. The per-target prompt changes accordingly — a sample-mode run tells the player to leave the rig alone, not to play. **A meters target is refused in capture mode:** integrated LUFS is at or below 0 by construction, so a positive `--target-db` came from the chain-gain metric — applying it under `--measure-via capture` would ask every tone for tens of dB and slam every output level to the cap, so the run errors instead (naming whether the target came from a flag or from preferences; the two settings are stored independently and changing `measure_via` does not convert one into the other). A `--seconds` window too short to reach `--min-playing` also warns up front, since every target would otherwise be silently SKIPPED. The closed loop over `device measure`: recalls each target (snapshot scope: each NAMED snapshot of the local `.hsp` — the device's ACTIVE tone must be that preset; its name is **verified** via the active-preset property before anything is measured and a mismatch aborts, an unverifiable name only warns; setlist scope: loads each manifest tone by its observed CID and verifies the loaded preset's name matches the tone — a mismatch means a stale observation and that tone is SKIPPED), prompts the player per target, and computes each target's dB trim so its **total loudness** — which IS the measured value, because every measurement path sits **downstream** of the output block's gain and therefore already contains the trim in force (adding the output level on top double-counted it and made the loop oscillate; hc-daz) — matches the **anchor**'s total (the first target that measured ok) or an absolute `--target-db`. Sizing trims from totals makes the loop **idempotent**: a re-run (same playing) computes in-band zero trims instead of compounding, and hand-balanced output overrides that already equalize are left alone. Deltas within `--tolerance-db` are in band and left alone (don't chase meter noise). **DRY-RUN by default** — measuring happens, trims are only reported; `--yes` writes them into the **local `.hsp` file(s)** (the source of truth) as output-block `level` moves: per-snapshot overrides (snapshot scope) or a whole-preset shift of base + any per-snapshot array (setlist scope; the uniform shift preserves the preset's own scene-to-scene and path-to-path balance). The device copy is NOT written — run `device sync <setlist>` / `device install` afterwards. If a mid-run write fails, the error lists the files already written (a re-run is safe — written files re-measure in band). Targets that can't be measured (too little playing, no local `.hsp`, no observed placement, name mismatch) are SKIPPED with a warning and the run exits 1 to flag the partial result. A setlist run restores the player's previously ACTIVE preset afterwards (best-effort); snapshot scope restores the preset's on-load snapshot. The output block's `level` is dB-native so each trim is exact in one move, and because the taps are **downstream** of it a written trim IS visible once the device copy is rebuilt (`device sync` / `device install`) — re-measuring is a valid way to **confirm** a trim, and re-running the whole loop reports in-band zeros rather than compounding. **`--measure-via capture`** (hc-57h) swaps the metric for a perceptual one: instead of the telemetry meters, each target is recorded off the Stadium's **USB audio output** with `sox` and reduced to **BS.1770 integrated LUFS** (`helixgen.audio_capture` → `helixgen.audio_metrics`). USB is downstream of the output gain too — measured on hardware (Stadium XL fw 1.3.2): output gain 8/14/20/25 dB gave captured RMS −30.20/−24.20/−18.20/−13.20 dBFS — so the same total-loudness math and the same idempotency hold. It needs `pip install 'helixgen[analyze]'` **and** the `sox` binary **and** `--capture-input NAME`; all three are checked **before the first capture**, never after a played window. `--capture-input` is deliberately not defaulted: naming the device (`'Helix Stadium XL'`) never touches the system default input, and capturing the wrong input would write confident, wrong trims. The capture is pinned by putting sox's format flags **before** the input device (the other order describes the *output* file and sox silently resamples) — `capture_argv()` exists so that ordering can't be got wrong. `--capture-channels` defaults to the proven mono capture of the processed output (the Stadium's USB map is ch1/2 processed, ch7 DI tap, ch3–6 silent; capture 8 with `--capture-remix 1,2` to fold the pair down). Analysis runs over the **middle** of the window — `--capture-skip` seconds dropped at each end, which removes capture start/stop transients and lets a looper's reverb tails overlap naturally (capturing exactly one loop period truncates long-reverb leads and under-counts them); `--seconds` must exceed twice the skip (30 s is the proven window). `--capture-dir` keeps each target's WAV (one per target) instead of wiping a temp dir, for A/B listening or re-analysis with `analyze-audio`. Per-target JSON entries then carry `lufs_integrated`, `peak_dbfs`, `true_peak_dbtp`, `crest_db`, `clipped`, `analyzed_seconds` and `capture` (the WAV path) in place of the meter fields, and the run's `--json` payload plus any library record carry `measure_via`. A capture with no gated audio (nothing playing / wrong device) SKIPS that target; a sox failure aborts the run. **The default metric is unchanged** — whether LUFS level-matches better *by ear* than the meter median is open (hc-3kg, needs a listening test). **`--source loop`** (workspace #82, core half): with a **front-of-chain looper** replaying a recorded signal, the input-jack gate reads pure silence — measuring gates on chain-out level instead and each target's total loudness is its raw measured chain-out **`output_db`** PLUS the output level in force (the looped source is identical across targets by construction, so output-level differences ARE the chain differences; `gain_db` is `null`). Keep the SAME loop replaying across every target of a run. Idempotency is unchanged (the taps are downstream either way, so a trimmed target simply re-measures at its new level). With `--measure-via capture` the source only picks the prompt — the metric is integrated LUFS regardless. **Library recording:** when a `--yes` run's `.hsp` is a registered tone-library variant (resolved via the library's tone metadata), the run is also recorded on that variant as a `normalized` record — `{at, scope, source, measure_via, target_total_db, tolerance_db, seconds, helixgen_version, targets: [...]}`, where `targets` carries the run's **full per-target measurement telemetry** exactly as this verb's `--json` reports it (`{snapshot|tone, name, ok, reason, gain_db, output_db, playing_seconds, output_level_db, total_db, trim_db, applied}`; snapshot scope stores every named snapshot's entry, setlist scope stores that tone's single entry). The telemetry is the point, not just the trims: `output_db` is chain-out dBFS, so a value over 0 flags **in-chain clipping** — agents (e.g. the tone skill) consume it to drive gain-staging fixes. Target entries are open dicts (unknown keys round-trip), so future per-node stats need no schema change. Summaries surface in `describe <tone>` / `library show <name>` (full telemetry under `library show --json`); this verb's `--json` lists the recorded variants under `library_recorded`. Records overwrite (latest run wins); in-band zero trims still record (they confirm the tone measures level-matched); a snapshot-scope run with any SKIPPED target records nothing; non-library `.hsp` files and dry-runs never touch metadata. The record is an optional schema-1 field — older helixgen readers simply ignore it. **Reachability preflight** (he-xth), reported BEFORE any file is written: the trim is the last stage of the chain, so a target can only be lifted to `measured total − the output level already in force + 20` (the output-level cap). Every measured target is stamped with `ceiling_db` and `reachable` in `--json`, and any target the requested `--target-db` overshoots is reported — before the write — as an **in-chain gain-staging problem**, not a level move: raise the amp's channel volume (both amps on a dual-amp preset), then re-run. Measured on a real library, 3 of 31 tones were in this state, and `ChVol` is wildly non-linear in dB (0.55 → 1.0 was +24.7 dB of chain gain on one amp). **The trim is NOT written** for such a target: clamping it at the cap would raise that chain's output — and its noise floor — by the same ~40 dB without ever reaching the target, leaving a worse tone that is still unmatched. The target is reported and its `.hsp` left alone; the reachable targets of the same run are still trimmed normally. Do NOT reach for the hardware's undocumented headroom above +20 instead (he-b9i: a write of 25 delivered a faithful +5 dB): boosting a quiet chain ~40 dB at the output amplifies its noise floor by the same amount, while `ChVol` makes real signal. Holds the `editbuffer` lock (it recalls snapshots / loads presets even in dry-run).

### Global Settings + Global EQ

- `helixgen device calibrate [--stimulus FILE] [--volume N] [--guitar SLUG] [--seconds N=10] [--min-playing N=40] [--tolerance-db 1.0] [--max-steps 5] [--json]` — **calibrate a recorded stimulus against your own playing, and persist it** (he-xth). `sample`-mode normalization replays a fixed recording so a run is repeatable and unattended — but the **source level decides what the trims mean**: a clean chain tracks source level ~1:1 while a saturated one is nearly source-independent (measured **0.16 dB/dB** on a real high-gain preset — input rose 36.6 dB, output moved 6), so the clean-to-saturated spread, and every trim derived from it, is a function of how hard the source drives the chain and NOT a property of the preset. An arbitrary playback level yields a perfectly repeatable rig producing trims that are an artifact of that choice (error ≈ 0.84 × the level error). Two steps: (1) you PLAY BY HAND for one window and the jack level `input_db` is recorded as the **reference**; (2) the stimulus plays and the system output volume is stepped until it reads within `--tolerance-db` of that reference. **The reference is `input_db`, never `gain_db`** — `input_db` is the jack level itself, so it is chain-independent and works on any preset, whereas `gain_db` on a clean chain is precisely the quantity that does *not* move with source level, so nulling against it "converges" instantly at any arbitrary level and calibrates nothing. On macOS the volume is set via `osascript`; on other platforms — and on a Mac where the attempt FAILS (no automation permission: headless ssh, MDM) — the value to set **by hand** is reported and the run stops. **The volume the run found is restored on every exit path**, success or failure: the loop drives the system volume, and leaving a machine wherever the search ended with a loop cabled into a guitar amp is not an acceptable exit state. The `--guitar` the reference was played on is recorded (defaulting to `default_guitar`) because instruments differ by 10+ dB — active EMG vs P-90 — so a reference taken with another guitar is not a reference. On success it writes `normalization.mode = "sample"`, `normalization.sample` (path, volume, playback command) and `normalization.calibration` (reference/achieved `input_db`, guitar, date) into `preferences.json`, merging key-by-key so nothing else in the file is disturbed; `device normalize` then takes its defaults from there. The mode is set to `sample` **unless the profile already says `looper`**, which is preserved (a looper rig needs the same calibration, and demoting it to `sample` would flip the implied `--source` back to `input`, whose gate reads pure silence while a looper replays — every later target SKIPPED). **A window that gates nothing stops the run immediately** rather than stepping the volume toward the dB floor (which would ramp to 100 and stay there), and so does a volume already at the rail. `--max-steps` below 1 is refused. Under `--json` the prompts go to **stderr** — they are redirected, never suppressed, since an unannounced by-hand window measures an empty room. **A run that does not converge writes NOTHING** — the usual cause is that the audio never reached the jack, because the Stadium is itself a USB audio interface and often steals the system default output. Playback is owned by the CLI (`helixgen.device.stimulus`): the loop starts before each window and is stopped on every exit path, including when a window raises. The default playback command is sox's own `play -q {path} repeat 9999`, and a shell-looped `afplay` is **refused** — each invocation costs ~0.8–0.9 s of process startup, turning a 5.00 s stimulus into a ~5.9 s jittering period, which destroys the whole point of an exact loop length (a window should cover WHOLE loop cycles, or the reading depends on window phase). Read-only on the device (it only measures).
- `helixgen device settings list [--page <p>] [--values]` / `get <key>` / `set <key> <value>` — read/write the device's **Global Settings** over the network (no Stadium app). Every Global Settings page — Ins/Outs, Switches/Pedals, Displays, Preferences, Songs, Tempo/Click, MIDI, Date/Time — plus Tuner and Wireless is exposed as a device *property* in the `global.*` namespace (161 curated keys) and read/written via `/PropertyValueGet` / `/PropertyValueSet`. `list` browses the curated page→key catalog (offline; `--values` also fetches each key's live value + range from the device; `--page` narrows to one page); `get` reads one value with its device-supplied name/type/range/enum labels; `set` writes one — `<value>` may be a number or, for enum settings, a label (e.g. `set global.tuner.type Strobe`) or index, validated against the property's range/enum before sending. The device self-describes each key via `/PropertyDefWithKeyGet`, so the catalog is live, not hardcoded. Protocol RE + hardware-validation: `docs/superpowers/specs/2026-07-13-global-settings-re-findings.md`. **Global EQ** (`dsp.globaleq.*`) has its own verb — see `device globaleq` below (it IS property-based, just a variant value shape).
- `helixgen device globaleq list` / `set <output> <band> <param> <value>` — write the device's **Global EQ** over the network (no Stadium app). The Stadium has three independent Global EQs, one per output layer: 1/4" (`qtr`), XLR (`xlr`), Phones (`pho`) — each a 7-band EQ (`lowcut`, `lowshelf`, `low`, `mid`, `high`, `highshelf`, `highcut`) plus an output level. Each param is a device property `dsp.globaleq.<out>.<band>.<param>` written via `/PropertyValueSet` with a **variant `{parm,valu}`** blob (byte-exact codec, HW-validated 2026-07-14). `list` prints the offline catalog; `set` writes one param (e.g. `device globaleq set qtr low gain 3.5`, or `set pho - level -2.0` for the output level). **Write-only over the network** — the device serves no `/PropertyValueGet` read-back for `dsp.globaleq.*`, so there is no `get`. Findings: `docs/superpowers/specs/2026-07-14-parity-capture-findings.md` §2.

### IR verbs (on the device)

- `helixgen device list-irs [--json]` — list the user IRs registered **on the device**: one line per IR, `<hash>  <mono|stereo>  <name>`; `--json` emits the raw metadata list, each entry enriched with **`file`** — the IR's on-device `.wav` basename (resolved via `/IrPathForHashGet`), which is what `device pull-ir` takes. Read-only. Distinct from the local `helixgen list-irs`, which prints helixgen's own `mapping.json` (`irhash → wav-path`) without touching the device. The hash shown is what `device delete-ir` / `device rename-ir` accept to disambiguate duplicate names. The listing is read **strictly** (a dropped or truncated `-11` reply errors instead of printing as "no IRs"). The device's `-11` listing cache is **never invalidated by watched-dir IR imports** (hardware-observed 2026-07-27: stale for 11+ min; a 2001 subscription does not force convergence — an RPC content write is what refreshes it), the cause of the observed "25th IR missing from `list-irs` while `/IrPathForHashGet` resolves it" under-report (backlog #38). helixgen's `push-ir` nudges the cache after registering, so an IR pushed by helixgen must appear; an IR imported by another client may stay unlisted until some content write. The point lookup stays the authority on presence: helixgen cross-checks it before reporting an IR a preset needs as missing. **Wedge detection (backlog #93):** a *wedged* IR (backing file present and resolving, but no `-11` registry entry) satisfies that lookup too, so every point-lookup override is put to the same nudged-listing check `push-ir` uses — a hash still absent from a listing taken after a **confirmed** cache refresh (a no-op same-name rename of a listed row) is wedged and reported **missing**, so the auto-upload paths (`install --auto-irs`, `device sync`) re-push it, and the re-push (`push_ir`) removes the orphaned file and re-imports: the wedge self-heals. When the refresh can't be confirmed — a failed listing (flaky transport must not read as a wedge), an empty listing, or a nudge whose rename didn't confirm — the point lookup stays trusted and the IR reads present (the lag case's false "missing" is the commoner trap); `device delete-ir --force-wedge` is the sure clear then. Both directions warn to stderr.
- `helixgen device push-ir <file.wav>` — import an impulse response onto the device **instantly**, exactly like the editor. Uploads the device-canonical processed IR (`helixgen.ir.write_stadium_ir`), which embeds a `HASH` chunk carrying helixgen's `irhash` — the device reads that and registers under exactly that hash. And `push_ir` subscribes to the device's **2001 change stream first**, which activates the device's watched-dir monitor so the file registers in ~0.1 s (without a 2001 subscriber, external uploads wait on the device's slow ~15-20 min scan). Confirms via the `/addContent` broadcast; result reports `device_hash`/`hash_match`/`cid`. **Listing-cache nudge (hardware-observed 2026-07-27, fw 1.3.2 b1340):** a watched-dir import registers the IR fully (content row, path index, `/addContent`) but does **not** invalidate the device's `-11` container-listing cache — `list-irs`/`rename-ir`/`delete-ir` would miss the new IR indefinitely (stale for 11+ min in observation; an RPC content write is what refreshes it). So after registration `push-ir` issues a same-name rename of the new cid as a no-op nudge, making the IR immediately visible to the listing verbs. The "already on device" short-circuit uses the same trick to tell apart the two states the point lookup can't: hash unlisted but listed **after** a nudge = genuinely registered under a stale cache ("already"); still unlisted after the nudge = **wedged** orphan file (backlog #93 — e.g. a client killed between `/RemoveContent` and the file removal), which is removed and imported normally instead of false-positiving "already". The wedge verdict is only earned off a **confirmed** refresh: a *failed* listing (flaky transport must not be read as a wedge), an **empty** listing (nothing to nudge — exactly the single-wedged-IR case), a listing with no usable row to rename, or a nudge whose rename didn't confirm all keep the trusting "already" behavior — the heal silently no-ops there, and `delete-ir --force-wedge` is the sure clear (backlog #93). See [`helix-sftp-access.md`](helix-sftp-access.md).
- `helixgen device pull-ir <filename> <outfile>` — download an IR `.wav` by its on-device **file basename** — discover it with `device list-irs --json` (the `file` field). The file keeps its original upload basename: `device rename-ir` changes only the *display* name (validated live), so a renamed IR still downloads under its original basename. EXPERIMENTAL.
- `helixgen device delete-ir <name-or-hash> [--yes] [--force-wedge]` — delete one user IR from the device **completely**: the registry entry (`/RemoveContent` on `-11`) plus its backing `.wav` (the device only garbage-collects the file lazily, which makes a quick re-import think it's "already on device"; removing the file closes that window). Presets that referenced it show a silent cab until it's re-imported. `--force-wedge` (32-hex hash only) additionally cleans the *wedged* state a delete→quick-re-import can leave (file + path index resolving, no registry entry) — never use it on a just-imported IR, whose listing may merely be lagging (a helixgen `push-ir` import is nudged into the listing, so only imports by other clients lag). Alternatively, re-running `device push-ir` on the same WAV distinguishes a stale listing cache from a wedge and heals the wedge by re-import — and since 0.32.0 the auto-upload paths (`install --auto-irs`, `device sync`) run the same wedge check and self-heal it too (#93). All of these need a `-11` listing with a row to nudge; an empty or failed listing keeps the trusting "already on device" answer, so `--force-wedge` is the sure clear.
- `helixgen device rename-ir <name-or-hash> <new-name>` — rename a user IR on the device. Display-name only; the hash presets reference is untouched, so nothing breaks.
- `helixgen device ir-prune [--yes] [--force] [--ignore-warnings] [--only <name-or-hash>] [--json]` — delete device IRs **no preset references any more** (backlog #11). Diffs the device's user IRs against the `irmd` hashes referenced by every pool preset (non-activating `get_content` scan), by the **live edit buffer**, and by local tone-library sources — `.hsp` files and the `.sbe` device-content blobs `device push` records (decoded natively since 0.21.0; previously a pushed tone produced a misleading "missing rpshnosj magic" warning). Hardened to fail closed: every listing it trusts is strict (a timeout/partial listing aborts rather than reading as "no presets"), the pool listing is cross-checked against setlist references (a **dangling** reference — one pointing at a deleted pool preset — aborts with an actionable "remove the stale reference" error, not a misleading reboot hint), and execute mode re-scans + re-verifies the plan immediately before deleting (a disagreement aborts with nothing deleted). **Dry-run by default**; `--yes` executes. Two **independent** consents: `--force` also deletes IRs referenced only by a local off-device tone (*protected*); `--ignore-warnings` proceeds when a local tone's `.hsp` can't be read to verify its protection (executing over warnings). `--only` narrows to a single IR.

### Setlist management + sync

- `helixgen device setlist list|add <setlist> <tone.hsp> [--pos N]|remove <setlist> <tone>|create-local <setlist>` — **manage the local setlist manifest** (`~/.helixgen/setlists/manifest.json`, override `$HELIXGEN_SETLISTS`; a legacy `~/.helixgen/setlists.json` v2 manifest auto-migrates up to the new location on first load — see "The tone library" below). The device stores a preset **pool** (container `-2`) plus named **setlists** that hold **references** into it, so one authored tone can belong to many setlists. The manifest records, per setlist, an ordered list of tone names backed by a `tones` path map; it also **absorbs the old slot ledger** (one file now). `add` registers a tone's `.hsp` (by its `meta.name`) and appends it to the setlist's membership; `remove` drops membership (keeping the tone in the pool if other setlists still use it); `create-local` makes an empty setlist in the manifest only. **Never hand-edit the file** — use these verbs (or the `tone` skill). `create-local` and `add`'s auto-create only touch the manifest — use `device setlist create` (below) to also create the setlist on the device.
- `helixgen device setlist create <name>` / `rename <old> <new>` / `delete <name> [--yes]` / `duplicate <src> <dst>` — **device-side setlist management** (backlog #8 **shipped**: `/CreateContent` under the setlists root with the setlist ctype, live-validated — no Stadium app needed). `create` makes an empty setlist on the device (and records it in the manifest); `rename` renames it on the device (and in the manifest, if tracked); `delete` removes the setlist container — its references die with it but the **pool presets they point at are never deleted** (never-orphan); `duplicate` copies `src`'s references into `dst` (auto-created when absent; must be empty otherwise) — references are pointers, so the pool presets are shared, not copied. Every one of these resolves its setlist name(s) **strictly** (backlog #39, shipped): a network timeout/undecodable listing aborts with a clear error instead of silently reading as "absent" and minting a duplicate-named setlist — `create`'s already-exists check, `rename`'s new-name-free check, and `duplicate`'s dst-absent check all fail closed the same way. `create` (and `duplicate`'s auto-create of an absent dst) always picks its position automatically (no `--pos`); that position-picking listing is strict too (backlog #40, shipped) — a timeout aborts before `/CreateContent` rather than risk computing "lowest empty" from a truncated listing and colliding with a real setlist. A create whose `/CreateContent` comes back with a **non-zero status code** is **not** treated as a failure (#38, root-caused 2026-07-19): field 3 of that reply is the device's edit-buffer dirty flag, not an error code, so the client **confirms by re-listing** the setlists root (bounded retries, strict) and proceeds with the re-listed cid — the same resolution the push/save paths use. A `/CreateContent` that comes back with **no `/status` reply at all** (a dropped frame on the flaky Stadium stack) is resolved the same way, for the same reason: a missing reply says nothing about whether the create landed, and reporting it as failure both mis-told the user and made `duplicate`'s auto-create abort on — and leak — a setlist that was really there. Only a setlist that is **genuinely absent** after those retries raises (naming the code, or saying no reply came back), and that path deletes **nothing** (the old self-cleaning cleanup destroyed creates that had landed). Nothing is recorded in the local manifest on failure.
- `helixgen device setlist import-hss <file.hss> [--list] [--setlist <name>] [--dry-run]` — **EXPERIMENTAL: import a Stadium-app `.hss` setlist-bundle export** (backlog #31, READ side). A `.hss` is a 24-byte Line 6 header + gzip + POSIX tar of `manifest.json` + 128 fixed `.N` slot files (empty = 1-byte `0x00` sentinel; filled = the preset's `.hsp` — magic `rpshnosj` + JSON, manifest `type` `application/stadium-preset`), decoded + pinned against real captured exports (empty **and** non-empty — findings spec `2026-07-15-hss-and-cc-capture-findings.md`). `--list` decodes the bundle fully offline (no device needed) and prints each slot's filled/empty state, **payload format** (`hsp`/`sbepgsm`), and preset name (from the embedded `.hsp`'s `meta.name`). Without `--list`, each filled slot is **transcoded** — an `.hsp` payload via `transcode.hsp_to_sbepgsm`, a device content blob (`_sbepgsm`/`/SetContentData`) via `content.to_content_data` — then installed into the device **pool** (non-activating) and referenced into a device setlist (named `--setlist`, or the bundle's own name if omitted; created if absent) — reusing the same install + setlist-create + reference primitives as `device install`/`device sync`; `--dry-run` previews without writing. New references are **appended after whatever the destination setlist already has** (never a raw slot-index write), so importing into an already-populated setlist never collides with/overwrites its existing members. The payload format is detected by **magic bytes** and cross-checked against the manifest `type` (a disagreement warns, non-fatal); an unrecognized payload is skipped with a clear per-slot error. Per-slot install/reference failures are reported without aborting the rest. Imported presets **are recorded in the tone library** as *pathless* tones (source `import-hss`) with membership in the destination setlist — load-bearing, so a later `device sync <setlist>` keeps their references instead of stripping them; having no local `.hsp`, they can't be restored by `device slots restore`. **Not idempotent on retry**: re-running after a partial failure duplicates the already-succeeded slots (pool presets + references) — delete the setlist + orphaned pool presets, or import into a fresh setlist, before retrying.
- `helixgen device setlist export-hss <setlist> <out.hss>` — **EXPERIMENTAL: export a DEVICE setlist to a `.hss` bundle** (backlog #31, WRITE side). Reads the named device setlist's references (order + slot `posi`) and assembles a `.hss` whose **container framing is byte-faithful**: the 24-byte header, the gzip 10-byte header (`MTIME`/`XFL`/`OS`), and the decompressed tar's structure (member names/order + exact octal ustar header field formatting + two-zero-block EOF) reproduce the app's byte-for-byte — pinned by re-serializing both real captures, where *given the same slot payload bytes* the entire decompressed tar is byte-identical. Two envelope caveats: the compressed DEFLATE stream differs (the app uses a non-zlib encoder no `zlib` window/mem/level reproduces — harmless, any gunzip yields the identical tar), and an export built from helixgen tones embeds helixgen's **compact**-JSON `.hsp` where the app pretty-prints — same `rpshnosj`+JSON family, functionally equivalent, re-importable. Each referenced preset's local `.hsp` (resolved by preset name via the tone library, embedded verbatim, `type: application/stadium-preset`) fills its slot — mirroring how the app embeds a `.hsp` per preset. A referenced preset with **no local `.hsp`** (device-born or untracked by the tone library) is **skipped** with a warning — helixgen has no device-content → `.hsp` converter, so it can't be re-embedded (backlog #31 residual); the `.hss` is still written with the presets that resolved. The writer proper is `helixgen.device.hss.write_hss` (unit-testable offline).
- `helixgen device sync <setlist> [--exclude-irs] [--repush]` / `helixgen device sync --all [--gc] [--exclude-irs] [--repush]` — **push the manifest's setlist(s) onto the device** (reference-based; **not** a destructive mirror). Resolves the named setlist under `-5` (errors clearly, pointing at `device setlist create <name>`, if the device doesn't have it — but a listing failure that couldn't actually determine absence is reported as its own distinct "could not verify" error, not "not found", so it never nudges you into creating a duplicate; backlog #39). Then reconciles the **pool first** — installs tones missing from the pool, re-pushes ones whose `.hsp` content hash changed (the hash is **recomputed from the file at sync time**, so an in-place edit to an already-synced tone is detected even though the mutating verb never refreshed the manifest's cached hash — #92), skips unchanged ones (idempotent) — and **rebuilds the setlist's references** to manifest order, adding/removing/reordering as needed and **never orphaning** a pool preset another setlist still references (the pool listing and the reference-rebuild's own current-references read are both strict, for the same duplicate-mint reason). Uploads each tone's referenced IRs (unless `--exclude-irs`). `--all` reconciles every **synced** manifest setlist (local-only drafts are skipped; a targeted `sync <setlist>` marks that setlist synced); `--gc` (only with `--all`) deletes pool presets no setlist references any more — its never-orphan listing is strict too: an unverifiable listing skips this run's deletes rather than risk treating a still-referenced preset as an orphan. Install **transcodes** each tone's `.hsp` straight into device content (no template, full fidelity — dual-amp, parallel splits, snapshots, and footswitch/EXP assignments all synthesized); a tone whose recorded source is a **`.sbe`** (what `device push` / `device slots restore` record) is pushed **verbatim** instead — it already is device content — with its IRs read from `mdls[*].irmd`. A source that is neither reports one error naming **both** formats. **`--repush`** (#25 residual) forces every in-scope tone already in the pool into the update bucket even when its `.hsp` bytes are unchanged since the last sync, re-pushing its content via the same non-activating `SetContentData`-on-the-existing-cid path (the `device restore` primitive) a normal hash-triggered update uses. Because plain sync now recomputes the file hash at sync time it already re-pushes genuinely edited `.hsp` files, so `--repush` is **only** for the unchanged-bytes case: **after a helixgen transcoder upgrade**, `device sync <setlist> --repush` refreshes device content that a plain sync would skip as unchanged (a byte-hash comparison can't see a transcoder-output difference for an unchanged `.hsp`). Per-tone/per-setlist failures (install, IR upload, reference rebuild, delete-gate verification) are reported in `errors[]` without aborting the rest of the run; result is `{ok, setlists, pool, references, gc, irs, errors}`. Shows a **live progress display on stderr** — a `click.progressbar` per phase when stderr is a TTY; otherwise (non-TTY, or `--no-progress`) a plain-text form that prints a **header line per phase PLUS one line per item** (`  <phase> <i>/<n>: <label>`) and one per IR upload, so a large sync emits on the order of a hundred stderr lines (a 40-tone sync is ~100). **`--no-progress` only disables the rich progress bar; it does NOT suppress the per-item plain lines** — there is no fully-quiet progress mode. stdout (the summary above) and `--json` are never affected. **The Stadium's network stack is flaky — if a sync drops or stalls, just re-run it (idempotent, auto-reconnecting); if it keeps dropping, reboot the Helix.** EXPERIMENTAL.

### The tone library (which tone lives where)

Every tone helixgen **generates auto-registers** into the **tone library** — the
manifest, now at `~/.helixgen/setlists/manifest.json` (override
`$HELIXGEN_SETLISTS`; a legacy `~/.helixgen/setlists.json` v2 manifest — or an
even older `device-slots.json` / v1 manifest — auto-migrates up to the new
location on first load: a `.bak-v2`/`.bak-v1` backup is written first, then
the legacy file is renamed `*.migrated-v2` so a re-run doesn't re-migrate).
A **tone** is *content + identity + management **intent***: its `.hsp` (or
nothing, if it came off the device), a unique name (also the device preset
key), a desired **user slot** (`null` = off device, `"auto"` = wants device /
address TBD, or `"1A".."128D"` — manifest-only since 0.30.0: `device add --slot` accepts
only `auto` and rejects explicit labels, since sync never converted a label into a device
address, backlog #30; place with `auto`, then move with `device reorder`), its
**setlist memberships** (ordered), and
provenance `source`. **"On the device" ⟺ the tone has a slot.** There is **no
separate slot ledger** — this one manifest is the single management-intent
record (design `docs/superpowers/specs/2026-07-13-tone-library-model-
redesign.md`; manifest v3 split design
`docs/superpowers/specs/2026-07-15-library-metadata-design.md` §3).

As of manifest v3, a specific Helix's **observed** placement (`cid`/`posi`
per tone, keyed by device serial) is **not** in the manifest — it lives in
`~/.helixgen/devices/<serial>.json` (`helixgen.device.observations`),
rebuilt wholesale by every `device sync`; losing a devices file costs
nothing, since the next sync rebuilds it. A v2→v3 migration folds any old
observed data into `devices/legacy.json`. **This directory is intentionally
not committed** to the `~/.helixgen` git repo (see "Home directory and git
plumbing" in `CLAUDE.md`) — only intent is.

- `helixgen register <tone.hsp>` — import an existing local `.hsp`
  into the library (off-device; `source: import-local`). The source is checked
  **at registration**: a file without the `.hsp` magic header is rejected with
  an error naming `device push` (the verb that takes a `.sbe`), rather than
  being recorded and failing on every later `device sync` — same check, same
  error, for `device setlist add <setlist> <file>`. (The old `--doc`
  companion-markdown flag was retired with manifest v3 — tone descriptions now
  live in the tone-metadata JSON, not a manifest `doc` sidecar path.)
- `helixgen device add <tone> [--slot auto]` — mark a library tone for the
  device (placed on the next `device sync`). **`--slot` accepts only `auto`**
  (the default). An explicit label (`5A`) is **rejected with an error**, not
  recorded: `device sync` never converted a recorded label into a device
  address — it installs at the lowest empty slot regardless — so the flag used
  to report a placement that never happened (backlog #30). To put a preset at
  a specific address, sync it with `auto` and then move it with
  `device reorder`.
- `helixgen device unsync <tone>` — clear a tone's slot so the next sync
  **deletes it from the device** (it stays in the library); cascades it out of
  any *synced* setlist.
- `helixgen device library [--json]` / `helixgen device slots [list] [--verify]`
  — list every tone: slot, on/off-device, and setlist memberships. Offline
  unless `--verify`, which cross-checks the live user setlist and flags
  `ok` / `missing` / `offline` / `untracked`.
- `helixgen device slots restore <name-or-slot> [--pos N] [--setlist S] [--force]`
  — re-install a tone from its recorded `.hsp` (re-authored) or `.sbe` (re-pushed).
  `--setlist` takes `user`/`factory`/a device setlist name like the other
  preset verbs (named setlist: pooled + referenced at the destination position).
  Pathless `save`/`create` tones have no local source and can't be restored.
  `--force` pushes into an occupied **pool** slot (for **both** `.hsp` and
  `.sbe` sources) — it skips the pool emptiness check. The device **INSERTS**
  at an occupied posi (hardware-characterized fw 1.3.2 b1340, #94/#69): the
  restored tone lands AT the requested posi and the occupant — every
  subsequent preset, in fact — **shifts down one**. Nothing is overwritten or
  deleted; `--force` does NOT replace the occupant's content in place. Because
  a landed create therefore always allocates a NEW cid, helixgen snapshots
  the pool's cids before creating (#94) and refuses to write if the entry it
  would write into already existed before the call (a same-name occupant
  matched by a lagging confirm when the create was actually dropped) — the
  error names the cid; nothing is written or deleted, though if the dropped
  create lands late an EMPTY stub with the tone's name may appear at the
  posi, shifting the occupant down: re-list, delete the stub, retry. A
  **failed write** into an entry that snapshot PROVED fresh cleans up that
  exact cid — but deletes leave a **gap** (they never shift entries back),
  so after that cleanup the occupant and every subsequent preset stay one
  posi lower with a hole at the target slot; helixgen warns, and
  `device reorder` restores placement. The attribution snapshot also
  refuses (before creating) when the pre-create listing carries an entry
  with no cid — such a listing can't prove a later cid fresh. `--force` is a **pool**
  flag only: restoring into a **named setlist** always writes at a freshly
  computed lowest-empty pool posi, which no `--force` ever skipped a check on,
  so a failed write there **does** clean up the stub it created (same for
  `device install --setlist`, which is `known_empty`, not `force`). An occupied
  **named-setlist** position is refused even with
  `--force` (backlog #69): `reference_into_setlist` never removes an
  incumbent, so proceeding would stack a second reference at one position —
  uncataloged device behavior. Remove the incumbent reference first
  (`device delete <cid> --setlist <name>`), then re-run. The emptiness
  checks are strict either way (backlog #40) — a listing timeout aborts the
  restore rather than reading the slot as empty.
  The destination is an explicit `--pos`, else the recorded slot
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
`1A`..`128D` label. The device's native content format (`_sbepgsm`) is a
separate schema from `.hsp`; see [`helix-protocol.md`](helix-protocol.md) and
`docs/superpowers/specs/2026-07-11-helix-device-v2-plan.md`.

**Pushing tones to the device is driven by the `device` skill**
(`.claude/skills/device/`), which runs after `tone` has authored the `.hsp`. It
centers on `device sync <setlist>` (the pool-first, reference-rebuilding,
IR-uploading, idempotent path). The skill adds the judgment those verbs need:
manifest membership via `device setlist add/remove`, the
**setlist-must-exist-first** rule (a missing device setlist is one `device
setlist create <name>` away), the **template-free transcode** install (any
block chain, full fidelity, no template/coverage step), the **never-orphan**
guarantee, the **full-graph synthesis** (dual-amp, parallel splits, snapshots,
footswitch/EXP assignments all transcode), the single-tone `device install
--auto-irs` IR upload (the same per-tone IR-upload core `device sync` uses;
it also records the tone-library manifest), and the **flaky-hardware** rule
(re-run a dropped sync; reboot the Helix if it persists). Read it before
scripting a setlist sync.
