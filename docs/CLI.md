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

For the full spec surface — input routing, snapshots, footswitch assignment,
expression pedal targets, per-block IR references — see the project
[`CLAUDE.md`](../CLAUDE.md) which documents every field.

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
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.
- `helixgen bootstrap` — clone sensorium/phelix and ingest its `blocks/` folder.
- `helixgen register-irs <wav1> <wav2> ...` — compute each WAV's Stadium hash and register. Use `--force` to overwrite existing mappings.
- `helixgen register-irs <preset.hsp> <wav1> <wav2> ...` — bind each unknown `irhash` in the preset (path-then-position order) to the corresponding wav arg.
- `helixgen ir-scan <dir>... [--rescan] [--remove <basename>]` — recursively walk one or more directories for `*.wav`, compute each Stadium hash, and cache.
- `helixgen list-irs` — print `<hash>  <wav-path>` for every registered IR.
