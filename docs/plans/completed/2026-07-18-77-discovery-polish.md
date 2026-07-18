# Plan: #77 remainder — discovery polish nits

## Context

Implements the remaining local polish nits from backlog #77 (helix workspace
`BACKLOG.md`; deferred from the 0.27.0 discovery batch, recorded in core PR
#20's body). All five are local-only — no device required; live tests
auto-skip. Repo rules apply: TDD (failing test first), stdlib + click only,
agent-facing surfaces (verb `--help`, `CLAUDE.md`, `docs/CLI.md`) updated in
the same change. Do NOT touch the plugin repo — the helix workspace sequences
a companion PR there after this merges. One task = one declared behavior
change (`--ip ""` rejection); the rest are additive.

### Task 1: Persist and reuse a discovered nonstandard RPC port

- [x] Read the discovery/record code (`discover` verb, `~/.helixgen/devices/` records) to find where the RPC port is assumed standard
- [x] Write failing test(s): a discovery record carrying a nonstandard RPC port is persisted with that port, and subsequent connection resolution uses the persisted port
- [x] Implement the minimal change
- [x] Update agent-facing surfaces the change touches: verb `--help`, `CLAUDE.md`, `docs/CLI.md`

### Task 2: `discover --forget` (record pruning)

- [x] Write failing test(s): `helixgen device discover --forget <serial-or-name>` removes the matching persisted record; unknown target = clear error, exit nonzero; record dir absent = clear error, not a traceback
- [x] Implement the minimal change
- [x] Update `--help`, `CLAUDE.md`, `docs/CLI.md` (new flag is an agent-facing surface)

### Task 3: Failure message respects `$HELIXGEN_HOME`

- [x] Write failing test: with `HELIXGEN_HOME` set, the discovery failure message that today hardcodes `~/.helixgen/devices/` shows the effective path instead
- [x] Implement the minimal change (use the effective home resolution, do not fix unrelated `HELIXGEN_HOME` gaps — that's backlog #73)

### Task 4: Live-conftest serial tie-break

- [x] Read `tests/live/` conftest device-selection logic
- [x] Write failing test: when multiple discovery records are candidates, selection tie-breaks deterministically by serial (document the ordering in the conftest docstring)
- [x] Implement the minimal change

### Task 5: Reject `--ip ""` (declared behavior change)

- [x] Write failing test(s): `--ip ""` (and whitespace-only) is rejected with a clear message and nonzero exit wherever `--ip` is accepted
- [x] Implement the minimal change
- [x] Note the behavior change in `docs/CLI.md` and the verb `--help` if wording changes

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`, which pins the agent-facing `--help`
  contract). Live tests under `tests/live/` auto-skip without
  `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.
