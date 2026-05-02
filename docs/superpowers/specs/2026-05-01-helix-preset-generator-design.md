# Helix Preset Generator — v1 design

**Date:** 2026-05-01
**Status:** Approved (pending user review of this written spec)
**Source brief:** `input.md` (in repo root)

## Goal

A Python CLI that programmatically generates Line 6 Helix `.hlx` preset files
from a structured JSON tone description. The tool also builds up a reusable
library of Helix block schemas by ingesting real exported presets, so over
time the user can generate presets for models the tool has already seen
without needing fresh exports.

## Two-layer system (only Layer 1 is in scope here)

This spec covers **Layer 1**, the deterministic compiler:

```
                    ┌─────────────────┐
   .hlx exports ───▶│   ingest        │──▶  block library
                    └─────────────────┘     (~/.helixgen/library/)
                                                    │
   tone-spec.json ──┐                               │
                    ▼                               │
                ┌─────────────────┐                 │
                │   generate      │◀────────────────┘
                └─────────────────┘
                        │
                        ▼
                   output.hlx
```

**Layer 2** is a Claude skill (lives outside this repo, in
`~/.claude/skills/`) that translates natural-language tone goals
("Goldfinger Superman rhythm tone for my Strandberg") into the structured
JSON spec that Layer 1 consumes. Layer 2 will get its own spec after Layer 1
ships and the input format is stable.

## Non-goals (v1)

- Parallel A/B routing inside a single DSP chain (deferred — see
  `docs/features/parallel-paths.md`)
- Snapshot variations beyond a single default snapshot
- Translating display values (Drive 6 on a 0–10 dial) to wire values (0.6
  on a 0–1 float). v1 takes wire values directly. Display-value mapping is
  Layer 2's job (and a future v2 enhancement here once we have multiple
  exemplars per block to triangulate the mapping).
- A/B preset-diff tool
- Python DSL wrapper (`Preset(amp="Brit 2204", drive=6, ...)`)
- File formats beyond `.hlx` — no `.hls`, `.hlb`, `.hxb` support
- The Layer-2 Claude skill itself

## CLI surface

```
helixgen ingest <path> [--library DIR]
helixgen generate <spec.json> -o <out.hlx> [--library DIR]
helixgen list-blocks [--category CAT] [--library DIR]
helixgen show-block <name-or-id> [--library DIR]
helixgen bootstrap [--phelix-ref REF] [--library DIR]
```

- `--library DIR` overrides default `~/.helixgen/library/`. Also overridable
  via `HELIXGEN_LIBRARY` env var.
- `bootstrap` clones (or pulls) `sensorium/phelix` into
  `~/.helixgen/.cache/phelix/` and runs ingest against `phelix/blocks/`.
- `list-blocks` prints library contents grouped by category. `--category amp`
  filters.
- `show-block "Brit 2204"` prints the block's parameter schema (names,
  types, defaults) for spec authoring.
- Standard `--help` per command (Click handles this).
- Exit codes: `0` success, `1` user error, `2` internal error.

## Block library

Lives at `~/.helixgen/library/` by default. Layout:

```
library/
  blocks/
    amp/<model_id>.json
    cab/<model_id>.json
    drive/<model_id>.json
    reverb/<model_id>.json
    eq/<model_id>.json
    dynamics/<model_id>.json
    ... (one directory per block category encountered)
  index.json          # auto-rebuilt: display name + aliases → model_id
  chassis.json        # extracted minimal-preset shell, used as generation template
  meta.json           # firmware versions seen, ingest timestamps, source files
```

### Block file shape

```json
{
  "model_id": "HD2_AmpBrit2204Custom",
  "category": "amp",
  "display_name": "Brit 2204",
  "aliases": ["Marshall JCM800", "JCM 800"],
  "params": {
    "Drive":    { "type": "float",  "default": 0.5,  "observed_range": [0, 1] },
    "Bass":     { "type": "float",  "default": 0.5,  "observed_range": [0, 1] },
    "Master":   { "type": "float",  "default": 0.5,  "observed_range": [0, 1] },
    "High Cut": { "type": "int",    "default": 8000, "observed_range": [20, 20000], "unit": "Hz" },
    "Mic":      { "type": "enum",   "default": "57 Dynamic", "values": ["57 Dynamic", "121 Ribbon", "421 Dynamic", "67 Condenser"] }
  },
  "exemplar": { /* full block JSON as exported, used as composition template */ },
  "first_seen": {
    "preset": "MyPresets/SomeRhythm.hlx",
    "firmware": "3.71",
    "date": "2026-05-01"
  }
}
```

