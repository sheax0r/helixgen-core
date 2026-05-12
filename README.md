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

Open this repo in Claude Code, seed the library once (step 1 below), then type something like:

> `/tone Les Paul with stock humbuckers, classic rock / hard rock. Make me one preset with three snapshots: a clean intro, a Plexi crunch for verses, and a singing lead for solos — Slash / Joe Bonamassa territory.`

A good prompt usually includes (a) your guitar (model and pickup type are most useful), (b) the musical style or a band/song reference, and (c) the role(s) you need. The skill will ask you for anything missing.

What the skill does: drafts a spec, runs `helixgen generate`, and reports back with the chain, your guitar-side knob/selector settings, the file path, and one suggested tweak after you load it. Multi-part requests ("rhythm + lead", "verse + chorus + solo") are bundled into snapshots automatically; fundamentally different sounds get split into separate presets.

**Iterate on the tone.** Generation is the start, not the end. After you load the preset on your device, come back to the same Claude Code session and describe what's off — *"the lead is too compressed,"* *"verses are too dark, more sparkle,"* *"swap the delay for something shorter and slappier,"* *"clean snapshot needs a touch of room reverb."* Claude will adjust the spec, regenerate, and tell you what changed so you can A/B against the previous version. Same `.hsp` filename by default, so you just re-import.

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

## Loading presets onto your device

helixgen produces files — it does **not** talk to the hardware directly. To get a generated preset onto your Stadium / Helix you go through Line 6's official desktop app.

**Default output location:**
- The `helixgen generate` CLI requires `-o <path>` — it writes wherever you point it; there is no default.
- The `/tone` Claude Code skill writes to `/tmp/<slug>.hsp` by default. Move it somewhere durable (e.g. `~/Documents/Helix Presets/`) before you reboot if you want to keep it.

**To load on the device:**

1. Connect your Stadium / Helix to your computer via USB.
2. Open Line 6's **HX Edit** application (or whichever Helix management app matches your device — check Line 6's downloads page if unsure).
3. Use the app's import / open command to load the `.hsp` (or `.hlx`) file.
4. Save the loaded preset to a slot on the device.

If HX Edit refuses to open the file, double-check that the chassis in your library matches your hardware (Stadium chassis → `.hsp`, legacy Helix chassis → `.hlx`).

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
