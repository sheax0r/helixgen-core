# Plan: <title>

<!--
ralphex plan scaffold. Copy to docs/plans/<yyyy-mm-dd>-<slug>.md, fill in,
then run `ralphex` against it. Keep tasks small and checkbox-granular —
ralphex works the unchecked boxes top to bottom and checks them off as it
goes. Move the finished plan to docs/plans/completed/ when done.
-->

## Context

One short paragraph: what this change is, why now, and any backlog entry it
implements (e.g. `docs/BACKLOG.md #NN`). Link relevant specs under
`docs/superpowers/specs/` if they exist. Remember the repo rules: TDD
(failing test first), stdlib + click only, agent-facing surfaces (verb
`--help`, `CLAUDE.md`, `docs/CLI.md`) updated in the same change.

### Task 1: <name>

- [ ] Write the failing test(s) first (see the matching `tests/test_*.py` for the established pattern)
- [ ] Implement the minimal change to make them pass
- [ ] Update every agent-facing surface the change touches: verb `--help` text, `CLAUDE.md`, `docs/CLI.md`

### Task 2: <name>

- [ ] ...

<!-- Add more `### Task N:` sections as needed. Defer anything punted to
docs/BACKLOG.md as a numbered entry, not a TODO comment. -->

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`, which pins the agent-facing `--help`
  contract). Live tests under `tests/live/` auto-skip without
  `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

Opt-in (NOT part of default validation — requires a real Helix Stadium on
the LAN and mutates device state; preapproved for test runs, but keep to
expendable slots and never leave the device broken):

- `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and <marker>" tests/live`
  — run only the impact-area marker(s) matching the change (`authoring`,
  `library`, `ir`, `device_read`, `device_write`, `liveops`, `setlists`,
  `sync`, `device_ir`, `locks`, `discover`; see `pyproject.toml` markers).
