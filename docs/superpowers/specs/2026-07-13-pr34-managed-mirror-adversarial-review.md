# PR #34 adversarial review — managed-mirror sync fixes

**Date:** 2026-07-13 · **Verdict:** MERGE (after 3 fix rounds) · **Reviewer:** independent adversarial subagent

PR #34 fixes the 2.19.0 tone-library bugs (slot-only tones never installed —
`plan_mirror` was dead code; `setlist remove` unregistering slot-marked tones;
`--gc` deleting slot-only tones). The review ran three rounds against the live
branch, each finding reproduced with scripts before being reported, each fix
re-verified afterwards. Suite at merge: **1022 passed / 7 skipped**.

## Round 1 — 10 findings on the original fix (all fixed)

1. **MAJOR** Mirror delete lacked prior-placement evidence — could delete a
   same-named pool preset helixgen never placed (every generated tone
   auto-registers `slot: null`). → placement-evidence gate (`rec.device` /
   `observed.pool`).
2. **MAJOR** Pathless `save`/`create` tones with a slot poisoned every sync
   with a permanent "cannot install" error. → excluded from the slot-marked
   union when absent from the pool.
3. **MAJOR** `synced` flag never consulted: `--all` synced drafts; an empty
   local draft matching a device setlist name got its device references wiped,
   then `--gc` deleted the untracked presets. → `--all` filters to synced
   setlists; a targeted sync is the opt-in and flips the flag.
4. CLI never printed `pool.deleted`. → printed, names included.
5. Members of unresolved target setlists landed in the delete bucket. →
   excluded.
6. add-then-remove was no longer a no-op (implicit `auto` mark survived). →
   see round 2 R1 for the final shape.
7. Never-orphan delete skips were silent. → `pool.delete_skipped` + CLI note.
8. BACKLOG `#22` ID collision. → renumbered (now `#26–#29` after further main-side collisions).
9. **PLAUSIBLE/deferred** Slot labels for slot-only tones are fictional (pool
   install, no reference; occupancy never fetched; posi≠slot address space). →
   backlog **#30**; false comments corrected.
10. Test gaps (delete-after-rebuild ordering unpinned; mid-loop error path). →
    pinned.

## Round 2 — 2 findings on the round-1 fixes (fixed)

- **R1 MAJOR** The blanket "clear `auto` on last membership" destroyed explicit
  `device add` marks (default `--slot auto`) and contradicted the MCP docs. →
  `auto_marked` provenance tag: only implicit synced-setlist stamps die with
  the last membership; explicit adds and concrete labels survive; tag clears
  on real placement; MCP descriptions rewritten. Accepted residual:
  pre-provenance manifests' untagged implicit stamps survive as
  explicit-looking.
- **R2** Pre-2.21 v2 manifests silently dropped out of `--all` (nothing ever
  set `synced=True`). → load-time migration from observed-setlist evidence +
  `skipped_draft_setlists` reporting.

## Round 3 — 1 finding on the round-2 fixes (fixed)

- **MAJOR** The R2 migration re-ran on every load and re-flipped any
  ever-synced setlist to `synced=True`, permanently undoing `setlist
  sync-off`. → `set_setlist_synced(False)` drops the setlist's
  `observed.setlists` entry (the migration evidence); opt-out verified durable
  across load/sync-all/re-opt-in lifecycle. Plus: `unsync()` pops a stale
  `auto_marked`.

## Lessons

- The 2.19.0 bugs and every round-2/3 finding share one shape: **a mutation
  path not cross-checked against the full lifecycle** (load → mutate → save →
  load → sync). Reviews of manifest/sync changes should always walk that loop.
- Migrations keyed on evidence that normal operation keeps re-creating are not
  one-time; make the evidence removable (or version the migration).
- `plan_mirror` sat dead for a release because nothing integration-tested the
  CLI verb → engine path end-to-end. The fake-client tests now cover the
  engine; a CLI-level wiring test remains worth adding (backlog #28 review
  pass).
