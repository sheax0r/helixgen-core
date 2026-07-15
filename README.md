# helixgen-core

The core Python library + CLI behind **helixgen**: generate Line 6 Helix
Stadium `.hsp` presets (and legacy `.hlx`) from JSON tone recipes, edit them
surgically in place, manage impulse responses by Stadium-exact content hash,
and control a Helix Stadium directly over the LAN (OSC-over-ZeroMQ — no
editor app).

> ⚠️ **Unofficial tool — use at your own risk.** Not affiliated with or
> endorsed by Line 6 / Yamaha (see [Trademark notice](#trademark-notice)).
> Loading any user-generated preset on your hardware carries risk — rejected
> loads, corrupted preset slots, on-device crashes. Review what you import.
> The MIT [LICENSE](LICENSE) disclaims all warranty.

## Repo family

| Repo | What it is |
|---|---|
| **helixgen-core** (this repo) | The `helixgen` Python package: libs, CLI, MCP server |
| [helixgen](https://github.com/sheax0r/helixgen) | The Claude Code plugin — `/tone`, `/setup`, `/device` skills + marketplace |
| [helixgen-tui](https://github.com/sheax0r/helixgen-tui) | Terminal UI for tones, setlists, and device control |

Want natural-language preset generation inside Claude Code? Install the
[plugin](https://github.com/sheax0r/helixgen) — you don't need this repo
directly. This repo is for using the CLI/library standalone, or developing
against it.

## Install

Requires **Python 3.11+**. Not yet published to PyPI (coming); install from
source for now:

```bash
pip install 'helixgen[device] @ git+https://github.com/sheax0r/helixgen-core'
```

Extras: `device` (network device control: pyzmq, msgpack, paramiko), `mcp`
(the MCP server), `dev` (pytest).

A standalone install starts with an empty block library — seed it first:

```bash
helixgen bootstrap
```

Computing IR hashes from WAVs (`register-irs <wav>`, `ir-scan`) additionally
needs **libsndfile** (`brew install libsndfile` / `apt install libsndfile1`).

## Quick tour

```bash
helixgen list-blocks --category amp        # browse the block library
helixgen show-block "Brit Plexi Brt"       # exact param names/ranges
helixgen generate recipe.json -o tone.hsp  # author a preset from a recipe
helixgen set-param tone.hsp "Tape Echo Stereo" Mix 0.3   # surgical edit
helixgen view tone.hsp                     # read a .hsp back as a recipe
helixgen device list                       # talk to a Stadium on the LAN
helixgen device sync my-setlist            # mirror a managed setlist onto it
```

Full references:

- [`docs/CLI.md`](docs/CLI.md) — every verb, including the complete
  `helixgen device` reference.
- [`docs/recipe-reference.md`](docs/recipe-reference.md) — the exhaustive
  recipe schema (paths, splits, snapshots, footswitches, expression, MIDI,
  Command Center, IRs).
- [`docs/ir-hash-algorithm.md`](docs/ir-hash-algorithm.md) — the
  reverse-engineered Stadium IR hash, field-validated.
- [`docs/helix-protocol.md`](docs/helix-protocol.md) — the network protocol.

## Tests

Run from a source checkout with the package on `PYTHONPATH`:

```bash
PYTHONPATH=$PWD/src python -m pytest
```

## Acknowledgments

helixgen leans **heavily** on
[**sensorium/phelix**](https://github.com/sensorium/phelix) — a
community-maintained, hand-curated repository of Helix block JSON files. The
`helixgen bootstrap` command clones phelix and ingests its `blocks/`
directory; without that pre-extracted catalog the cold-start experience of
this tool would be considerably worse.

## Trademark notice

helixgen is an unofficial community project. **Line 6**, **Helix**, **HX**,
and related product names are trademarks of **Yamaha Guitar Group, Inc.**
helixgen is not affiliated with, endorsed by, or sponsored by Line 6 or
Yamaha. References in this project to Line 6 hardware, file formats (`.hlx`,
`.hsp`), and model identifiers are descriptive — helixgen generates files
intended to be compatible with Line 6 Helix devices but is not a Line 6
product.

If you are a representative of Line 6 / Yamaha and have concerns about this
project's name or scope, please open an issue.
