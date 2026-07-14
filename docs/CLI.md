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
Snapshot actions; EXPERIMENTAL, #16), per-block IR references, trails — see the project
[`CLAUDE.md`](../CLAUDE.md) which documents every field. Device-network verbs
(`helixgen device …`, incl. `device info`) are likewise documented there.

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

With the `device` extra (`pip install 'helixgen[device]'`) helixgen drives a
Helix Stadium over the LAN — no editor app. Point at the device with
`--ip`/`--port` or `$HELIXGEN_HELIX_IP`. The full verb reference lives in the
project [`CLAUDE.md`](../CLAUDE.md); highlights:

- Presets: `device list / read / load / create / save / rename / delete /
  set-param / pull / push / restore / backup / install / sync`.
- Preset info: `device set-info <cid>... [--color <name|0-11>] [--notes
  <text>]` — batch-capable color + notes (notes are written without
  activating the preset).
- Setlists: `device setlists`, `device setlist
  create|rename|delete|duplicate` (device-side; delete/duplicate never touch
  the preset pool) and `device setlist list|add|remove|create-local`
  (local manifest membership for `device sync`).
- Live device ops: `device snapshot|bypass|model|blocks` (mutate the ACTIVE
  tone), `device tuner`/`device meters` (read-only 2003 telemetry), and
  `device reorder <setlist> <target> --to <N>` — a direct DEVICE-side preset
  reorder, distinct from the local-manifest `device slots reorder` + `sync`.
  Reorder gotcha: a purely-numeric `<target>` is **always parsed as a cid**
  (cid-first), never as a display name — a preset literally named e.g. `"7"`
  can only be addressed by its cid. If the container holds an item *named*
  the digit string you passed, the cid reading wins with a stderr warning
  when that cid exists in the container, and the command errors (telling you
  the named item's real cid) when it doesn't. `--to` is bounds-checked
  against the container's length. Same cid-first rule for `<setlist>`: a
  literal integer is a container cid (`-2` = the pool, whose presets also
  resolve by name), and the word `setlists` always means the setlists root.
- `device setlist import-hss <file.hss> [--list] [--setlist <name>]
  [--dry-run]` — EXPERIMENTAL: import a Stadium-app `.hss` setlist-bundle
  export (backlog #31, READ side). `--list` decodes fully offline (slot,
  filled/empty, preset name); otherwise each filled slot is installed into
  the pool and referenced into a device setlist (created if absent), in
  bundle order. Imported presets are recorded in the tone library as
  pathless tones (source `import-hss`) + setlist membership, so a later
  `device sync <setlist>` keeps their references. If the destination setlist
  held references helixgen does NOT track, a later targeted sync of that
  (now manifest-tracked) setlist will strip those untracked references —
  inherent managed-mirror semantics. NOT idempotent on retry —
  re-running after a partial failure duplicates the already-succeeded slots;
  clean up (or use a fresh setlist) before retrying. The filled-slot byte
  framing is an inferred assumption, not yet confirmed against a real
  non-empty export — see `src/helixgen/device/hss.py`.
- IRs: `device list-irs / push-ir / pull-ir`, `device delete-ir
  <name-or-hash>`, `device rename-ir <name-or-hash> <new>`, and
  `device ir-prune [--yes] [--force] [--ignore-warnings] [--only …]` — delete
  IRs no preset references (dry-run by default; IRs referenced only by local
  off-device presets need `--force`; proceeding over unverifiable-local-tone
  warnings needs `--ignore-warnings` — a separate consent).
- Global settings: `device settings list|get|set` (161 `global.*` keys).
