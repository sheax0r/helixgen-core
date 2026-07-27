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

- [x] Write the failing test(s) first, following the established patterns in `tests/test_locks.py`
- [x] Test: a lease whose recorded pid is dead is reclaimed by a contender after `SESSION_PID_GRACE_S`, and a subsequent call presenting the original `$HELIXGEN_LOCK_TOKEN` is NOT recognized as the owner (this is the #97 bug, and pinning it prevents a regression later reintroducing silent takeover)
- [x] Determine and pin whether a token-authenticated call currently RENEWS the lease (`_renew()` exists at `src/helixgen/locks.py:345`). If sibling calls already renew, establish why the observed lease still went stale — the workflow made calls well inside the grace window, so either renewal is not wired to token-authenticated use or the pid-death check short-circuits it. Write down the answer in the plan/PR; do not guess

**Task 1 answer (determined, pinned by `test_97_*` in `tests/test_locks.py`):**
renewal IS wired to token-authenticated use. Every lock-taking (mutating)
verb's `acquire()` passes through an owned live covering lease and `_renew()`s
it (step 1 of `_acquire_one`), and this works even while the recorded pid is
already dead, as long as the call lands inside `SESSION_PID_GRACE_S` — each
such call resets the grace clock (`acquired_at`). The observed lease went
stale because the workflow's calls were dominated by READ-ONLY verbs
(`device measure` in a `--source loop`), which take no lock at all and
therefore never renew: the grace clock runs from the last *mutating* call,
not the last call. Once it lapsed, the pid-death check short-circuits
passthrough/renewal for the stale `all` session lease; a later
token-authenticated mutating call silently narrows to a fresh transient
lease for just its own scope (leaving the stale `all` file reclaimable),
and after a contender reclaims, the original token authenticates nothing —
`owned()` is False and the agent's calls are ordinary unauthenticated
newcomers (blocked on held scopes, silently unlocked on free ones).

### Task 2: a lease an agent can actually hold

- [x] Provide a way to take a lease not bound to the invoking shell's lifetime. Preferred shape: `device lock --detach` (records `pid: None`, TTL-only, explicitly released via `device unlock`); an alternative is `--pid <pid>` to bind the lease to a caller-supplied long-lived process. Pick ONE and justify it in the PR
- [x] A detached lease must NOT be able to brick the device indefinitely: default it to a materially shorter TTL than the 7200 s session default, keep `device unlock` working, and keep an explicit force/break path for a genuinely orphaned lease
- [x] Renew a detached lease on each token-authenticated use, so an active workflow keeps its lease and an abandoned one expires on its own
- [x] Update every agent-facing surface: `device lock --help`, `device unlock --help`, `CLAUDE.md`, `docs/CLI.md`

**Task 2 decision (for the PR): `--detach`, not `--pid <pid>`.** An agent has
no long-lived local process to bind a lease to — every tool call is a fresh
shell, and the agent itself is not a pid in this machine's namespace. `--pid`
would just relocate the fragility onto whatever pid the caller guessed
(and invites binding to a pid that gets recycled). `--detach` removes the
binding entirely: `kind: "detached"`, `pid: None`, so `is_stale()`'s pid
branch never applies and only the TTL expires it.

Safety, since the TTL becomes the sole automatic reclaim path:
`DEFAULT_DETACHED_TTL = 300` s vs the session default of **900** (note: the
plan text above said 7200 — that was the value in the *observed* lease, not
the code default, which has been `DEFAULT_SESSION_TTL = 900` throughout);
`--ttl 0` (no expiry) is **refused** with `--detach`, because no pid AND no
expiry leaves a lease only `unlock --force` can clear; `device unlock` (by
token) and `device unlock --force` both still work unchanged.

Renewal needed no new mechanism: `_acquire_one` step 1 already passes
through and `_renew()`s any owned live covering lease, and a detached lease
is never pid-stale, so every token-authenticated mutating call renews it.
Pinned by `test_97_detached_lease_is_renewed_by_token_authenticated_use`.

### Task 3: presented-token-but-no-longer-owner must fail loudly, read-only included

- [x] This is the safety half and matters more than Task 2. When `$HELIXGEN_LOCK_TOKEN` is set but does not authenticate against a live lease for the scope a verb touches, the verb must FAIL FAST with a clear message naming the current holder — rather than proceeding unlocked
- [x] Apply it to read-only verbs too (`device measure`, `device meters`, `device tuner`, `device blocks`, `device params`, ...). Setting the token is an explicit declaration of "I am in a held session"; if that session is gone, a read is no more trustworthy than a write. Verbs invoked with NO token keep today's behavior exactly (unlocked reads stay free — do not make read-only verbs start requiring locks)
- [x] Test the exact observed scenario end to end: token set, lease reclaimed by a contender, `device measure` must error instead of returning a measurement
- [x] Make the error message actionable: name the holder, and say the workflow's lease was reclaimed (not merely "locked")

**Task 3 implementation (for the PR):** `locks.check_session(ip, scopes)` +
`locks.LockLost`. With no token it is a no-op, so unlocked callers — read-only
verbs included — are byte-for-byte unchanged; with a token it requires a LIVE
lease that token opens covering each scope (the scope's own file or `all`),
else raises naming the current holder ("the session lease was reclaimed or
expired, and X holds it now") with the recovery instruction (re-take a
detached lease and re-read state; do not retry-and-continue).

Wired at the CLI policy layer, not inside `acquire()`, so the Task 1
characterization tests still pin the lock layer's own mechanics:
`_locked` calls it before acquiring (a dangling token no longer narrows
silently into a fresh transient lease), and a new sibling decorator
`_reads(<scope>)` — check only, no lease, no new flag — carries it to the
read-only networked verbs: `measure`/`meters`/`tuner`/`blocks`/`params`/
`active`/`watch` (editbuffer), `list`/`setlists`/`read`/`pull`/`backup`/
`setlist export-hss`/`slots list --verify` (library), `list-irs`/`pull-ir`
(irs), `settings list`/`get` (globals). `device lock`/`unlock`/`discover` and
the offline verbs stay exempt — recovery must never be locked out.

### Task 4: document the constraint that remains

- [x] Whatever Task 2 lands, a workflow that never renews can still lose a lease. State the operating rule in `CLAUDE.md` + `docs/CLI.md`: an agent driving multi-call device work should take a detached lease, and treat any lock error as "stop and re-establish state", never "retry the failed call and continue"
- [x] Note that the companion `device` skill guidance lives in the plugin repo and ships in a separate PR (agent-facing surfaces ship in sync) — flag it in the PR description; do not edit plugin files from here

**Task 4 landed:** `docs/CLI.md` "Device locks" gains (a) the dangling-token
fail-loud rule from Task 3 — which was code-only until now — naming every
read-only verb that checks, and (b) a 4-point **operating rule for an agent
driving multi-call device work**: detached lease up front + exported token,
`device unlock` at the end (including on failure), any lock error means STOP
and re-establish state (re-take a lease, re-read active preset/snapshot/block
state) rather than retry-and-continue, and size `--ttl` to cover long
read-only stretches because only covered (mutating) verbs renew. `CLAUDE.md`
carries the same rule in condensed form, pointing at `docs/CLI.md` for the
verb table.

**For the PR description (companion plugin PR):** this PR changes behavior the
plugin repo's `device` skill describes — an agent should now take
`device lock --scope all --detach`, and a dangling `$HELIXGEN_LOCK_TOKEN` now
errors on read-only verbs too. That skill guidance lives in `sheax0r/helixgen`
(`.claude/skills/device/`) and ships as a **separate, sequenced PR** after the
core release (agent-facing surfaces ship in sync; core releases first). No
plugin files are edited from this repo.

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

- [x] Bump the version and tag `vX.Y.Z`. Task 3 changes behavior for any
      caller that sets `$HELIXGEN_LOCK_TOKEN` (previously-silent proceeds now
      error), so call that out in the release notes — minor-level, not patch.
      Releases are preapproved from the coordination workspace; the tag fires
      the PyPI publish workflow. Sequence the companion plugin skill PR after
      the release, as usual.

      **Bumped to 0.33.0** (minor, not patch: Task 3 makes a dangling
      `$HELIXGEN_LOCK_TOKEN` an error on verbs that previously proceeded
      silently, read-only ones included) in `pyproject.toml` +
      `src/helixgen/__init__.py` on this branch — matching the `(0.33.0, #97)`
      references already written into `CLAUDE.md`. `github/main` and the tag
      list were re-fetched first; 0.33.0 was unclaimed.

      Tag `v0.33.0` NOT pushed from here: the publish workflow fires on tags
      pushed to `main`, so the tag goes on the merge commit after this branch
      lands, not on a worktree branch head. Release-note line for the tag:
      *"Device locks: `device lock --detach` gives an agent a lease not bound
      to the invoking shell (TTL-only, default 300 s, `--ttl 0` refused).
      BEHAVIOR CHANGE — a set-but-dangling `$HELIXGEN_LOCK_TOKEN` now fails
      loudly naming the current holder instead of proceeding unlocked, on
      read-only verbs too; callers with no token are unaffected."*
