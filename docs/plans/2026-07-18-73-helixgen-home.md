# Plan: #73 — route preferences + IR-hash cache through helixgen_home()

## Context

Backlog **#73**. Core docs imply `HELIXGEN_HOME` centralizes all local state,
but `src/helixgen/preferences.py` and `src/helixgen/irhash_cache.py` hardcode
`~/.helixgen` instead of going through `helixgen_home()`. Either route them
through `helixgen_home()` (respecting each module's existing env override, if
any) so `HELIXGEN_HOME` actually relocates them, or fix the docs' overclaim.
Prefer the code route (consistency) unless a module already has a deliberate
separate override. Repo rules: TDD, stdlib + click only. This is
local-state-consistency groundwork ahead of any manifest-v3 work — verified no
in-flight library-foundations work collides (2026-07-18).

### Task 1: preferences.py honors helixgen_home()

- [x] Read `helixgen_home()` (its module + env-override semantics) and
      `preferences.py`'s current path resolution.
- [x] Write a failing test: with `HELIXGEN_HOME` set to a tmp dir,
      preferences.json resolves under that dir (not real `~/.helixgen`).
      Preserve any preexisting `HELIXGEN_*` override specific to preferences if
      one exists (env override still wins). (Covered by
      `test_default_prefs_path_honors_helixgen_home` +
      `test_default_prefs_path_honors_env_var` in `tests/test_preferences.py`;
      `HELIXGEN_PREFS` still wins over `HELIXGEN_HOME`.)
- [x] Route the path through `helixgen_home()`; make the test pass.
      (`default_prefs_path()` anchors under `home.helixgen_home()` after the
      `HELIXGEN_PREFS` check — 81 preferences tests green.)

### Task 2: irhash_cache.py honors helixgen_home()

- [ ] Same pattern for `irhash_cache.py`: failing test that `HELIXGEN_HOME`
      relocates the IR-hash cache file, respecting its existing
      `HELIXGEN_IRHASH_CACHE` override (override wins over HOME).
- [ ] Route through `helixgen_home()`; make the test pass.

### Task 3: reconcile the docs

- [ ] Update any doc (CLAUDE.md, docs/CLI.md, or wherever HELIXGEN_HOME is
      described) so the described behavior matches: HOME now covers
      preferences + IR-hash cache, with per-file env overrides taking
      precedence. No overclaim, no underclaim.

## Validation Commands

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (runs under
  `-n auto`). Live tests auto-skip without `HELIXGEN_LIVE=1`.