The block files on disk are the source of truth. `index.json` is derived and
rebuilt after every ingest run.

### Identifying blocks

- **Canonical key:** `model_id` (the internal Helix string from the exported
  JSON).
- **Lookup:** specs reference blocks by `display_name` or alias. The index
  resolves names to `model_id`. Unmatched names fall back to a literal
  `model_id` match. Ambiguous names (multiple matches) are an error listing
  the candidates.
- **Display name extraction:** if the exported block JSON includes a
  human-readable name field, use it. Otherwise humanize the `model_id`
  (`HD2_AmpBrit2204Custom` → `"Brit 2204"`). Users can hand-edit
  `display_name` and `aliases` in any block file and the change is picked up
  on the next index rebuild.
- **Category determination:** prefer an explicit category field in the
  exported block JSON if present. Otherwise infer from the `model_id`
  prefix (`HD2_Amp...` → `amp`, `HD2_Cab...` → `cab`, `HD2_Rvb...` →
  `reverb`, `HD2_Dly...` → `delay`, `HD2_Drv...` or `HD2_Dist...` →
  `drive`, `HD2_EQ...` → `eq`, `HD2_Dyn...` → `dynamics`, `HD2_Mod...` →
  `modulation`, `HD2_Pitch...` → `pitch`, `HD2_Wah...` → `filter`). If
  neither yields a match, the block lands in `blocks/uncategorized/` and
  the user can move and recategorize the file by hand. The inferred map
  is configurable in code so we can extend it as new prefixes appear.
- **`observed_range`** widens as more exemplars are seen — it is
  descriptive (the range of values we have *observed*), not prescriptive.
  It is metadata for inspection and Layer 2 use, never used to reject
  generation values.

### Wire values, not display values

"Wire value" means **the value as it appears in the exported JSON, whatever
its type**: a 0–1 float for amp gain, an integer Hz for cut frequencies, a
string like `"57 Dynamic"` for mic types, a seconds float for reverb decay.
Wire values are not normalized to a single scale.

The brief warns explicitly that the mapping between display values (e.g.
`Drive 6` on a 0–10 dial) and wire values (e.g. `0.6` on a 0–1 float) cannot
be guessed. v1 sidesteps this by accepting wire values directly in specs.
Display-value translation is Layer 2's responsibility.

### Chassis

Rather than constructing the full preset JSON shell from scratch (which has
many metadata fields, signal routing scaffolding, snapshot definitions,
etc.), we extract a "chassis" on first ingest: a deep-copy of one ingested
preset with all blocks removed but routing/meta scaffolding preserved.
Generation inserts blocks into the chassis copy.

The chassis is single-DSP, serial-only, no A/B split. A separate parallel
chassis is required when the parallel-paths feature is implemented (see
`docs/features/parallel-paths.md`).

## Input spec format (consumed by `generate`)

JSON, intentionally strict (no comments — Layer 2 generates these
mechanically and the user wants strict validation over conveniences).

```json
{
  "name": "Goldfinger Superman Rhythm",
  "author": "mike",
  "paths": [
    {
      "input":  "Multi",
      "output": "Multi",
      "blocks": [
        { "block": "Noise Gate",        "params": { "Threshold": 0.40, "Decay": 0.30 } },
        { "block": "Scream 808",        "params": { "Drive": 0.10, "Tone": 0.50, "Level": 0.60 } },
        { "block": "Brit 2204",         "params": { "Drive": 0.60, "Bass": 0.50, "Mid": 0.75, "Treble": 0.55, "Presence": 0.55, "Master": 0.60, "Ch Vol": 0.50 } },
        { "block": "4x12 Greenback 25", "params": { "Mic": "57 Dynamic", "Distance": 0.10, "Axis": "12° off", "High Cut": 8000, "Low Cut": 80 } },
        { "block": "Parametric EQ",     "params": { "Freq 1": 350, "Gain 1": -2, "Freq 2": 2800, "Gain 2": 2, "Freq 3": 7500, "Gain 3": -2 } },
        { "block": "Plate Reverb",      "params": { "Mix": 0.10, "Decay": 1.2, "Pre-delay": 0.010 } }
      ]
    }
  ]
}
```

### Field rules

- `name` — required. Becomes `data.meta.name` in the output.
- `author` — optional. Embedded in `data.meta` if present.
- `paths` — required, array, length 1 or 2. First entry maps to `dsp0`,
  second (if present) to `dsp1`.
- Each path:
  - `input`, `output` — optional. If omitted, the chassis defaults are used.
  - `blocks` — required, array of block entries. Order is signal flow.
