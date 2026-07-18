# Plan: core lock-race — serialize acquisition (create→rescan double-hold)

## Context

Advisory-lock acquisition (`src/helixgen/locks.py`) has a create-then-rescan
race in `_post_create_conflict` / `_acquire_one`: two processes taking
CONFLICTING scopes can both commit. Each atomically creates its lease file then
re-scans + applies an `(acquired_at, nonce)` tiebreak — but this is only sound
if BOTH creates land before either rescans. A younger-`acquired_at` racer that
creates AND rescans before the older's file is visible sees no conflict and
commits; the older then creates, rescans, computes `theirs < ours == False`,
and also commits. Both hold conflicting scopes — the "exactly one winner"
invariant is violated. It's cross-PROCESS (separate CLI invocations), so an
in-process mutex does NOT fix it. Currently quarantined:
`tests/test_locks.py::test_all_vs_scope_create_race_yields_exactly_one_winner`
is `xfail(strict=False)` under xdist.

#### The exact double-commit interleaving (Task 1 finding)

Two DISTINCT processes race conflicting scopes — `p-old` taking `all`
(OLDER `acquired_at`) and `p-young` taking `library` (YOUNGER
`acquired_at`). Neither owns the other's lease (distinct pid + token).
`_acquire_one` does, per process: (step 2) pre-create SCAN of the
conflicting files, (step 3) atomic `_write_new` of its own lease, then
`_post_create_conflict` RESCAN of the conflicting files applying the
`(acquired_at, nonce)` tiebreak. The double commit happens when:

1. `p-old` SCANs `all`'s conflict set (every granular scope) → nothing.
2. `p-young` SCANs `library`'s conflict set (`library`, `all`) → nothing.
   (Both scans land before either create — the pre-condition for the bug.)
3. `p-young` `_write_new`s `library`, then RESCANs `all` → still ABSENT
   (p-old has not created yet) → no conflict → COMMITS.
4. `p-old` `_write_new`s `all`, then RESCANs the granular scopes → sees
   `library` (p-young). Tiebreak: `theirs(young) < ours(old)` is FALSE
   (younger has the LARGER timestamp), so `we_lose == False` → p-old does
   NOT back off → ALSO COMMITS.

Both hold conflicting scopes → "exactly one winner" violated. The
`_break_stale` mutex (`locks.py:352`, atomic `_write_new` of an `X.lock.break`
file, re-verify UNDER the mutex) is the filesystem-serialization pattern the
fix (Task 2) mirrors: a stale-breakable meta-lock held across the whole
scan→create→verify critical section forces p-old's SCAN in step (1)/(4) to
observe p-young's already-committed lease, so it blocks instead of
double-committing.

The Task-1 reproduction (`test_all_vs_scope...`) drives this exact order
deterministically with two `threading.Event`s (one signalling that p-old has
finished scanning, one that p-young has committed) plus forced
`acquired_at` values, so the race no longer depends on xdist CPU contention.
The event waits are BOUNDED (2 s) so the serialized (fixed) code — where the
losing process never reaches `_write_new` — falls through gracefully rather
than deadlocking. Marker removal + stress-loop is Task 3.

**This is a concurrency redesign — correctness is paramount.** Mirror the
filesystem-level serialization the repo ALREADY uses in `_break_stale`
(`locks.py:~320-350`): a stale-breakable filesystem meta-lock (atomic
`mkdir`/`O_EXCL`) held across the scan→create→verify critical section so
acquisition is serialized across processes. Repo rules: TDD, stdlib only.

### Task 1: reproduce the race deterministically FIRST

- [x] Study the existing `test_all_vs_scope_create_race_yields_exactly_one_winner`
      (threads + `threading.Barrier`) and `_break_stale`'s existing
      filesystem-serialization pattern. Document (in the plan or a comment) the
      exact interleaving that double-commits. (See "The exact double-commit
      interleaving" note above + the test docstring.)
- [x] Ensure there is a test that RELIABLY provokes the race and asserts exactly
      one winner — deterministically, and passing under `-n auto` once fixed.
      (Reuse/strengthen the existing test; it must stop being xfail.) Rewrote it
      as an event-ordered harness that forces the double-commit interleaving
      independent of the scheduler — verified 15/15 (serial + xdist) via
      `--runxfail`. Kept `xfail(strict=False)` this iteration so the suite stays
      green pre-fix; the marker itself is removed in Task 3.

### Task 2: serialize acquisition with a stale-breakable meta-lock

- [ ] Introduce a filesystem acquisition meta-lock (atomic `mkdir` or `O_EXCL`
      create) that a process must hold while it does scan → create → verify for
      a given device/scope space. Model its stale-breaking on `_break_stale`
      (a process that dies holding the meta-lock must not wedge others: TTL +
      break). Keep the critical section MINIMAL (acquisition is brief).
- [ ] The meta-lock itself must be correct: atomic acquire, no deadlock, a
      bounded wait/retry, and stale-break identical in spirit to leases. Add
      tests for: (a) meta-lock is mutually exclusive; (b) a stale/abandoned
      meta-lock is broken and acquisition proceeds; (c) no deadlock when two
      processes contend.

### Task 3: remove the xfail; prove the invariant holds

- [ ] Remove the `xfail` marker on
      `test_all_vs_scope_create_race_yields_exactly_one_winner`; it must now
      PASS deterministically, including repeatedly under `-n auto` (run it many
      times / with high worker count to stress the timing).
- [ ] All existing lock tests still pass. No regression in the non-contended
      fast path (single acquirer must not pay a meaningful penalty). No new
      deadlock/livelock. Confirm release/renew (the #72 nonce-guard) still
      interoperates with the new acquisition path.

## Validation Commands

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (`-n auto`); the
  #88 test must pass (no longer xfail). Live tests auto-skip.
- `PYTHONPATH=$PWD/src python -m pytest tests/test_locks.py -n auto -p no:randomly` —
  run the lock suite specifically; consider looping it to stress the race.
