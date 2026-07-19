# Plan: harden the #88 acquisition meta-lock stale-reclaim (TOCTOU)

## Context

Follow-up on the same branch. Deep review found the `_acquire_meta`
stale-reclaim in `src/helixgen/locks.py` has a check-then-unlink TOCTOU that
`_break_stale` (`locks.py:~357-388`) was specifically engineered to avoid:

```
if time.time() - path.stat().st_mtime > _ACQUIRE_META_TTL_S:
    path.unlink(missing_ok=True)   # not atomic with the stat; no re-verify
```

**Failure W2:** stale meta C exists. Acquirer B stats C (old mtime → stale),
about to unlink. Acquirer A runs the same branch, unlinks C, recreates a FRESH
meta, enters the critical section. B's pending `unlink` then deletes A's FRESH
meta; B recreates and ALSO enters the critical section → two processes in
scan→create→verify → the #88 double-commit race reopens (the nonce-guard on
release does NOT prevent the concurrent critical sections).

**Failure W1:** a live-but-overrunning holder's meta (critical section stalled
>10s) is broken purely on mtime — no pid-liveness check — so an alive holder
loses its meta.

The core meta-lock design is sound and the common-case race is proven fixed;
this hardens ONLY the crash-recovery edge. Do NOT redesign the meta-lock or
touch the (correct) acquire/release/nonce-guard paths beyond the reclaim.
Repo rules: TDD, stdlib only.

### Task 1: reproduce W1/W2 deterministically

- [x] Add a test that provokes the two-reclaimers-vs-fresh-recreate interleaving
      (W2): assert that a fresh meta created by one acquirer is NEVER unlinked
      by another acquirer's stale-reclaim, i.e. the meta's peak concurrency in
      the critical section stays 1 even when a stale meta pre-exists and two
      acquirers race to reclaim it. Deterministic (event/barrier-gated, like the
      existing meta tests).
      (`test_w2_racing_reclaimers_never_delete_a_fresh_meta`, xfail; --runxfail
      shows `max: 2` — two acquirers in the CS at once.)
- [x] Add a test for W1: a live holder whose meta is younger-than-TTL OR whose
      pid is alive is NOT broken by a contender.
      (`test_w1_live_or_young_holder_meta_is_not_broken`, xfail; --runxfail
      shows "a live holder's meta was broken on mtime alone".)

### Task 2: serialize + re-verify the reclaim (mirror _break_stale)

- [ ] Make the stale-reclaim in `_acquire_meta` race-free the way `_break_stale`
      is: re-read/re-verify the meta is STILL the same stale file (same
      mtime/nonce) immediately before unlink, under the same break-mutex
      serialization `_break_stale` uses — never stat-then-unlink across a gap.
      Only unlink the exact stale file observed; never a fresh recreate.
- [ ] Add a pid-liveness check so a meta whose owner process is still alive is
      NOT broken on mtime alone (only break when TTL-expired AND owner dead /
      unknown), consistent with how scope leases are judged stale.
- [ ] Keep the fast path cheap; do not add fsync.

### Task 3: prove it

- [ ] W1/W2 tests pass; the existing #88 race test still passes
      deterministically under `-n auto` (stress it).
- [ ] Full lock suite + full offline suite green. No deadlock/livelock; the
      non-contended acquire cost is unchanged.

## Validation Commands

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (`-n auto`).
- `PYTHONPATH=$PWD/src python -m pytest tests/test_locks.py -n auto` — lock suite.