- Each block entry:
  - `block` — required. Display name (preferred) or `model_id`.
  - `params` — optional. Dict of param-name → wire value. Missing params
    fall back to the block exemplar's defaults.

### Forward-compatibility for parallel A/B (not implemented in v1)

The block-entry shape is forward-compatible with parallel routing. A future
parallel section replaces a single block entry with a `parallel` entry whose
value is two sub-chains. v1 generators must reject any spec containing
`parallel` with a clear "not yet supported in v1" error rather than silently
flattening. Full design in `docs/features/parallel-paths.md`.

## Ingest behavior

`helixgen ingest <path>` where `<path>` is a file or directory.

- **Single file:**
  - Auto-detect input shape:
    - **Full preset** (has `version`, `schema`, `data.tone.dsp0.blocks`) →
      walk both DSP path block dicts.
    - **Single-block file** (top-level looks like one block — `model_id` and
      params at the top) → ingest as one block directly.
  - This dual mode supports both real `.hlx` exports and pre-extracted block
    files like those in `sensorium/phelix`.
- **Directory:** walk recursively, ingest every `.hlx` and `.json`. Skip
  files that don't parse or don't match either shape, with a warning per
  skipped file.
- **First-ever ingest** also extracts the chassis: deep-copy the first
  successfully-parsed full preset, strip its blocks, save as `chassis.json`.
  Single-block files alone never produce a chassis.
- **Dedup by `model_id`:**
  - First sighting of a `model_id` → write the block file.
  - Subsequent sighting → compare param keys + types against the existing
    block. Match → skip ("already in library"). Mismatch → write
    `<model_id>.v2.json` (or v3, v4, …) and log a "conflict" warning.
- **Index rebuild** after every ingest run: re-derive `index.json` from
  block files on disk.
- **Idempotency:** running ingest on the same files twice is a no-op aside
  from timestamp metadata.

### Output summary

```
Ingested 12 presets from ~/MyPresets/
  +14 new blocks (3 amp, 4 cab, 3 drive, 2 reverb, 2 eq)
   8 already in library
   1 conflict — see ~/.helixgen/library/blocks/amp/HD2_AmpUSDeluxe.v2.json
```

### Verify-on-first-run

The exact field shape of `sensorium/phelix`'s individual block JSON files is
not yet known to the implementation team. The first `helixgen bootstrap`
run during development may reveal that phelix wraps blocks in an outer
metadata object, uses different field names, or requires a small adapter.
Treat this as a known unknown — implement the obvious shape first, run
bootstrap against a test phelix checkout, fix what doesn't fit.

## Generate behavior

`helixgen generate <spec.json> -o <out.hlx>`

Pipeline:

1. **Load spec** — parse JSON, validate top-level shape (required fields,
   types, `paths` length 1 or 2, no `parallel` entries).
2. **Resolve blocks** — for each `block` reference, look up via `index.json`
   (display name → `model_id`), then load
   `library/blocks/<category>/<model_id>.json`. Hard-fail with a clear
   message if any block is missing or any name is ambiguous.
3. **Validate params** — each param key in the spec must exist in the
   block's schema. Hard-fail listing all unknown keys (typo protection).
   Value types are not strictly checked (we lack authoritative ranges).
4. **Build chassis copy** — deep-copy `chassis.json` as the working preset.
5. **Place blocks** — for each chain in `paths`:
   - Deep-copy each block's `exemplar`.
   - Overlay user-specified params on top of the exemplar.
   - Insert into `data.tone.dspN.blocks` in chain order, with whatever
     position-key convention the chassis uses (this convention is
     discovered during chassis extraction and recorded in `meta.json`).
   - If `input`/`output` are set on the chain, overlay onto the chassis's
     dsp-level config.
6. **Set meta** — `data.meta.name = spec.name`. If `author` is set, write it
   to `data.meta.author`. Write a provenance object to
   `data.meta.helixgen` containing the helixgen version, the source spec
   filename, and an ISO-8601 timestamp. We embed under `data.meta` because
   that's where Helix already keeps free-form preset metadata; unknown
   fields under `data.meta` should be preserved by HX Edit on round-trip
   (verify on first real import).
7. **Write** — pretty-printed JSON to the output path.

### Validation philosophy

Structural validation only: block exists, params exist, shape is correct.
Value-range validation is not attempted — Helix's authoritative range for
any given param is not knowable from a single exemplar, and we don't want
to reject specs Helix would actually accept. Out-of-range values are
Helix's problem to reject on import.

### Output is not byte-identical to HX Edit

