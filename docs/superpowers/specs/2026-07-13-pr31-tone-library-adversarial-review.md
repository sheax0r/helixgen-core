# Adversarial review — PR #31 "tone-library model redesign" (shipped 2.19.0, un-reviewed)

Post-merge adversarial review requested 2026-07-13 (PR #31 merged to `main` as
2.19.0 without a review). Scope: `e64807f..82dea85`
(`src/helixgen/device/manifest.py`, `setlist_sync.py`, `cli.py` device group,
`mcp_server/tools.py`, migration + new tests).

**These are NOT in the Global Settings feature** (this branch's work) — they are
pre-existing in shipped 2.19.0. Recorded here for the library owner to triage.
Findings verified against the code by the reviewer; line numbers as of 2.19.0.

## Critical / High

1. **The "managed-set mirror" engine was never wired in — `plan_mirror` is dead
   code, and 3 new CLI verbs make false promises.** `plan_mirror`
   (`setlist_sync.py:125`) has no production callers. `sync_setlists` is still the
   old pool+references engine: it never reads `tone.slot`, never deletes on
   `slot=None`, never consults the `synced` flag.
   - `device add <tone> --slot ...` says "placed on next sync" — but sync installs
     only `union_tones(resolved_setlists)`; a tone marked for device but in **no
     setlist is never installed**, and `--slot` drives nothing.
   - `device unsync <tone>` says "deleted from device on next sync" — nothing
     deletes on `slot=None`; the pool preset survives (except `--all --gc`).
     User is told a preset is gone when it isn't.
   - `setlist sync-off` ("local-only draft, not mirrored") — `sync_setlists`
     ignores `synced`; `device sync --all` still mirrors the draft and
     `mirror_setlist` **removes device references** it lacks → a draft edit +
     `sync --all` destructively rewrites a device setlist marked local-only.

2. **"Never touch untracked" is false on name collision.** `plan_pool`
   (`setlist_sync.py:54-65`) matches manifest tones to device pool **by name
   only**. An untracked on-device preset sharing a manifest tone's name →
   observed_hash None ≠ content_hash → "update" → `set_content_data(cid, blob)`
   **overwrites the untracked preset**. Auto-register-on-every-generate inflates
   the managed namespace, raising collision odds. Latent worse case: once
   `plan_mirror` is wired, its `managed_names` param is discarded (`_ = managed`,
   line 151) so a `slot=None` tone matching a same-named untracked preset lands in
   the **delete** bucket with the untracked cid.

3. **v1→v2 migration silently drops ledger-only placements (local data loss).**
   **→ DISMISSED by the owner (2026-07-13): historical/ledger data is disposable,
   the ledger is being retired, and nobody else uses helixgen yet. No migration
   is needed — do NOT spend effort here.**
   2.16–2.18 `device install/push/save/create` recorded placements **only** in the
   ledger `entries`. `_migrate_v1` (`manifest.py:186-202`) builds tone records
   only by iterating `data["tones"]`, so every entries-only placement loses its
   name/slot/cid/`source_path` (needed by `device slots restore`) on first
   post-upgrade save. Contrast `_migrate_from_ledger` (line 212), which does it
   right. Untested (migration test only covers a tone present in both sections).
   Hits anyone who used `device install` without `setlist add` — the common case.

## Medium

4. **`assign_slots` called with `occupied=set()`** (`setlist_sync.py:262`) →
   auto slots dealt from "1A" up with zero device knowledge; the label is fiction
   yet persisted and later drives `device slots restore` (`--force` can clobber
   the real preset at that slot). Resolves autos across **all** manifest tones,
   not just sync targets. >32 slotted tones raises `ValueError` before the client
   opens; CLI catches only `HelixError` → raw traceback, no sync.

5. **Corrupt/unknown-version manifest → silently empty → clobbered.** `load`
   (`manifest.py:137-155`): any JSON/OS error or `version` not exactly 1/2 falls
   through to an empty manifest. Since every `generate` now load-saves the manifest
   (`_auto_register_tone`), the next generate rewrites `setlists.json` with only
   the new tone — entire library/slot/setlist state destroyed, no backup of the
   unreadable file, at most a stderr warning.

6. **Last-writer-wins concurrency, window widened to minutes.** `sync_setlists`
   loads at CLI start, saves at end of a multi-minute device session; any
   `generate` auto-register or setlist edit in that window is reverted by the
   sync's save. Atomic per-writer, but no lock / reload-before-save.

## Low

7. **MCP `device_install_preset` manifest recording broken when `name` ≠ the
   `.hsp`'s `meta.name`** (`tools.py:721-732`): `register_tone` registers under
   `meta.name`, then `m.tones[name]` KeyErrors, swallowed by bare `except` →
   nothing recorded (and `save()` never runs). CLI twin gets it right.

8. **Mutator nits:** `mark_on_device` doesn't enforce concrete-slot uniqueness
   (two tones can claim "1A"); `remove_tone` GCs a device-resident tone →
   untracked; `setlist sync-on <typo>` silently creates an empty setlist via
   `setdefault`; `_ledger_rename` onto an existing name overwrites + leaves a
   stale `observed.pool` entry.

## Solid (checked, no issue)

Atomic manifest writes (tmp + `os.replace`); auto-register is advisory (never
fails `generate`); migration idempotent in-memory; `register_tone` name-collision
guard + content-hash refresh; `plan_gc`'s in-loop never-orphan re-verify;
per-tone error isolation; partial-sync re-runs converge.

## Recommendation

Most urgent: (1) wire or gate `device add`/`unsync`/`sync-off` so they don't
misrepresent sync; (2) match pool by name **and** managed-ownership before any
overwrite/delete; (5) refuse to save over a manifest that failed to load (guards
*future* v2 corruption — distinct from migration). Finding (3) is **dismissed by
the owner** (no historical migration wanted). Route the rest to the library
owner.
