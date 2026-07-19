# Plan: Plain `sync` recomputes `.hsp` hash — stop skipping edited tones

## Context

Implements `docs/BACKLOG.md #92` (HIGH — silent data-integrity). An authored
`.hsp` change can silently fail to reach the device under plain `device sync`.

Root cause: `manifest.content_hash(name)` is a **register-time cache** — written
only by `generate` / `library import` / `register` (`manifest.py` `_hash_file`,
sole writer ~`manifest.py:418`; getter is stored-field-only ~`manifest.py:591`).
Every in-place `.hsp` mutation path rewrites the file bytes but **never
refreshes that cache**: `device normalize` (`cli_device.py` ~3745 preset /
~3845 setlist) and all surgical edits through `_run_mutation` (`cli.py`
~505-524: `set-param`, `patch`, `enable`, `disable`, `add-block`, `remove-block`,
`swap-model`, `set-ir`). Plain sync's change detection in `plan_pool`
(`setlist_sync.py` ~144) compares the **stored** `manifest.content_hash(name)`
against `observed_hash_of(name)` = `obs.pool_hash(name)` (the `synced_hash`
recorded from that same stored hash at the previous sync, `setlist_sync.py`
~432/532). Both sides derive from the same stale cache, so they always agree →
tone routed to `skip` → `updated: []`. `--repush` masked it only because
`force=True` bypasses the comparison entirely.

The `.hsp` hash is over full file bytes (`_hash_file` = `sha256(path.read_bytes())`,
`manifest.py` ~102) and DOES change on an output-block (b13) trim edit — the bug
is purely that sync trusts the cached value instead of recomputing.

**Fix (narrowest, defends the invariant structurally):** recompute the file
hash at sync time for pathful pool tones in `plan_pool`, instead of trusting the
stored `content_hash`. One site closes the whole mutator class regardless of
which verb touched the file (vs. the fragile alternative of re-hashing at every
current+future mutation site — exactly the invariant that failed here). Cost:
one `sha256` per small pool `.hsp` per sync — negligible.

Repo rules: TDD (failing test first), stdlib + click only, agent-facing surfaces
(verb `--help`, `CLAUDE.md`, `docs/CLI.md`) updated in the same change. The
`--repush` help/docs currently mischaracterize the cause ("hash-based change
detection can't see transcoder-output change") — correct it here (see Task 3).

**Cross-repo note (not in this run):** the plugin repo's device `SKILL.md`
`--repush` rationale carries the same mischaracterization; file/ship its
correction as the #92 plugin companion after this core PR lands (backlog #92).

### Task 1: Failing test — plain `sync` detects an in-place `.hsp` edit

- [x] Add a failing test in the matching sync test module (find it: `grep -rl "plan_pool\|def test.*sync" tests/`; follow that file's fixture pattern for a scratch `$HELIXGEN_HOME` + manifest). Scenario: register/generate a pool tone into a setlist; run one `plan_pool` (or the sync-planning entry point) and record the post-sync observed `synced_hash` (simulate a completed prior sync); mutate the tone's `.hsp` in place via a real edit path (`set-param` on the `output` pseudo-block level, or a direct `write_hsp` of changed bytes) so the on-disk hash genuinely changes; run planning again and assert the tone is classified **changed/needs-push**, NOT skipped. — `test_sync_detects_inplace_hsp_edit` (real .hsp on disk, in-place byte rewrite without refreshing `content_hash`; asserts `updated`, `set_content_data`, fresh hash recorded).
- [x] Confirm it FAILS against current code (tone is skipped — reproduces #92). — confirmed: `assert [] == ['Tone A']` (tone routed to skip).
- [x] Add a second case pinning the intended behavior boundary: a tone whose `.hsp` is byte-identical since last sync is still correctly **skipped** (no false-positive re-push churn). — `test_sync_skips_byte_identical_hsp` (passes now and after the fix).

### Task 2: Recompute the file hash at sync time in `plan_pool`

- [x] In `plan_pool` (`setlist_sync.py` ~144), for a pool tone that has an on-disk path, compare a **freshly computed** `_hash_file(<tone path>)` against the observed hash instead of the stored `manifest.content_hash(name)`. Reuse the existing `_hash_file` helper (`manifest.py`) — do not reimplement hashing. Resolve the tone path via the existing accessor (`m.tones[name]["path"]` / whatever `plan_pool` already has in scope). — new `_effective_content_hash(manifest, name)` helper; `plan_pool` compares it against `observed_hash_of`.
- [x] Keep the stored `content_hash` as the **fallback** only for pathless tones (no `.hsp` on disk) so their behavior is unchanged. — falls back when path is null, missing on disk, or unreadable (OSError).
- [x] Ensure the value recorded as the new `synced_hash` after a push is consistent with what the next sync will recompute (so a synced tone reads clean next run — no perpetual re-push). If `record_pool`/`synced_hash` currently stores the stale manifest hash, record the freshly-computed file hash instead for pathful tones. — the `record_pool` call now records `_effective_content_hash`, same value the next sync recomputes.
- [x] Make the failing tests from Task 1 pass; run the full offline suite green. — `test_setlist_sync.py` 45/45; full offline suite 2331 passed, 180 skipped.

### Task 3: Correct the `--repush` agent-facing surfaces

- [x] Update the `sync --repush` help text: after this fix, plain `sync` catches genuine `.hsp` edits. `--repush` remains only for the case where the `.hsp` bytes are **genuinely unchanged** but the transcoder *output* differs (e.g. after a transcoder upgrade). Remove any framing that implies `--repush` is the way to push an edited tone. — `--repush` option help + `device_sync` docstring now state plain sync recomputes the file hash at sync time and re-pushes genuine edits; `--repush` scoped to the unchanged-bytes/transcoder case.
- [x] Reconcile `docs/CLI.md` (the `sync` / `--repush` entry under "Device commands" / "Setlists + sync") to the corrected rationale. — pool-reconcile sentence notes hash recomputed at sync time (#92); `--repush` sentence scoped to unchanged bytes.
- [x] Reconcile the `CLAUDE.md` sync bullet ("`--repush` forces content re-push … hash-based change detection can't see transcoder-output change") to match — keep it accurate about *what* plain sync now detects. — bullet now states plain sync recomputes the `.hsp` hash at sync time (#92); `--repush` scoped to unchanged bytes.
- [x] Verify `tests/test_cli_parity.py` (the `--help` contract) passes with the updated help text. — 104 passed; full offline suite 2331 passed, 180 skipped.

## Validation Commands

Run from the repo root:

- `PYTHONPATH=$PWD/src python -m pytest` — full offline suite (includes the
  golden-output contract, the 211-export round-trip acceptance test, and
  `tests/test_cli_parity.py`, which pins the agent-facing `--help` contract).
  Live tests under `tests/live/` auto-skip without `HELIXGEN_LIVE=1`.

There is no separate lint/format/type-check step configured in this repo.

Opt-in (NOT part of default validation — requires a real Helix Stadium on the
LAN and mutates device state; preapproved for test runs, keep to expendable
slots):

- `HELIXGEN_LIVE=1 PYTHONPATH=$PWD/src python -m pytest -m "live and sync" tests/live`
  — the `sync` blast radius for this change.