Per the brief: aim for "loads correctly," not "diff-clean." Field ordering,
whitespace, and unset optional fields will differ from HX Edit's output.
Round-trip equivalence is checked structurally (same blocks, same params),
not via string diff.

## Error handling

All errors are loud, named, and actionable. Examples:

- `Block "Brit 2204" not found in library at ~/.helixgen/library/. Try `helixgen ingest <export.hlx>` or `helixgen bootstrap`.`
- `Unknown param "Drive2" for block "Brit 2204". Known params: Drive, Bass, Mid, Treble, Presence, Master, Ch Vol.`
- `Spec at tone.json: "paths" must be an array, got object.`
- `Spec at tone.json: "paths" length 3 not supported (max 2 — one per DSP).`
- `Spec at tone.json: "parallel" entries not supported in v1. See docs/features/parallel-paths.md.`
- `Block name "Plate Reverb" matches multiple library entries: HD2_RvbPlate, HD2_LegacyPlateReverb. Use the model_id explicitly.`

No silent fallbacks, no defaults applied for things the user clearly asked
for explicitly.

## Testing

- `pytest`. Tests in `tests/`.
- `tests/fixtures/presets/` — real `.hlx` files exported by the user, used
  as ingest inputs and round-trip targets.
- `tests/fixtures/specs/` — JSON specs we expect to compile, including the
  Goldfinger reference preset.
- **Unit tests:** spec validation, block resolution (including ambiguity
  and missing-block errors), schema diffing for the conflict path, chassis
  stripping, single-block vs full-preset auto-detection.
- **Integration tests:** full ingest of a fixture directory, full generate
  from a spec, round-trip (ingest a real preset → derive a spec from it →
  generate from that spec → re-ingest → assert the library state is
  unchanged and the resulting block exemplars match structurally).
- **Manual check:** "loads in HX Edit" stays manual, since it requires the
  user's hardware/software in the loop. The Goldfinger preset is the
  canonical manual acceptance test for v1.

## Project layout

```
helix-preset-generator/
  pyproject.toml            # package metadata, click dep, helixgen entry point
  README.md                 # quickstart, written after implementation
  src/helixgen/
    __init__.py
    cli.py                  # click commands
    library.py              # block library read/write, index rebuild
    ingest.py               # parse .hlx → block extraction, shape detection
    generate.py             # spec → .hlx
    spec.py                 # spec schema + validation
    chassis.py              # chassis extraction + composition
    bootstrap.py            # phelix clone + ingest
  tests/
    fixtures/
      presets/              # user-provided .hlx files
      specs/                # JSON specs, including Goldfinger
    test_ingest.py
    test_generate.py
    test_spec.py
    test_library.py
    test_chassis.py
    test_bootstrap.py
    test_cli.py
  docs/
    features/
      parallel-paths.md     # already exists
    superpowers/specs/
      2026-05-01-helix-preset-generator-design.md   # this file
  input.md                  # original brief, kept for reference
```

## Implementation language and dependencies

- Python 3.11+
- Runtime deps: `click` (CLI). That is the only required runtime dep.
- Dev deps: `pytest`.
- We do not import any prior-art repo's code. We may *ingest* their data
  (phelix's blocks/ folder) via `helixgen bootstrap`, but no Python
  dependency is taken on those projects.

## Acceptance criteria for v1

1. `helixgen bootstrap` clones phelix and ingests `phelix/blocks/` into a
   non-empty library. If phelix's block-file shape requires an adapter, the
   adapter is implemented and tested; otherwise ingest's auto-detection
   handles it directly.
2. `helixgen ingest <user-export.hlx>` extracts blocks from a real exported
   preset, populates the library and the chassis on first run, and is
   idempotent on second run.
3. `helixgen generate tests/fixtures/specs/goldfinger.json -o /tmp/gf.hlx`
   produces a `.hlx` file that imports into HX Edit without errors and
   plays audio when activated. Subjective fidelity to the Goldfinger
   Superman rhythm tone is the user's manual judgment, outside the
   automated acceptance bar.
4. All errors listed in the "Error handling" section above are reachable
   and produce the messages described.
5. Round-trip integration test passes: ingest a fixture preset, generate
   from a spec derived from it, re-ingest the result, library state is
   unchanged.
6. Test coverage of `ingest`, `generate`, `spec`, `library`, `chassis`,
   `bootstrap`. CLI smoke-tested via `click.testing.CliRunner`.

## Related documents

- `input.md` — original brief from the user.
- `docs/features/parallel-paths.md` — deferred feature, full design for
  parallel A/B routing extension.
