# helixgen

A Claude Code plugin that generates Line 6 Helix Stadium presets from natural-language tone descriptions. Ask in plain English — *"Plexi crunch for my Strat, push it for the lead"* — and get a working `.hsp` file you can drop onto your device.

![Demo: register an IR, ask Claude /tone for a U2 "Streets" rhythm clean, generate the .hsp](docs/demo.gif)

> ⚠️ **Unofficial tool — use at your own risk.** Not affiliated with or endorsed by Line 6 / Yamaha (see the [Trademark notice](#trademark-notice) below). helixgen produces preset files that you import via HX Edit; loading any user-generated preset on your hardware carries non-zero risk — rejected loads, corrupted preset slots, on-device crashes, or other behavior we haven't seen. Review what you import. The MIT license under which helixgen is distributed disclaims all warranty; see [LICENSE](LICENSE).

## Install

helixgen is a Claude Code plugin backed by a Python package. You need both:

```
/plugin marketplace add sheax0r/helixgen
/plugin install helixgen@helixgen
```

```bash
pip install git+https://github.com/sheax0r/helixgen.git@stable
```

The plugin contributes the `/tone` skill, a `setup` skill, and an MCP server. The pip package is the actual generator the MCP server invokes; without it the MCP tools can't import their handler module. The pip install pins to the same `stable` branch the plugin install does, so the two always agree on version.

## Use it

In any Claude Code session, type something like:

> `/tone Les Paul with stock humbuckers, classic rock / hard rock. Make me one preset with three snapshots: a clean intro, a Plexi crunch for verses, and a singing lead for solos — Slash / Joe Bonamassa territory.`

A good prompt usually includes (a) your guitar (model and pickup type are most useful), (b) the musical style or a band/song reference, and (c) the role(s) you need. The skill will ask you for anything missing.

What the skill does: drafts a spec, runs the generator, and reports back with the chain, your guitar-side knob/selector settings, the file path, and one suggested tweak after you load it. Multi-part requests ("rhythm + lead", "verse + chorus + solo") are bundled into snapshots automatically; fundamentally different sounds get split into separate presets.

**Iterate on the tone.** Generation is the start, not the end. After you load the preset on your device, come back to the same Claude Code session and describe what's off — *"the lead is too compressed,"* *"verses are too dark, more sparkle,"* *"swap the delay for something shorter and slappier,"* *"clean snapshot needs a touch of room reverb."* Claude will adjust the spec, regenerate, and tell you what changed so you can A/B against the previous version. Same `.hsp` filename by default, so you just re-import.

## Impulse Responses (IRs)

helixgen supports user IRs in Stadium presets — `With Pan` blocks (and the rest of the `HX2_ImpulseResponse*` family) can reference a `.wav` file by basename, and helixgen will resolve it to the 32-character `irhash` the device expects. Stadium identifies user IRs by a content-derived hash, not by filename or slot. helixgen reproduces that hash bit-identically without any device round-trip, so you can register an entire IR library once and reference IRs by name in any spec the `/tone` skill writes.

In Claude Code, ask the skill to register an IR — it can call the MCP `register_ir` tool (one file) or `register_irs` (a whole directory, in one round-trip) without needing Bash permission. Memory will remember your IR directory after the first time.

**Caveat:** for the `irhash` in a generated preset to actually resolve on the device, the matching WAV must also be loaded onto the device via the Helix Stadium app's **Librarian → Cab IRs → Import**. helixgen only handles the preset side; importing IRs onto the device is the Stadium app's job. If a slot displays "No Model" on the device after loading a preset, that IR wasn't imported.

See [`docs/ir-hash-algorithm.md`](docs/ir-hash-algorithm.md) for the hash algorithm and the field-validated reference implementation.

## Loading presets onto your device

helixgen produces files — it does **not** talk to the hardware directly. To get a generated preset onto your Stadium / Helix you go through Line 6's official desktop app.

The `/tone` skill writes to `/tmp/<slug>.hsp` by default. Move it somewhere durable (e.g. `~/Documents/Helix Presets/`) before you reboot if you want to keep it.

**To load on the device:**

1. Connect your Stadium / Helix to your computer via USB.
2. Open Line 6's **HX Edit** application (or whichever Helix management app matches your device — check Line 6's downloads page if unsure).
3. Use the app's import / open command to load the `.hsp` (or `.hlx`) file.
4. Save the loaded preset to a slot on the device.

If HX Edit refuses to open the file, double-check that the chassis in your library matches your hardware (Stadium chassis → `.hsp`, legacy Helix chassis → `.hlx`).

## CLI

helixgen ships a Python CLI for direct generation, library inspection, IR management, and ingesting your own preset exports to grow the block library. The Claude Code plugin uses the same code under the hood — most users won't need to reach for the CLI directly.

See [`docs/CLI.md`](docs/CLI.md) for the full surface: install, spec format, all subcommands, IR registration, library location.

## Limitations (v1)

- **Device validation:** `.hsp` output has been load-tested on a Helix **Stadium XL** and works. The non-XL **Helix Stadium** uses the same `.hsp` format and should work but is **untested** — the chassis baked into your library carries the device_id of whichever Stadium variant first exported a preset into it, so a chassis built from XL exports might or might not load cleanly on a non-XL Stadium. `.hlx` output is code-complete and round-trips through the parser and a real HX Edit export fixture, but has **never been loaded on a legacy Helix** (Floor / LT / Rack / Native) — treat it as plausibly-working-but-unverified until someone confirms.
- Single serial chain per DSP; no parallel A/B routing yet (see `docs/features/parallel-paths.md`).
- Wire values only — no display-value (0–10) translation.
- Output is not byte-identical to HX Edit's exports; it aims to load correctly.
- Footswitch assignment is not generated; assign on-device after loading.

## Acknowledgments

helixgen leans **heavily** on [**sensorium/phelix**](https://github.com/sensorium/phelix) — a community-maintained, hand-curated repository of Helix block JSON files. The `helixgen bootstrap` command clones phelix and ingests its `blocks/` directory; without that pre-extracted catalog the cold-start experience of this tool would be considerably worse.

## Trademark notice

helixgen is an unofficial community project. **Line 6**, **Helix**, **HX**, and related product names are trademarks of **Yamaha Guitar Group, Inc.** helixgen is not affiliated with, endorsed by, or sponsored by Line 6 or Yamaha. References in this project to Line 6 hardware, file formats (`.hlx`, `.hsp`), and model identifiers are descriptive — helixgen generates files intended to be compatible with Line 6 Helix devices but is not a Line 6 product.

If you are a representative of Line 6 / Yamaha and have concerns about this project's name or scope, please open an issue.

## Tests

```bash
pytest
```
