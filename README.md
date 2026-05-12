# helixgen

helixgen is **two things in one repo**:

1. A **Python CLI** that generates Line 6 Helix `.hsp` (Stadium) and `.hlx` (legacy) preset files from a strict JSON tone spec, and builds up a reusable library of block schemas by ingesting real exports.
2. A **Claude Code skill** at `.claude/skills/tone/` that drives the CLI from natural-language tone descriptions ("make me a Plexi crunch for my Strat verses, push it for the lead") — it clarifies, surveys the library, drafts the spec, runs the generator, and reports back with guitar-side settings.

You can use either piece on its own. The skill is the easier surface; the CLI is what you reach for if you want to tweak specs by hand or wire helixgen into other tooling.

> **Unofficial tool.** Not affiliated with or endorsed by Line 6 / Yamaha — see the [Trademark notice](#trademark-notice) below.

## Install

```bash
git clone https://github.com/sheax0r/helixgen
cd helixgen
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Quickstart

### A. With Claude Code (recommended)

Open this repo in Claude Code, seed the library once (step 1 below), then type:

> `/tone make me a Plexi crunch with my Strat for the verses, push it for the lead`

The skill asks any missing questions (guitar / role / reference), drafts a spec, runs `helixgen generate`, and reports the chain plus guitar-side settings (pickup selector, volume, tone). Multi-part requests like "rhythm + lead in one preset" are handled with snapshots automatically.

### B. CLI directly

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
- Each block has a `block` (display name or model_id) and optional `params` (wire values: 0–1 floats for amp gain, integer Hz for cut frequencies, strings for enums like mic types).

Full design: `docs/superpowers/specs/2026-05-01-helix-preset-generator-design.md`.

## Library location

Default: `~/.helixgen/library/`. Override with `--library DIR` or `HELIXGEN_LIBRARY` env var.

## Limitations (v1)

- **Device validation:** `.hsp` output has been load-tested on a Helix **Stadium** and works. Helix **Stadium XL** uses the same `.hsp` format and should work but is **untested**. `.hlx` output is code-complete and round-trips through the parser and a real HX Edit export fixture, but has **never been loaded on a legacy Helix** (Floor / LT / Rack / Native) — treat it as plausibly-working-but-unverified until someone confirms.
- Single serial chain per DSP; no parallel A/B routing yet (see `docs/features/parallel-paths.md`).
- Wire values only — no display-value (0–10) translation.
- Output is not byte-identical to HX Edit's exports; it aims to load correctly.
- Footswitch assignment is not generated; assign on-device after loading.

## Acknowledgments

helixgen leans **heavily** on [**sensorium/phelix**](https://github.com/sensorium/phelix) — a community-maintained, hand-curated repository of Helix block JSON files. The `helixgen bootstrap` command clones phelix and ingests its `blocks/` directory; without that pre-extracted catalog the cold-start experience of this tool would be considerably worse, and a meaningful share of the block coverage you get out-of-the-box comes from their work.

If you find helixgen useful, please give [sensorium/phelix](https://github.com/sensorium/phelix) a star, and if you find new blocks or schema corrections, contribute them back upstream first — that benefits everyone in the Helix tooling ecosystem, not just helixgen users.

## Trademark notice

helixgen is an unofficial community project. **Line 6**, **Helix**, **HX**, and related product names are trademarks of **Yamaha Guitar Group, Inc.** helixgen is not affiliated with, endorsed by, or sponsored by Line 6 or Yamaha. References in this project to Line 6 hardware, file formats (`.hlx`, `.hsp`), and model identifiers are descriptive — helixgen generates files intended to be compatible with Line 6 Helix devices but is not a Line 6 product.

If you are a representative of Line 6 / Yamaha and have concerns about this project's name or scope, please open an issue.

## Tests

```bash
pytest
```
