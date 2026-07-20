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
| **helixgen-core** (this repo) | The `helixgen` Python package: libs + CLI (the engine's only surface) |
| [helixgen](https://github.com/sheax0r/helixgen) | The Claude Code plugin — `/tone`, `/setup`, `/device` skills + marketplace |
| [helixgen-tui](https://github.com/sheax0r/helixgen-tui) | Terminal UI for tones, setlists, and device control |

Want natural-language preset generation inside Claude Code? Install the
[plugin](https://github.com/sheax0r/helixgen) — you don't need this repo
directly. This repo is for using the CLI/library standalone, or developing
against it.

## Install

Requires **Python 3.11+**. Published to PyPI:

```bash
pip install 'helixgen[device]'
```

Extras: `device` (network device control: pyzmq, msgpack, paramiko),
`dev` (pytest).

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

### Live integration suite (`tests/live/`)

An opt-in suite that drives the **real CLI via subprocess** against the
user's real block library and a **real Helix Stadium on the LAN**. It is
skipped entirely (fast, green) unless `HELIXGEN_LIVE=1`:

```bash
HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest tests/live -q
```

Tests are grouped by impact area with registered markers — `authoring`,
`library`, `ir`, `device_read`, `device_write`, `liveops`, `setlists`,
`sync`, `device_ir` (plus `live` on everything and `live_global` for the
extra-gated global-settings write) — so a targeted change can run just its
blast radius, e.g.:

```bash
HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and sync" tests/live
```

Run the `device_write` module with the active preset deliberately **dirty**
(tweak a knob on the unit without saving). A dirty edit buffer is the state
that made `/CreateContent` answer status field 3 = `1`, which pre-0.30.0
clients misread as an error and "cleaned up" — a clean buffer answers `0` and
exercises the uninteresting path, so the #38 regression guard only guards
under those conditions. See each module's docstring.

Safety is enforced by fixtures: all local state (manifest, IR mapping,
IR-hash cache, prefs, backups) is redirected to a scratch dir; an upfront
`device backup` runs; device state is diffed before/after (the suite fails
itself on any leak); every artifact is `HGTEST`-prefixed and torn down even
on failure. See `tests/live/conftest.py` for the full safety model and the
list of deliberately excluded verbs.

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
