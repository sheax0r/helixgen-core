# Plan: #79(k) — suite-level real-home-untouched guard

## Context

Backlog **#79 remainder (k)**. The #87 assessment found the offline suite's
per-test tmp isolation (conftest autouse fixtures) already keeps tests off the
real `~/.helixgen` — the historical leak is effectively closed. This adds
defense-in-depth: a suite-level guard that FAILS the run if any test mutates
the real `~/.helixgen`, so a future isolation regression is caught immediately
instead of silently polluting real state. Port the pattern the live suite
already uses (`tests/live/conftest.py` real-home-untouched guard) to the main
`tests/conftest.py`. Repo rules: stdlib + click only.

**Out of scope (do NOT do here):** #79(i), the helixgen auto-commit git-identity
question — that's a design decision held for the user, not this change.

### Task 1: real-home-untouched guard in the offline suite

- [x] Read the live suite's real-home guard (`tests/live/conftest.py`) — how it
      snapshots + asserts the real `~/.helixgen` (or `$HELIXGEN_HOME` unset →
      real home) is unchanged.
- [x] Add an equivalent session-scoped guard to `tests/conftest.py` covering
      the offline suite: snapshot the real home dir (the one that WOULD be used
      with no test overrides) at session start, assert untouched at session end.
      Must be xdist-safe (works with `-n auto`; per-worker snapshot/assert is
      fine — each worker asserts its own view).
- [x] Verify: full suite green (guard passes — nothing leaks). Sanity-check the
      guard actually bites by temporarily making a throwaway test write to real
      home locally and confirming the guard fails (then remove the throwaway —
      do not commit it).

## Validation Commands

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (`-n auto`); the
  new guard must pass. Live tests auto-skip without `HELIXGEN_LIVE=1`.
