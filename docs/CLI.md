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
- `helixgen bootstrap` — clone sensorium/phelix and ingest its `blocks/` folder.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash and register. Use `--force` to overwrite existing mappings. By default each WAV is **copied** into `library/irs/<pack>/` with a scaffolded metadata sidecar and `mapping.json` points at the copy; `--no-copy` registers in place with no metadata.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg (same copy-into-library default + `--no-copy` escape hatch as the direct form).
- `helixgen irhash <wav-or-dir>... [--json]` — compute Stadium hashes statelessly (nothing written to `mapping.json`); directories are recursed for `*.wav`.
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>] [--no-copy] [--json]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache. By default each newly-hashed WAV is **copied** into `library/irs/<pack>/` with a scaffolded metadata sidecar and `mapping.json` points at the copy (content-addressed idempotent; a re-scan of the same content is a no-op); `--no-copy` registers in place with no metadata.
- `helixgen list-irs [--json]` — print `<hash>  <wav-path>` for every registered IR.
- `helixgen ir-cache --stats | --clear | --prune` — inspect/maintain the IR-hash **cache** (a pure-local perf layer that memoizes expensive Stadium-hash computes, keyed by absolute path + mtime + size; **not** `mapping.json`). `--stats` prints entry count, path, and size; `--clear` deletes the cache file; `--prune` drops entries whose backing WAV is gone. Default location `~/.helixgen/cache/irhash.json` (override with `$HELIXGEN_IRHASH_CACHE`, or `$HELIXGEN_CACHE` for the cache dir). All IR-hashing paths (`register-irs`, `ir-scan`, `irhash`) share it transparently.
- `helixgen controllers [--json]` — the device's assignable controllers (FS/EXP) with English names + positions.
- `helixgen analyze-audio <capture.wav> [--json]` — offline audio-quality metrics from a WAV capture (backlog #62 phase 3): integrated/momentary/short-term LUFS per ITU-R BS.1770 (K-weighting, 400 ms blocks / 75% overlap, −70 LUFS absolute + −10 LU relative gates), crest factor / peak / true-peak / RMS in dB, a clipping flag, spectral centroid, and FFT band energies over the 5-band guitar vocabulary (low 60–200 Hz, low_mid 200–500, mid 500–1200, high_mid 1200–4000, high 4000–10000 — **provisional edges**, pending reconciliation with the IR catalog's measured-tag pass). Undefined metrics (silence, sub-400 ms files) come back `null` with a `notes` entry, not an error; non-finite samples (NaN/Inf) are zeroed and counted in `notes`, so `--json` is always strictly valid JSON. Needs numpy (`pip install 'helixgen[analyze]'`); accepts any PCM / IEEE-float WAV, any sample rate, mono or stereo. Measurement caveats (backlog #84): the WAV is decoded whole-file into memory as float64 (~2.7 GB peak for an hour of 48 kHz stereo — keep captures to minutes; no streaming mode), and the momentary/short-term LUFS **maxima** are computed on a 100 ms hop, so a peak straddling two hop positions can under-read by a fraction of a dB (integrated LUFS is unaffected). EXPERIMENTAL `--record N -o <out.wav> [--input <device>] [--rate] [--channels]` records the capture first from an audio input (the Stadium's USB return) via sounddevice (`pip install 'helixgen[capture]'` + PortAudio) — untested against real hardware. The capture options `--input`/`--rate`/`--channels` apply only to `--record`; passing any of them without `--record` is a usage error (they used to be silently ignored). Complements `device measure` (network meters, loudness only): this tier is the one that can say what a tone *sounds* like, not just how loud it is.

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
with an instructive error naming `device discover`; `--port` defaults to
2002 (the RPC control port; the telemetry verbs `tuner`/`meters`/`measure`
stream on the fixed PUB port 2003 and use `--port` for their reachability
preflight — see those verbs). Protocol reference:
[`helix-protocol.md`](helix-protocol.md).

#### `device discover` — find + persist the Stadium's address (0.24.0)

```
helixgen device discover [--timeout N] [--probe/--no-probe] [--json]
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
   the machine's **own /24 only** on RPC port 2002 (the device ignores
   ICMP). Short per-connect timeouts, bounded concurrency, never probes
   beyond the local subnet — and it refuses to scan at all when the
   machine's own address is not RFC 1918-private (10/8, 172.16/12,
   192.168/16): connect-scanning a public /24 would be a port scan of
   strangers, not LAN discovery (backlog #77). `--no-probe` disables it.

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
read-only on the device: no lock scope is taken.

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

### Device locks (machine-local, advisory — 0.22.0)

Every device-**mutating** verb auto-acquires a **machine-local advisory
lock** for its duration, so concurrent helixgen processes on the same
machine (including agents nobody is orchestrating) never collide on the
device. Read-only verbs acquire nothing. Locks are **lease files** —
`~/.helixgen/locks/<device-ip>/<scope>.lock` (root override
`$HELIXGEN_LOCKS`; the default follows `$HELIXGEN_HOME` like every
other home subarea, and `locks/` is gitignored in the home repo), JSON `{pid, hostname, acquired_at, ttl_seconds, label,
token?, kind, nonce}` — created atomically; the file is the source of truth
(no fcntl handle is held across processes, so shell-agent flows where every
CLI call is a fresh pid work). **Limitations (by design):** advisory —
nothing stops a `--no-lock` caller — and machine-local — direct-protocol
clients on **other hosts** and the **Stadium desktop editor are NOT
covered**.

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
| `library` | `create`, `save`, `rename`, `delete`, `set-info`, `push`, `restore`, `install` (without `--auto-irs`), `reorder`, `slots restore`, `setlist create/rename/delete/duplicate`, `setlist import-hss` (not `--list`/`--dry-run`) |
| `library` + `irs` | `sync` (`--exclude-irs` drops the `irs` scope), `install --auto-irs` (it uploads device IRs) |
| `irs` | `push-ir`, `delete-ir`, `rename-ir`, `ir-prune` (only with `--yes`; dry-run takes nothing) |
| `globals` | `settings set`, `globaleq set` |
| *(none)* | every read/list verb, the local-manifest verbs (`add`, `unsync`, `library`, `slots list/reorder`, `setlist list/add/remove/create-local/sync-on/sync-off`, `export-hss`, `local-list`), `backup`, `pull`, `pull-ir`, `watch`, `tuner`, `meters`, `measure` |

**Session leases — `device lock` / `device unlock`:**

- `helixgen device lock --scope <editbuffer|library|irs|globals|all> --label
  <text> [--ttl 900]` (scope repeatable; default `all`) — hold scope(s)
  ACROSS calls. Prints `HELIXGEN_LOCK_TOKEN=<token>`; **export it** and
  every covered verb passes through the lease (renewing its TTL) instead of
  deadlocking against it. Calls from the **same shell** as the `lock` also
  pass through without the token (the lease records the invoking shell's
  pid). Re-locking your own scope renews it (idempotent).
- `helixgen device lock --status [--json]` — inspect the device's leases:
  scope, label, pid, host, age, TTL, live/stale, ours. Read-only, exit 0.
- `helixgen device unlock [--scope <s>]... [--force]` — release your leases
  (all of them without `--scope`). An explicit `--scope` you don't own is an
  error unless `--force` (which breaks even a live foreign lease —
  dangerous). Foreign leases are otherwise reported and left alone.

**Contention:** a blocked acquire waits up to `$HELIXGEN_LOCK_TIMEOUT`
seconds (default **30**; `0` = fail fast) with polling backoff, then exits
non-zero naming the holder (label, pid, host, age). **Staleness:** a lease
whose TTL expired or whose recorded pid is dead (same host) is reclaimed
with a stderr warning (stale-breaks are serialized through a break-mutex
file and re-verified under it, so a renewed/re-acquired lease is never
broken); a **live lease is never broken** implicitly. Escape hatch: every
mutating verb takes `--no-lock` (dangerous — you're opting out of
collision protection).

Fine print: `--ttl 0` = no TTL expiry (reclaim then relies on pid-liveness
or `device unlock`). A **session** lease whose recorded pid is dead gets a
**120 s grace** (from its last acquisition/renewal) before pid-death makes
it stale — so run `device lock` from your long-lived shell, not via a
wrapper script (the wrapper's pid dies immediately; the lease then only
survives while covered verbs keep renewing it). Pid-liveness is POSIX-only:
on Windows it is disabled (probing would kill the probed process) and only
TTL staleness applies. Lease files are `0600` (the token is a private
capability).

### Preset + edit-buffer verbs

- `helixgen device list [--setlist <user|factory|NAME>] [--json]` — presets in the pool (`user`, default) or factory; with a named setlist, its **references** (each row: position, the reference's own cid, `rcid=` the pool preset it points at, name).
- `helixgen device setlists [--json]` — the device's setlist containers.
- `helixgen device info [--json]` — the device's identity over the network: model (+ helixgen chassis key), numeric device id, serial, firmware version/build/date, SD storage free/total (`/ProductInfoGet`; read-only, never touches presets or the edit buffer).
- `helixgen device active [--json]` — the device's **ACTIVE preset**: cid, name, and pool slot (reads the live property `server.active.preset.id` — it tracks the player's own panel selection as well as network loads — then resolves the cid via the read-only `/GetContentRef`; live-verified 2026-07-15, fw 1.3.2). This is how an agent saves/restores the player's selection: note the cid, do your work, `device load <cid>`.
- `helixgen device read <cid> [--json]` — a preset's metadata (name/slot/parent).
- `helixgen device load <cid>` — load a preset into the edit buffer.
- `helixgen device create --from <src_cid> [--setlist <dest>] --pos <N>` — no positionals; both options required. Into the pool (default): a **copy**, auto-named by the device after the source (`"<Name> (1)"` style — live-verified; rename with `device rename`). Into a **named setlist**: no copy — a **reference** to the source pool preset is added at `--pos` (the printed cid is the reference's own).
- `helixgen device save <name> [--setlist <dest>] --pos <N>` — save the live edit buffer as a new preset (slot must be empty; the emptiness check is strict — backlog #40 — a listing timeout aborts the save rather than reading the slot as empty). With a named setlist: saved into the pool (lowest empty slot) + referenced at `--pos`.
- `helixgen device rename <cid> <new_name>` — rename a preset.
- `helixgen device delete <cid> [--setlist <dest>] [--yes]` — delete a pool preset; with a **named setlist**, remove only the setlist's reference (`<cid>` may be the reference's cid or the referenced pool cid) — the pool preset is never touched.
- `helixgen device set-param <path> <block> <param_id> <value>` — set one edit-buffer param (`/ParamValueSet`). `<block>` is the `device blocks` coordinate — the DSP **grid slot**, sent to the wire unchanged (0.21.0 erratum, HW-proven 2026-07-15: the old `(key-1)/2` translation of the block's list position only coincided with the true slot for contiguous chains, which is why the output block was unaddressable). Discover `<param_id>` with **`device params <path> <block>`** — never guess pids. `<value>` is in the param's **raw units** (dB/Hz/enum-int, exactly as `device params` reports), not normalized. Proven live example: `helixgen device set-param 0 13 2 3.0` (output block at grid slot 13, `gain` pid 2, 3 dB).
- `helixgen device params <path> <block> [--json]` — one edit-buffer block's params: numeric **pid**, name (from the vendored model defs), **current value** (RAW units), type, range, default. The pid-discovery surface for `device set-param`. Read-only.
- `helixgen device blocks [--json]` — list the **live edit buffer's blocks** with their `(path, block)` coordinates — `block` is the DSP **grid slot** (0-27, not necessarily contiguous: outputs sit at 13/27, the hidden second input at 14), model name, and saved base on/off. Read-only. These are the coordinates `device bypass`/`device model`/`device set-param`/`device params` address.
- `helixgen device pull <cid> <outfile.sbe>` — back up a preset's raw content blob.
- `helixgen device push <file.sbe> <name> [--setlist <dest>] --pos <N>` — install a local content file into a new slot (restore/clone; the slot-emptiness check is strict — backlog #40 — a listing timeout aborts rather than reading the slot as empty). Named setlist: pooled + referenced at `--pos`. The `.sbe` is recorded as the tone's local source (`ir-prune` decodes it directly for IR references — no more bogus "missing rpshnosj magic" warning on a normal push flow).
- `helixgen device restore <file.sbe> <cid>` — overwrite an existing preset's content from a file.
- `helixgen device backup [--setlist <user|factory|NAME>] [--dir <D>]` — pull the pool (default) — or the pool presets a named setlist references, in setlist order — to local `.sbe` files + `manifest.json` (offline backup).
- `helixgen device local-list [--dir <D>]` — list locally backed-up presets (works with the Helix disconnected).
- `helixgen device watch [--seconds N] [--filter <addr>]` — stream the device's live property/telemetry events (2001/2003).
- `helixgen device set-info <cid>... [--color <name|0-11>] [--notes <text>]` — set preset **color** and/or **notes** on one or more CIDs (batch-capable). Color is the `colr` content attr (int enum; names `auto, white, red, dark orange, light orange, yellow, green, turquoise, blue, violet, pink, off` — order inferred from the app menu, pass the raw index if a name renders unexpectedly). Notes are the Preset Info text, stored as the `preset.meta.info` property inside the content blob and written via a **non-activating** content round-trip.
- `helixgen device install <preset.hsp> <name> --pos <N> [--setlist <dest>] [--auto-irs]` — **author a helixgen `.hsp` onto the device as a new, playable preset** (named `--setlist`: pooled at the lowest empty slot + referenced at `--pos`) (the `/tone` → on-your-amp path). **Transcodes** the `.hsp` straight into the device's native content format (`_sbepgsm`) via `device/transcode.py` and `/SetContentData`s it into the empty pool slot (the slot-emptiness check is strict — backlog #40 — a listing timeout aborts rather than reading the slot as empty) — **no template, any block chain, full fidelity** (models/params/IRs); model/param names bridge helixgen↔device via `device/modelmap.py` + `device/defs.py`. Synthesizes the **full signal graph** — dual-amp / dual-DSP, **intra-flow parallel splits**, **snapshots** (per-scene bypass + param deltas), and **footswitch/EXP assignments** all transcode faithfully onto the device's real 28-slot grid (hardware-validated byte-for-byte vs HX Edit's own import, 2.18.0). `--auto-irs` uploads any IRs the preset references that aren't already on the device (resolving each `irhash` to a local WAV via `mapping.json`, then `push-ir`). Each `push-ir` registers instantly under the preset's `irhash` (via the `HASH` chunk + 2001 subscription — see `push-ir` below), so the installed preset's cabs resolve immediately with no editor step. EXPERIMENTAL.

### Live device ops (mutate the ACTIVE tone)

These live-ops verbs mutate the ACTIVE tone (decoded + HW-validated 2026-07-14).

- `helixgen device snapshot <index>` — **recall a snapshot** (0-based, 0..7) on the live device (`/activateSnapshot`; absolute index) — changes the ACTIVE tone's snapshot immediately, like stepping the snapshot footswitch.
- `helixgen device bypass <path> <block> <on|off>` — **bypass/enable a block** in the live edit buffer (`/BlockEnableSet [dsp, grid_slot, enable]`; the `device blocks` coordinate — the DSP grid slot — goes on the wire unchanged; 0.21.0 erratum to the 2026-07-14 `(key-1)/2` finding, which only held for contiguous chains. The device echo alone is NOT proof a toggle landed — it happily echoes a toggle of an empty slot; the meters are ground truth). The toggle is *volatile* (audible at once, not written to the preset until you save, so `device blocks` won't reflect it).
- `helixgen device model <path> <block> <model>` — **swap a block's model** live (`/ModelSet [dsp, grid_slot, sub, modelId]`; grid slot unchanged, like `bypass`). `<model>` is a numeric model id or a model-id string like `HD2_AmpBritPlexiNrm` (see `list-blocks`). The device rejects a cross-category swap; the app's re-attach-controllers + push-defaults cascade is not replayed.
- `helixgen device reorder <setlist> <target> --to <N>` — **move a preset to a new position within a setlist** (`/ReorderContainerContent [container, [cids], newPos]`, decoded 2026-07-14, HW-validated). `<setlist>` is a setlist display name (resolved the way `device setlist rename/delete/duplicate` resolve setlists) or a literal container cid (`-2` = the pool, whose `cctp==PRESET` entries also resolve by their own names); `<target>` is a preset display name or a literal cid within it. Pass `setlists` as `<setlist>` to instead reorder the top-level setlist list itself (`<target>` is then a setlist name/cid) — the keyword is checked before name resolution, so a real setlist literally named "setlists" must be addressed by its container cid. **Numeric arguments are cid-first**: a purely-digit `<target>`/`<setlist>` is always parsed as a cid, never a display name. If an item is display-named that digit string, the cid reading wins with a stderr/result **warning** when the cid itself resolves in the container, and the command **errors** (pointing at the named item's real cid) when it doesn't. `--to` is bounds-validated against the container's current length before anything is sent. A **total reply timeout** (no `/error`, no `/status`, no update frame) raises instead of silently re-listing as if the move succeeded; a partial reply still falls back to a bookkeeping re-list because a reqid-correlated frame proves the device processed the write. **This is a direct, immediate DEVICE-side write** — distinct from the local-manifest `device slots reorder`, which only edits the tone library's recorded order and takes effect on the device on the next `device sync` (which can then reorder things right back to the manifest's order).
- `helixgen device tuner [--seconds N] [--json]` — **live network tuner** (no Stadium app, no hardware-tuner engage needed). The Stadium runs an always-on background pitch detector and streams it on 2003 as `/dspEvent {eid_:10,mid_:796}` = a single **fractional-MIDI** float (int = note, frac×100 = cents, `-1` = silence). Prints a live note/cents/Hz readout with an in-tune meter; `--json` emits one reading per line. HW-validated (stream+decode); pitch math golden-tested. Reachability is **preflighted** (one cheap TCP probe of the `--port` control port, #64c) — an unreachable/powered-off device fails fast with a clear error instead of streaming silence for the whole window (the SUB socket connects lazily and can't tell a dead host from a quiet one).
- `helixgen device meters [--seconds N] [--json]` — **live network level meters** (no Stadium app needed), read-only. Same always-on `/dspEvent` burst as the tuner also carries two grid-level meter arrays, `{eid_:1,mid_:796}` and `{eid_:1,mid_:800}` — each a **128-float** array — which this decodes into a live bar readout; `--json` emits one reading per line (`{mid, peak, values}`). HW-characterized 2026-07-14: the grids are **live per-node audio envelopes** at ~10 Hz per mid (linear amplitude, >1.0 legal) — mid 796 carries the path chain nodes (cells 0–1 = instrument input), mid 800's populated cells are the output-send pairs (= chain-out level); all taps sit **upstream of the output block's `gain`**. Full per-layout cell map still open (backlog #62). Same reachability preflight as `tuner` (one TCP probe of the `--port` control port; fail-fast on an unreachable device, #64c).
- `helixgen device measure [--seconds N=20] [--min-playing N=40] [--source input|loop] [--json]` — **measure how loud the ACTIVE tone is while the player plays**, read-only. Reduces the playing-gated telemetry (real pitch + non-silent input; hum/silence ignored — single-coil hum defeats level-only gating but reads `-1` on the pitch stream) to robust dB stats: instrument input, chain-out (median + p75), and the input-invariant **chain gain** (out/in) — the number to compare across snapshots/presets when level-matching. **`--source loop`** (workspace #82, core half): when a **front-of-chain looper** replays a recorded signal, the input-jack gate is structurally silent (no pitch, no input level — every sample would gate out), so loop mode gates on **chain-out level** instead (`measure.is_playing_loop`, floor `LOOP_OUTPUT_FLOOR`); `gain_db` is `null` (no input reference) and the number to compare across targets is the raw **`output_db`** — the looped source is identical across targets by construction. The `--json` result carries a `source` field. Tell the player to play steadily; exits 1 (JSON `ok:false` + `reason`) when the window had too little actual playing (~10 gated samples/sec of playing; default needs ~4 s). The reported `seconds` is the window **actually sampled** — a Ctrl-C'd partial window reports its true elapsed time, not the requested `--seconds` — and `playing_seconds` derives from the window's **observed** sample rate rather than assuming the nominal 10 Hz (#64d). Same reachability preflight as `tuner`/`meters` (one TCP probe of the `--port` control port; fail-fast instead of a full silent window ending in "no meter data", #64c). Loudness-feedback spec phase 1; `device normalize` (below) is the closed loop built on it.
- `helixgen device normalize [<preset.hsp> | --setlist <name>] [--target-db X] [--seconds N=20] [--min-playing N=40] [--tolerance-db 1.0] [--source input|loop] [--yes] [--json]` — **level-match snapshots or a whole setlist by measuring while the player plays** (loudness spec phase 2, backlog #62). The closed loop over `device measure`: recalls each target (snapshot scope: each NAMED snapshot of the local `.hsp` — the device's ACTIVE tone must be that preset; its name is **verified** via the active-preset property before anything is measured and a mismatch aborts, an unverifiable name only warns; setlist scope: loads each manifest tone by its observed CID and verifies the loaded preset's name matches the tone — a mismatch means a stale observation and that tone is SKIPPED), prompts the player per target, and computes each target's dB trim so its **total loudness** — the measured median chain gain **plus the output-block level already in force** (the meter taps sit upstream of the output gain, so the measured gain alone never includes an existing trim) — matches the **anchor**'s total (the first target that measured ok) or an absolute `--target-db`. Sizing trims from totals makes the loop **idempotent**: a re-run (same playing) computes in-band zero trims instead of compounding, and hand-balanced output overrides that already equalize are left alone. Deltas within `--tolerance-db` are in band and left alone (don't chase meter noise). **DRY-RUN by default** — measuring happens, trims are only reported; `--yes` writes them into the **local `.hsp` file(s)** (the source of truth) as output-block `level` moves: per-snapshot overrides (snapshot scope) or a whole-preset shift of base + any per-snapshot array (setlist scope; the uniform shift preserves the preset's own scene-to-scene and path-to-path balance). The device copy is NOT written — run `device sync <setlist>` / `device install` afterwards. If a mid-run write fails, the error lists the files already written (a re-run is safe — written files re-measure in band). Targets that can't be measured (too little playing, no local `.hsp`, no observed placement, name mismatch) are SKIPPED with a warning and the run exits 1 to flag the partial result. A setlist run restores the player's previously ACTIVE preset afterwards (best-effort); snapshot scope restores the preset's on-load snapshot. **Phase-0 caveat, by design:** the output block's `level` is dB-native so each trim is exact in one move, but every meter tap sits UPSTREAM of the output gain — the trim is invisible to `device measure`, so the loop trusts the dB math and deliberately does NOT re-measure to confirm. **`--source loop`** (workspace #82, core half): with a **front-of-chain looper** replaying a recorded signal, the input-jack gate reads pure silence — measuring gates on chain-out level instead and each target's total loudness is its raw measured chain-out **`output_db`** PLUS the output level in force (the looped source is identical across targets by construction, so output-level differences ARE the chain differences; `gain_db` is `null`). Keep the SAME loop replaying across every target of a run. Idempotency is unchanged (the meter taps sit upstream of the trims either way). **Library recording:** when a `--yes` run's `.hsp` is a registered tone-library variant (resolved via the library's tone metadata), the run is also recorded on that variant as a `normalized` record — `{at, scope, source, target_total_db, tolerance_db, seconds, helixgen_version, targets: [...]}`, where `targets` carries the run's **full per-target measurement telemetry** exactly as this verb's `--json` reports it (`{snapshot|tone, name, ok, reason, gain_db, output_db, playing_seconds, output_level_db, total_db, trim_db, applied}`; snapshot scope stores every named snapshot's entry, setlist scope stores that tone's single entry). The telemetry is the point, not just the trims: `output_db` is chain-out dBFS, so a value over 0 flags **in-chain clipping** — agents (e.g. the tone skill) consume it to drive gain-staging fixes. Target entries are open dicts (unknown keys round-trip), so future per-node stats need no schema change. Summaries surface in `describe <tone>` / `library show <name>` (full telemetry under `library show --json`); this verb's `--json` lists the recorded variants under `library_recorded`. Records overwrite (latest run wins); in-band zero trims still record (they confirm the tone measures level-matched); a snapshot-scope run with any SKIPPED target records nothing; non-library `.hsp` files and dry-runs never touch metadata. The record is an optional schema-1 field — older helixgen readers simply ignore it. Holds the `editbuffer` lock (it recalls snapshots / loads presets even in dry-run).

### Global Settings + Global EQ

- `helixgen device settings list [--page <p>] [--values]` / `get <key>` / `set <key> <value>` — read/write the device's **Global Settings** over the network (no Stadium app). Every Global Settings page — Ins/Outs, Switches/Pedals, Displays, Preferences, Songs, Tempo/Click, MIDI, Date/Time — plus Tuner and Wireless is exposed as a device *property* in the `global.*` namespace (161 curated keys) and read/written via `/PropertyValueGet` / `/PropertyValueSet`. `list` browses the curated page→key catalog (offline; `--values` also fetches each key's live value + range from the device; `--page` narrows to one page); `get` reads one value with its device-supplied name/type/range/enum labels; `set` writes one — `<value>` may be a number or, for enum settings, a label (e.g. `set global.tuner.type Strobe`) or index, validated against the property's range/enum before sending. The device self-describes each key via `/PropertyDefWithKeyGet`, so the catalog is live, not hardcoded. Protocol RE + hardware-validation: `docs/superpowers/specs/2026-07-13-global-settings-re-findings.md`. **Global EQ** (`dsp.globaleq.*`) has its own verb — see `device globaleq` below (it IS property-based, just a variant value shape).
- `helixgen device globaleq list` / `set <output> <band> <param> <value>` — write the device's **Global EQ** over the network (no Stadium app). The Stadium has three independent Global EQs, one per output layer: 1/4" (`qtr`), XLR (`xlr`), Phones (`pho`) — each a 7-band EQ (`lowcut`, `lowshelf`, `low`, `mid`, `high`, `highshelf`, `highcut`) plus an output level. Each param is a device property `dsp.globaleq.<out>.<band>.<param>` written via `/PropertyValueSet` with a **variant `{parm,valu}`** blob (byte-exact codec, HW-validated 2026-07-14). `list` prints the offline catalog; `set` writes one param (e.g. `device globaleq set qtr low gain 3.5`, or `set pho - level -2.0` for the output level). **Write-only over the network** — the device serves no `/PropertyValueGet` read-back for `dsp.globaleq.*`, so there is no `get`. Findings: `docs/superpowers/specs/2026-07-14-parity-capture-findings.md` §2.

### IR verbs (on the device)

- `helixgen device list-irs [--json]` — list the user IRs registered **on the device**: one line per IR, `<hash>  <mono|stereo>  <name>`; `--json` emits the raw metadata list, each entry enriched with **`file`** — the IR's on-device `.wav` basename (resolved via `/IrPathForHashGet`), which is what `device pull-ir` takes. Read-only. Distinct from the local `helixgen list-irs`, which prints helixgen's own `mapping.json` (`irhash → wav-path`) without touching the device. The hash shown is what `device delete-ir` / `device rename-ir` accept to disambiguate duplicate names.
- `helixgen device push-ir <file.wav>` — import an impulse response onto the device **instantly**, exactly like the editor. Uploads the device-canonical processed IR (`helixgen.ir.write_stadium_ir`), which embeds a `HASH` chunk carrying helixgen's `irhash` — the device reads that and registers under exactly that hash. And `push_ir` subscribes to the device's **2001 change stream first**, which activates the device's watched-dir monitor so the file registers in ~0.1 s (without a 2001 subscriber, external uploads wait on the device's slow ~15-20 min scan). Confirms via the `/addContent` broadcast; result reports `device_hash`/`hash_match`. See [`helix-sftp-access.md`](helix-sftp-access.md).
- `helixgen device pull-ir <filename> <outfile>` — download an IR `.wav` by its on-device **file basename** — discover it with `device list-irs --json` (the `file` field). The file keeps its original upload basename: `device rename-ir` changes only the *display* name (validated live), so a renamed IR still downloads under its original basename. EXPERIMENTAL.
- `helixgen device delete-ir <name-or-hash> [--yes] [--force-wedge]` — delete one user IR from the device **completely**: the registry entry (`/RemoveContent` on `-11`) plus its backing `.wav` (the device only garbage-collects the file lazily, which makes a quick re-import think it's "already on device"; removing the file closes that window). Presets that referenced it show a silent cab until it's re-imported. `--force-wedge` (32-hex hash only) additionally cleans the *wedged* state a delete→quick-re-import can leave (file + path index resolving, no registry entry) — never use it on a just-imported IR, whose listing may merely be lagging.
- `helixgen device rename-ir <name-or-hash> <new-name>` — rename a user IR on the device. Display-name only; the hash presets reference is untouched, so nothing breaks.
- `helixgen device ir-prune [--yes] [--force] [--ignore-warnings] [--only <name-or-hash>] [--json]` — delete device IRs **no preset references any more** (backlog #11). Diffs the device's user IRs against the `irmd` hashes referenced by every pool preset (non-activating `get_content` scan), by the **live edit buffer**, and by local tone-library sources — `.hsp` files and the `.sbe` device-content blobs `device push` records (decoded natively since 0.21.0; previously a pushed tone produced a misleading "missing rpshnosj magic" warning). Hardened to fail closed: every listing it trusts is strict (a timeout/partial listing aborts rather than reading as "no presets"), the pool listing is cross-checked against setlist references (a **dangling** reference — one pointing at a deleted pool preset — aborts with an actionable "remove the stale reference" error, not a misleading reboot hint), and execute mode re-scans + re-verifies the plan immediately before deleting (a disagreement aborts with nothing deleted). **Dry-run by default**; `--yes` executes. Two **independent** consents: `--force` also deletes IRs referenced only by a local off-device tone (*protected*); `--ignore-warnings` proceeds when a local tone's `.hsp` can't be read to verify its protection (executing over warnings). `--only` narrows to a single IR.

### Setlist management + sync

- `helixgen device setlist list|add <setlist> <tone.hsp> [--pos N]|remove <setlist> <tone>|create-local <setlist>` — **manage the local setlist manifest** (`~/.helixgen/setlists/manifest.json`, override `$HELIXGEN_SETLISTS`; a legacy `~/.helixgen/setlists.json` v2 manifest auto-migrates up to the new location on first load — see "The tone library" below). The device stores a preset **pool** (container `-2`) plus named **setlists** that hold **references** into it, so one authored tone can belong to many setlists. The manifest records, per setlist, an ordered list of tone names backed by a `tones` path map; it also **absorbs the old slot ledger** (one file now). `add` registers a tone's `.hsp` (by its `meta.name`) and appends it to the setlist's membership; `remove` drops membership (keeping the tone in the pool if other setlists still use it); `create-local` makes an empty setlist in the manifest only. **Never hand-edit the file** — use these verbs (or the `tone` skill). `create-local` and `add`'s auto-create only touch the manifest — use `device setlist create` (below) to also create the setlist on the device.
- `helixgen device setlist create <name>` / `rename <old> <new>` / `delete <name> [--yes]` / `duplicate <src> <dst>` — **device-side setlist management** (backlog #8 **shipped**: `/CreateContent` under the setlists root with the setlist ctype, live-validated — no Stadium app needed). `create` makes an empty setlist on the device (and records it in the manifest); `rename` renames it on the device (and in the manifest, if tracked); `delete` removes the setlist container — its references die with it but the **pool presets they point at are never deleted** (never-orphan); `duplicate` copies `src`'s references into `dst` (auto-created when absent; must be empty otherwise) — references are pointers, so the pool presets are shared, not copied. Every one of these resolves its setlist name(s) **strictly** (backlog #39, shipped): a network timeout/undecodable listing aborts with a clear error instead of silently reading as "absent" and minting a duplicate-named setlist — `create`'s already-exists check, `rename`'s new-name-free check, and `duplicate`'s dst-absent check all fail closed the same way. `create` (and `duplicate`'s auto-create of an absent dst) always picks its position automatically (no `--pos`); that position-picking listing is strict too (backlog #40, shipped) — a timeout aborts before `/CreateContent` rather than risk computing "lowest empty" from a truncated listing and colliding with a real setlist. A create whose `/CreateContent` comes back with a **non-zero status code** now **self-cleans** any setlist stub the device allocated anyway (the #38 anomaly — the same verify-before-delete cleanup, by name+position under the setlists root, that the push/save paths use; #66 residual) and fails naming the code; nothing is recorded in the local manifest on failure.
- `helixgen device setlist import-hss <file.hss> [--list] [--setlist <name>] [--dry-run]` — **EXPERIMENTAL: import a Stadium-app `.hss` setlist-bundle export** (backlog #31, READ side). A `.hss` is a 24-byte Line 6 header + gzip + POSIX tar of `manifest.json` + 128 fixed `.N` slot files (empty = 1-byte `0x00` sentinel; filled = the preset's `.hsp` — magic `rpshnosj` + JSON, manifest `type` `application/stadium-preset`), decoded + pinned against real captured exports (empty **and** non-empty — findings spec `2026-07-15-hss-and-cc-capture-findings.md`). `--list` decodes the bundle fully offline (no device needed) and prints each slot's filled/empty state, **payload format** (`hsp`/`sbepgsm`), and preset name (from the embedded `.hsp`'s `meta.name`). Without `--list`, each filled slot is **transcoded** — an `.hsp` payload via `transcode.hsp_to_sbepgsm`, a device content blob (`_sbepgsm`/`/SetContentData`) via `content.to_content_data` — then installed into the device **pool** (non-activating) and referenced into a device setlist (named `--setlist`, or the bundle's own name if omitted; created if absent) — reusing the same install + setlist-create + reference primitives as `device install`/`device sync`; `--dry-run` previews without writing. New references are **appended after whatever the destination setlist already has** (never a raw slot-index write), so importing into an already-populated setlist never collides with/overwrites its existing members. The payload format is detected by **magic bytes** and cross-checked against the manifest `type` (a disagreement warns, non-fatal); an unrecognized payload is skipped with a clear per-slot error. Per-slot install/reference failures are reported without aborting the rest. Imported presets **are recorded in the tone library** as *pathless* tones (source `import-hss`) with membership in the destination setlist — load-bearing, so a later `device sync <setlist>` keeps their references instead of stripping them; having no local `.hsp`, they can't be restored by `device slots restore`. **Not idempotent on retry**: re-running after a partial failure duplicates the already-succeeded slots (pool presets + references) — delete the setlist + orphaned pool presets, or import into a fresh setlist, before retrying.
- `helixgen device setlist export-hss <setlist> <out.hss>` — **EXPERIMENTAL: export a DEVICE setlist to a `.hss` bundle** (backlog #31, WRITE side). Reads the named device setlist's references (order + slot `posi`) and assembles a `.hss` whose **container framing is byte-faithful**: the 24-byte header, the gzip 10-byte header (`MTIME`/`XFL`/`OS`), and the decompressed tar's structure (member names/order + exact octal ustar header field formatting + two-zero-block EOF) reproduce the app's byte-for-byte — pinned by re-serializing both real captures, where *given the same slot payload bytes* the entire decompressed tar is byte-identical. Two envelope caveats: the compressed DEFLATE stream differs (the app uses a non-zlib encoder no `zlib` window/mem/level reproduces — harmless, any gunzip yields the identical tar), and an export built from helixgen tones embeds helixgen's **compact**-JSON `.hsp` where the app pretty-prints — same `rpshnosj`+JSON family, functionally equivalent, re-importable. Each referenced preset's local `.hsp` (resolved by preset name via the tone library, embedded verbatim, `type: application/stadium-preset`) fills its slot — mirroring how the app embeds a `.hsp` per preset. A referenced preset with **no local `.hsp`** (device-born or untracked by the tone library) is **skipped** with a warning — helixgen has no device-content → `.hsp` converter, so it can't be re-embedded (backlog #31 residual); the `.hss` is still written with the presets that resolved. The writer proper is `helixgen.device.hss.write_hss` (unit-testable offline).
- `helixgen device sync <setlist> [--exclude-irs] [--repush]` / `helixgen device sync --all [--gc] [--exclude-irs] [--repush]` — **push the manifest's setlist(s) onto the device** (reference-based; **not** a destructive mirror). Resolves the named setlist under `-5` (errors clearly, pointing at `device setlist create <name>`, if the device doesn't have it — but a listing failure that couldn't actually determine absence is reported as its own distinct "could not verify" error, not "not found", so it never nudges you into creating a duplicate; backlog #39). Then reconciles the **pool first** — installs tones missing from the pool, re-pushes ones whose `.hsp` content hash changed, skips unchanged ones (idempotent) — and **rebuilds the setlist's references** to manifest order, adding/removing/reordering as needed and **never orphaning** a pool preset another setlist still references (the pool listing and the reference-rebuild's own current-references read are both strict, for the same duplicate-mint reason). Uploads each tone's referenced IRs (unless `--exclude-irs`). `--all` reconciles every **synced** manifest setlist (local-only drafts are skipped; a targeted `sync <setlist>` marks that setlist synced); `--gc` (only with `--all`) deletes pool presets no setlist references any more — its never-orphan listing is strict too: an unverifiable listing skips this run's deletes rather than risk treating a still-referenced preset as an orphan. Install **transcodes** each tone's `.hsp` straight into device content (no template, full fidelity — dual-amp, parallel splits, snapshots, and footswitch/EXP assignments all synthesized). **`--repush`** (#25 residual) forces every in-scope tone already in the pool into the update bucket even when its recorded `.hsp` hash still matches, re-pushing its content via the same non-activating `SetContentData`-on-the-existing-cid path (the `device restore` primitive) a normal hash-triggered update uses — **after a helixgen transcoder upgrade**, `device sync <setlist> --repush` refreshes device content that a plain sync would skip as unchanged (hash-based change detection can't see a transcoder-output difference for an unchanged `.hsp`). Per-tone/per-setlist failures (install, IR upload, reference rebuild, delete-gate verification) are reported in `errors[]` without aborting the rest of the run; result is `{ok, setlists, pool, references, gc, irs, errors}`. Shows a **live per-phase progress display on stderr** (a `click.progressbar` when stderr is a TTY, plain one-line-per-phase text otherwise; auto-disabled — no bar — when stderr isn't a TTY), suppressible with `--no-progress`; stdout (the summary above) and `--json` are never affected. **The Stadium's network stack is flaky — if a sync drops or stalls, just re-run it (idempotent, auto-reconnecting); if it keeps dropping, reboot the Helix.** EXPERIMENTAL.

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
address TBD, or `"1A".."128D"`), its **setlist memberships** (ordered), and
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
  into the library (off-device; `source: import-local`). (The old `--doc`
  companion-markdown flag was retired with manifest v3 — tone descriptions now
  live in the tone-metadata JSON, not a manifest `doc` sidecar path.)
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
  `--setlist` takes `user`/`factory`/a device setlist name like the other
  preset verbs (named setlist: pooled + referenced at the destination position).
  Pathless `save`/`create` tones have no local source and can't be restored.
  `--force` pushes into an occupied **pool** slot (for **both** `.hsp` and
  `.sbe` sources) — it skips the pool emptiness check; the occupant is **not
  deleted**. An occupied **named-setlist** position is refused even with
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
