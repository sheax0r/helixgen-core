# Plan: lower the measure/normalize window default to 10 s

## Context

`device measure` and `device normalize` default `--seconds` to `20.0`. Field
use (live hardware, 2026-07-16 and again 2026-07-27) has consistently shown
10 s windows to be sufficient: the playing gate needs roughly 4 s of credited
playing per window (`--min-playing 40` at ~10 gated samples/sec), so a 10 s
window carries better than 2x margin. The plugin's `device` skill already
tells agents "`--seconds 10` suffices in practice — the default 20 is
conservative", but the CLI default and its `show_default` help text still say
20, so agents that read the command signature (rather than the field-guidance
prose 90 lines below it) brief the player for twice the playing they need. On
2026-07-27 that produced a session that told the player to expect ~3 minutes
of continuous playing for a 4-snapshot run that needs ~40 s.

Change the default, not the prose. Companion plugin PR resyncs the skill and
mirrored docs (helix workspace `BACKLOG.md`, measurement lineage #62/#82).

### Task 1: lower the default in both verbs

- [x] Write the failing test(s) first: assert the `--help` output of `device measure` and `device normalize` advertises `[default: 10.0]` for `--seconds` (follow the established `--help`-contract pattern in `tests/test_cli_parity.py`)
- [x] Change `default=20.0` to `default=10.0` on the `--seconds` option of `device_measure` (`src/helixgen/cli_device.py:3437`) and `device_normalize` (`src/helixgen/cli_device.py:3649`)
- [x] Grep the suite for any other test that pins the 20 s default (`grep -rn "20\.0" tests/`) and update whatever asserts it (only hit: `tests/test_library_group.py:627`, a recorded-run fixture payload, not a default assertion — no change needed)
- [x] Update every agent-facing surface the change touches: both verbs' `--help` text (the `show_default` renders it, but check the prose in the docstrings too), `docs/CLI.md:578` and `docs/CLI.md:579` (`[--seconds N=20]` -> `N=10` in both the `measure` and `normalize` entries)

### Task 2: fix the stale 20.0 in the library-metadata docstring

- [x] `src/helixgen/tone_meta.py:87` documents the `normalized` record's example payload with `"seconds": 20.0,  # per-target window (--seconds)` — update the example to `10.0` so the documented shape matches what a default run now records
- [x] Confirm no code reads that value as a constant (it is illustrative only) — if a test asserts the docstring example, update it (nothing reads it: no `__doc__` consumers; only other 20.0 hit is `tests/test_library_group.py:627`, a recorded-run fixture payload)

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`, which pins the agent-facing `--help` contract).
  Live tests under `tests/live/` auto-skip without `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

Opt-in (NOT part of default validation — requires a real Helix Stadium on the
LAN and mutates device state):

- `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and device_read" tests/live`
  — the measurement verbs' live coverage; a default-window run should still
  measure ok at 10 s.

## Release

- [x] Bump the version and tag `vX.Y.Z` (patch-level: a default change, no API
      change). Releases are preapproved from the coordination workspace; the
      tag fires the PyPI publish workflow. The plugin repo bumps its pin in
      its own companion PR afterwards — core releases first.
      (Version bumped to 0.32.1 in `pyproject.toml` + `src/helixgen/__init__.py`
      on this branch. Tag `v0.32.1` NOT pushed from here: the publish workflow
      fires on the tag, and tagging an unmerged worktree commit would publish
      unreviewed code. Coordinator pushes the tag on `main` after merge.)
