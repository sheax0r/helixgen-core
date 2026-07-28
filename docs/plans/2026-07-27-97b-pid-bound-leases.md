# Plan: #97 (revised) — PID-bound device leases

## Context

Supersedes `2026-07-27-97-agent-durable-leases.md`. That plan left two design
decisions to the implementer ("pick ONE and justify it"), and the review loop
spent 11 fix commits across 3 runs relitigating them without ever converging.
**Every design decision below is SETTLED. Implement it as written. If you
believe one is wrong, stop and say so in the progress log — do not redesign.**

Work continues **on the existing `agent-durable-leases` branch**, which already
carries a correct implementation of the read-only refusal and the detached
lease. This plan adds the piece its review never got to weigh, and retires the
mechanism that caused the original bug.

### The bug (workspace `BACKLOG.md` #97, observed twice on 2026-07-27)

`device lock` records the INVOKING SHELL's pid. An agent takes the lock in one
tool call; that shell exits when the call returns; `is_stale()` sees a dead pid
and, after `SESSION_PID_GRACE_S`, a contender reclaims the lease mid-workflow.
Then the asymmetry bites: the mutating verb (`device snapshot`) refused on the
lock while the read-only verb (`device measure`) proceeded and returned a
well-formed measurement **of an unknown snapshot**. Silent wrong data.

### The settled design

**1. `--pid <pid>` is the primary mechanism.** The caller supplies a pid that
spans the whole workflow and dies with it. For a Claude Code agent that is the
`claude` process — verified 2026-07-27: a tool-call shell's `$PPID` is the
`claude` process, alive for the entire session while each tool-call shell lives
seconds. So an agent takes `device lock --scope all --pid $PPID`.

Liveness of a `--pid` lease is DECIDABLE, so it needs no TTL guessing. This
replaces the question the old plan could not answer ("how long a gap before we
assume abandonment?") with one that has a real answer ("is the owner alive?").

**2. Lease identity is the `(pid, start_time)` PAIR, never the pid alone.**
PID numbers are recycled. A recycled pid would make an abandoned lease look
alive forever — strictly worse than today's bug. Record the owner's process
start time at acquisition (`ps -p <pid> -o lstart=`, stdlib `subprocess`, the
repo is stdlib-only) and verify it on every liveness check. A pid whose current
start time differs from the recorded one is DEAD, not alive.

**3. Three lease kinds, distinguished by `kind`:**

| `kind` | `pid` | Liveness | Grace | Use |
|---|---|---|---|---|
| `pid` | explicit, + `pid_start` | pid alive AND start time matches | **none** | agent workflows (the #97 case) |
| `session` | invoking shell (legacy) | pid alive | `SESSION_PID_GRACE_S`, unchanged | existing callers, unchanged behavior |
| `detached` | `None` | TTL only | n/a | no owning process: cron, CI |

**4. `SESSION_PID_GRACE_S` does NOT apply to `kind: "pid"` leases.** The grace
exists because a shell-pid lease taken via a short-lived wrapper would
otherwise be reclaimable instantly. An explicit `--pid` has no such problem:
pid death is conclusive, so a dead owner should be reclaimable immediately.
Leave the grace exactly as-is for `kind: "session"`.

**5. TTL demotes to a backstop for `kind: "pid"`** — it exists only for the
case liveness cannot be checked: a lease from a DIFFERENT host, where
`_pid_alive()` is meaningless (already guarded by
`lease.get("hostname") == hostname()`). Use `DEFAULT_SESSION_TTL` for
`--pid` leases; do not invent a new shorter default for them. `--detach` keeps
`DEFAULT_DETACHED_TTL = 300` as already implemented — automation has no human
gaps to survive.

**6. The read-only refusal already on the branch is CORRECT — keep it.** A
verb invoked with `$HELIXGEN_LOCK_TOKEN` set, where that token no longer opens
a live lease covering what the verb reads, must FAIL rather than proceed. No
token → no check, unlocked reads unchanged. Do not revisit this.

### Task 1: `(pid, start_time)` identity

- [x] Write the failing test(s) first, following `tests/test_locks.py` patterns
- [x] Record the owner's process start time at acquisition; add it to the lease JSON (document the field at the module head alongside the existing lease-shape docs)
- [x] Verify pid AND start time in the liveness path; a start-time mismatch reads as dead. Test the recycled-pid case explicitly — it is the whole reason this field exists
- [x] Reading the start time must not raise on a dead/foreign pid: absent or unreadable start time is a MISS (treat as dead), never an exception

### Task 2: `--pid <pid>` on `device lock`

- [ ] Write the failing test(s) first
- [ ] Add `--pid <pid>`, recording `kind: "pid"` with the pid and its start time. Mutually exclusive with `--detach`; reject both together with a clear error
- [ ] Reject a `--pid` whose process is not alive at acquisition time — taking a lease for a dead owner is always a caller bug
- [ ] Exempt `kind: "pid"` from `SESSION_PID_GRACE_S` (settled decision 4). Test that a `kind: "pid"` lease whose owner just died is immediately reclaimable, and that a `kind: "session"` lease is NOT (grace preserved)
- [ ] `--pid` leases use `DEFAULT_SESSION_TTL` (settled decision 5)
- [ ] Update every agent-facing surface: `device lock --help`, `device unlock --help`, `CLAUDE.md`, `docs/CLI.md`. Show the agent invocation explicitly: `helixgen device lock --scope all --pid $PPID --label "<who>"`

### Task 3: reconcile with what the branch already has

- [ ] The branch's `--detach`, `_reads()` read-only refusal, renewal-on-use, and `keep_alive` heartbeat all STAY. This task is reconciliation, not redesign
- [ ] `describe()` / `--status` / JSON output must render all three kinds unambiguously (`kind: "pid"` shows pid + liveness; `detached` shows no pid)
- [ ] Check the error text a contender sees names which kind it is up against — "held by 'agent' (pid 50754, alive)" is actionable; "locked" is not
- [ ] Confirm no unrelated files are touched. A prior run's review edited `docs/plans/2026-07-27-hw-validation-live-suite.md`, which had nothing to do with #97 — do not repeat that

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
  — note this suite is itself a lock contender; do not run it concurrently
  with other device work.

## Release

- [ ] Bump the version and tag `vX.Y.Z`. Behavior changes for any caller that
      sets `$HELIXGEN_LOCK_TOKEN` (previously-silent proceeds now error), so
      call that out in the release notes — minor-level, not patch. The
      companion plugin skill PR (workspace-side backlog #102) sequences after
      the release.
