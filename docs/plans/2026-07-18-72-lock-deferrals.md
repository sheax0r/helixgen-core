# Plan: #72 — lock-system deferrals (non-Windows)

## Context

Backlog **#72** — deferrals from the #71 advisory-lock review (core ≥0.22.0,
`src/helixgen/locks.py`). Ship the local, testable ones here; **Windows
validation is GATED** (no Windows runner — leave it to its backlog entry, do
NOT attempt). Also: do NOT attempt the #88 create→rescan double-hold race
(separate entry, needs a mutex-serialize acquisition redesign + its own spec) —
if a change here would touch that path, stop and note it. Repo rules: TDD,
stdlib + click only.

### Task 1: ip-sanitization collision

- [x] Read how a device IP is sanitized into a lock key/filename in `locks.py`.
      Identify the collision (distinct IPs sanitizing to the same key, or a key
      that can't round-trip).
- [x] Write a failing test demonstrating two distinct IPs (or an IP vs a
      lookalike) colliding on the same lock identity.
- [x] Fix the sanitization so distinct IPs get distinct lock identities; make
      the test pass.

### Task 2: release/renew micro-windows

- [x] Examine the release and renew paths for TOCTOU micro-windows (e.g.
      renew after expiry, release of a lease already broken/re-acquired by
      another owner). Identify the concrete window(s).
- [x] Write failing test(s) for the misbehavior (renew must fail/no-op if the
      lease is no longer ours; release must not delete a lease now owned by
      someone else).
- [x] Fix minimally (compare-and-act on owner token/nonce); make tests pass.
      Do NOT redesign acquisition (that's #88).

### Task 3: document the mixed-version caveat

- [x] Document that pre-0.22.0 clients ignore advisory locks (so running them
      in parallel against the device is unsafe) — in the lock section of
      `CLAUDE.md` and/or `docs/CLI.md`, wherever locks are described.

## Validation Commands

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (runs under
  `-n auto`). Live tests auto-skip without `HELIXGEN_LIVE=1`.

Note: the #88 lock-race test is xfail(strict=False) under `-n auto`; that is
expected and unrelated to this work — do not "fix" it here.
