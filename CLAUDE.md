# helixgen

CLI that generates Line 6 Helix Stadium `.hsp` presets (and legacy `.hlx`) from
JSON tone specs. The library lives at `~/.helixgen/library/` (override with
`$HELIXGEN_LIBRARY`) and is built by ingesting real device exports.

## CLI

- `helixgen list-blocks [--category amp|cab|drive|delay|reverb|modulation|filter|eq|dynamics|pitch|volume|send]` — list blocks, optionally filtered.
- `helixgen show-block "<name>"` — print a block's exact param names, types, defaults, and observed ranges. **Run this before writing a spec** — param names are case-sensitive and the generator rejects unknown ones.
- `helixgen generate <spec.json> -o <out.hsp>` — generate a preset. The `-o` flag is required. Output extension `.hsp` writes a Stadium-format file (8-byte magic + compact JSON); `.hlx` writes pretty JSON for the original Helix.
- `helixgen ingest <path>` — ingest a `.hsp`/`.hlx`/`.json` file or recurse a directory; first encountered file sets the chassis.

## spec.json shape

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

## Generation notes

- The chassis is whatever was first ingested. A Stadium chassis (`_helixgen_chassis_shape: "hsp"`) produces `.hsp` output; a `.hlx` chassis produces `.hlx`. Carryover `meta.color` / `meta.info` / `device_id` from the originating export is currently expected.
- Some Stadium model IDs are translated on ingest (e.g. `HD2_DistScream808Mono` → `HD2_DrvScream808`); generate translates back when writing `.hsp`.
- If the param validator fails with a list of valid names, run `show-block` and correct the spec — don't guess.

## Project layout

- `src/helixgen/` — `cli`, `ingest`, `hsp`, `chassis`, `library`, `spec`, `generate`, `bootstrap`
- `tests/` — pytest suite (172 tests, run with `pytest`)
- `tests/fixtures/` — synthetic + real-export fixtures
- `data/` (gitignored) — the user's personal `.hsp` exports
- `docs/superpowers/plans/` — implementation plan history

## Development conventions

- TDD throughout: failing test first, then minimal implementation. See existing test files for the established pattern.
- Pure stdlib + `click` for the CLI; no other runtime deps.
- Real-export fixtures live in `tests/fixtures/presets/` and are loaded by tests under skip-if-not-present guards so the suite stays green on a clean clone.
