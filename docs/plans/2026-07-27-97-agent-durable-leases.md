# Plan: #97 — device leases that survive agent tool calls

## Context

Implements coordination-workspace `BACKLOG.md` **#97**, observed twice on live
hardware 2026-07-27 during a `--source loop` measurement workflow.

`device lock --scope all` records the invoking shell's pid as the lease's
`pid` (`src/helixgen/locks.py`, lease shape documented at the module head).
An agent takes the lock from one Bash tool call; that shell exits when the
call returns; `is_stale()` then sees a dead pid on this host and, after
`SESSION_PID_GRACE_S`, the lease is reclaimable. A contender (a `live-test-suite`
pytest run on the same machine) took `editbuffer` ~60 s into the workflow, and
a second contender took `irs` later the same session. `$HELIXGEN_LOCK_TOKEN`
does not help: `owned()` authenticates the token against a lease that has
already been broken and replaced, so the agent's later calls are simply
unauthenticated newcomers.

The dangerous part is the asymmetry, not the contention. When the reclaim
happened mid-workflow:

- `device snapshot 0` (mutating) refused on the lock — correct, visible;
- `device measure` (read-only, takes no lock) ran anyway and returned a
  well-formed measurement **of whatever snapshot happened to be active**.

The agent had asked for snapshot 0, been denied, and been handed real-looking
numbers for an unknown snapshot. Both occurrences were caught only because the
recall error printed in the same combined output; an unattended run would have
written trims fitted to the wrong target. Silent wrong data is worse than a
failed run.

Agent-driven device work is precisely the workload leases exist for, and it is
the workload they currently cannot serve.

### Task 1: characterize the current behavior with failing tests

- [ ] Write the failing test(s) first, following the established patterns in `tests/test_locks.py`
- [ ] Test: a lease whose recorded pid is dead is reclaimed by a contender after `SESSION_PID_GRACE_S`, and a subsequent call presenting the original `$HELIXGEN_LOCK_TOKEN` is NOT recognized as the owner (this is the #97 bug, and pinning it prevents a regression later reintroducing silent takeover)
- [ ] Determine and pin whether a token-authenticated call currently RENEWS the lease (`_renew()` exists at `src/helixgen/locks.py:345`). If sibling calls already renew, establish why the observed lease still went stale — the workflow made calls well inside the grace window, so either renewal is not wired to token-authenticated use or the pid-death check short-circuits it. Write down the answer in the plan/PR; do not guess

### Task 2: a lease an agent can actually hold

- [ ] Provide a way to take a lease not bound to the invoking shell's lifetime. Preferred shape: `device lock --detach` (records `pid: None`, TTL-only, explicitly released via `device unlock`); an alternative is `--pid <pid>` to bind the lease to a caller-supplied long-lived process. Pick ONE and justify it in the PR
- [ ] A detached lease must NOT be able to brick the device indefinitely: default it to a materially shorter TTL than the 7200 s session default, keep `device unlock` working, and keep an explicit force/break path for a genuinely orphaned lease
- [ ] Renew a detached lease on each token-authenticated use, so an active workflow keeps its lease and an abandoned one expires on its own
- [ ] Update every agent-facing surface: `device lock --help`, `device unlock --help`, `CLAUDE.md`, `docs/CLI.md`

### Task 3: presented-token-but-no-longer-owner must fail loudly, read-only included

- [ ] This is the safety half and matters more than Task 2. When `$HELIXGEN_LOCK_TOKEN` is set but does not authenticate against a live lease for the scope a verb touches, the verb must FAIL FAST with a clear message naming the current holder — rather than proceeding unlocked
- [ ] Apply it to read-only verbs too (`device measure`, `device meters`, `device tuner`, `device blocks`, `device params`, ...). Setting the token is an explicit declaration of "I am in a held session"; if that session is gone, a read is no more trustworthy than a write. Verbs invoked with NO token keep today's behavior exactly (unlocked reads stay free — do not make read-only verbs start requiring locks)
- [ ] Test the exact observed scenario end to end: token set, lease reclaimed by a contender, `device measure` must error instead of returning a measurement
- [ ] Make the error message actionable: name the holder, and say the workflow's lease was reclaimed (not merely "locked")

### Task 4: document the constraint that remains

- [ ] Whatever Task 2 lands, a workflow that never renews can still lose a lease. State the operating rule in `CLAUDE.md` + `docs/CLI.md`: an agent driving multi-call device work should take a detached lease, and treat any lock error as "stop and re-establish state", never "retry the failed call and continue"
- [ ] Note that the companion `device` skill guidance lives in the plugin repo and ships in a separate PR (agent-facing surfaces ship in sync) — flag it in the PR description; do not edit plugin files from here

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`, which pins the agent-facing `--help` contract).
  Live tests under `tests/live/` auto-skip without `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

Opt-in (NOT part of default validation — requires a real Helix Stadium on the
LAN and mutates device state):

- `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and locks" tests/live`
  — the lock suite's live coverage. Note this suite is itself a lock
  contender: do not run it concurrently with other device work (it is what
  collided with the 2026-07-27 measurement session).

## Release

- [ ] Bump the version and tag `vX.Y.Z`. Task 3 changes behavior for any
      caller that sets `$HELIXGEN_LOCK_TOKEN` (previously-silent proceeds now
      error), so call that out in the release notes — minor-level, not patch.
      Releases are preapproved from the coordination workspace; the tag fires
      the PyPI publish workflow. Sequence the companion plugin skill PR after
      the release, as usual.
