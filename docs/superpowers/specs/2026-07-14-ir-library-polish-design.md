# IR + library polish — design (parity #20 capture-free subset, #11, ±#8)

2026-07-14. Implements the capture-free subset of backlog **#20** ("IR +
library polish", matrix §1/§2/§7): IR delete/rename/prune (**#11**), setlist
rename/delete/duplicate (+ a live attempt at device-side setlist **creation**,
**#8**), and preset color/notes. `.hss` bundle import/export is **out** (no
sample file exists anywhere on this machine — reversing the format without one
would be guesswork; left as a backlog entry). Active-preset select (**#1**) is
explicitly excluded.

## Protocol findings (probed live against the Stadium XL, 2026-07-14)

All discovery below was read-only (`/GetContainerContents`, `/GetContentRef`,
`/GetContentData` — non-activating, mental-model #4):

1. **`/GetContentInfo` does not exist on the device** (`Msg dispatch failed:
   /GetContentInfo is NOT known!!!`), despite appearing in the app binary's
   string table (it is an app-internal/cloud surface). The parity matrix's
   `/SetContentInfo` rows really mean **`/SetContentAttrs`** (the proven
   rename/color command) — matrix updated.
2. **Preset notes are NOT an attr — they are a content property.** A preset's
   `pm__` section (stored content) is a list of `{key_, type, val_}` property
   entries; the Preset Info notes text is `key_ == "preset.meta.info"`
   (`type: "s"`). Confirmed on factory preset `Double Double` (cid 152), whose
   `.hsp` export carries the same text as `meta.info`. So notes read/write =
   `get_content(cid)` → edit the `pm__` entry → `/SetContentData(cid)` — fully
   **non-activating**, all proven commands.
3. **Preset color is the `colr` attr** (`/SetContentAttrs {colr: …}`, already
   documented in `helix-protocol.md` §7.1). Container listings don't include
   `colr` (every probed preset is the default `auto`); value encoding is
   validated live during HW validation (string token vs int). The color
   vocabulary (from the app's strings table, snapshot-color menu — the same
   palette the librarian uses): `auto, white, red, oranged (Dark Orange),
   orangel (Light Orange), yellow, green, bluel (Turquoise), blue, violet,
   pink, off`.
4. **Create-setlist lead (#8):** every container item carries a `type` field
   that equals the known `/CreateContent` `ctype` for presets (`type == 2`),
   and setlist items carry **`type == 1003`**. Hypothesis: `/CreateContent
   (reqid, -5, pos, ctype=1003, {name})` creates a setlist. Tested live with
   `ZZC-`-prefixed junk setlists (created → verified via `device setlists` →
   deleted). Outcome recorded below.
5. IR items in `-11` carry `{cid_, name, hash, mono, posi, type: 8}`; `name`
   is the import basename without `.wav`.

### Live-experiment outcomes (Stadium XL, 2026-07-14, ZZC- artifacts only)

- **#8 `/CreateContent(-5, pos, 1003, {name})`:** ✅ **WORKS.** `/status
  [reqid, newCid, 0]` (`ZZC-test-sl` → cid 1186); the new setlist appears
  under `-5` with `cctp 1001` and accepts references
  (`/AddContentsToContainer` → a normal `cctp 1003` ref with `rcid`) exactly
  like an app-created setlist. **Backlog #8 closes**; `device setlist create`
  ships and `duplicate` auto-creates its target.
- **Setlist rename:** `/SetContentAttrs {name}` on the setlist cid ✅.
- **Setlist duplicate:** copying the source's references (by `rcid`, `posi`
  order) into a freshly created setlist ✅ (both setlists list the same
  `rcid`).
- **Setlist delete:** `/RemoveContent(-5, [setlist_cid])` deletes the setlist
  container **with live references in it**; the referenced pool preset is
  untouched afterwards (verified by re-listing the pool) — no `-21` orphan
  error, references die with their container.
- **`colr` encoding: an int enum.** Writing `{colr: 2}` shows `colr: 2` in
  the container listing and `/GetContentRef`; a **string** token (`"red"`)
  returns `/status 0` but is silently **coerced to 0** — so helixgen sends
  ints only. Once non-default, `colr` appears in every listing (that is the
  read path). Index→name mapping is **inferred** from the app color menu
  order (`auto=0, white=1, red=2, oranged=3, orangel=4, yellow=5, green=6,
  bluel=7, blue=8, violet=9, pink=10, off=11`) — the names ship as a
  convenience over the raw index, documented as inferred (visual confirmation
  on the hardware pending).
- **Notes (`preset.meta.info`):** `get_content(cid)` → edit the `pm__` entry
  → `/SetContentData(cid)` round-trips ✅ (wrote and read back a probe string
  on a ZZC pool preset carrying factory content).

## Surface

### New module `src/helixgen/device/maintenance.py`

Pure planning logic split from device I/O (the `setlist_sync.py` pattern —
unit-testable with a FakeClient / plain data):

- `content_ir_hashes(doc) -> set[str]` — recursively collect `mdls[*].irmd`
  16-byte values from a decoded content doc → 32-hex strings.
- `device_referenced_ir_hashes(client) -> dict[hash, [preset names]]` — scan
  every **pool** preset via `get_content` (non-activating). Setlists are
  references into the pool, and factory presets can't reference user IRs, so
  the pool scan is the complete device-side reference set.
- `local_referenced_ir_hashes(manifest) -> dict[hash, [tone names]]` — scan
  the tone-library manifest's `.hsp` files (`bridge.hsp_ir_hashes`).
- `resolve_device_ir(irs, name_or_hash)` — 32-hex exact hash match, else
  case-insensitive name match (tolerates a trailing `.wav`); `ValueError`
  listing near-misses when absent/ambiguous.
- `plan_ir_prune(device_irs, device_ref, local_ref) ->
  {referenced, protected, orphans}` — *orphan* = on-device IR whose hash no
  device preset references AND no local manifest `.hsp` references; *protected*
  = device-unreferenced but referenced by a local off-device `.hsp` (deleted
  only with `--force`).
- `ir_prune(ip, port=None, execute=False, force=False, only=None,
  manifest=None) -> {ok, dry_run, device_irs, referenced, orphans, protected,
  deleted, errors}` — **dry-run by default**; `only` restricts deletion to one
  IR (name-or-hash) so a live validation can act on a single junk IR.

Plus thin drivers for notes (`set_preset_notes(client, cid, text)` /
`get_preset_notes`) implementing the `pm__` content-property edit.

### `HelixClient` additions

- `delete_irs(cids)` — `/RemoveContent` on `-11` inside `mutating()`.
- `create_setlist(name, pos=None) -> Optional[int]` — `/CreateContent` under
  `-5` with `ctype=1003`; recovers the real cid by re-listing `-5` by name.
- `delete_setlist(cid)` — `/RemoveContent(-5, [cid])` inside `mutating()`
  (references die with the container; pool presets never touched).
- `duplicate_setlist_refs(src_cid, dst_cid) -> int` — copy the source's
  references (by `rcid`, in `posi` order) into the destination via
  `reference_into_setlist`; destination must have no references (clean
  semantics; a partial merge is a different operation).
- (rename for IRs/setlists reuses the existing generic `rename(cid, name)`.)
- `set_color(cid, color_index)` — `set_attrs(cid, {"colr": idx})`.

### CLI (all under `helixgen device`)

- `delete-ir <name-or-hash> [--yes]` — confirm unless `--yes`.
- `rename-ir <name-or-hash> <new-name>`.
- `ir-prune [--yes] [--force] [--only <name-or-hash>] [--json]` — dry-run by
  default; `--yes` executes; `--force` also deletes *protected* IRs; `--only`
  narrows to one IR.
- `set-info <cid>... [--color <name|index>] [--notes <text>]` — one verb for
  both (design call); multiple cids = the matrix's "batch color" row. Color
  accepts a palette name or the raw index.
- `setlist create <name>` — device-side creation (#8).
- `setlist rename <old> <new>` — device-side; also renames the local manifest
  setlist record when one exists (keeps sync targeting working).
- `setlist delete <name> [--yes]` — device-side; never touches pool presets;
  marks a matching manifest setlist unsynced (it becomes a local draft).
- `setlist duplicate <src> <dst>` — copies the reference list; creates `<dst>`
  on the device when absent (via `create`).

### MCP mirrors (path-based, no base64; descriptions carry the contracts)

`device_delete_ir`, `device_rename_ir`, `device_ir_prune` (dry-run default,
`execute`/`force`/`only` args), `device_set_info` (list of cids; batch color),
`device_setlist_create`, `device_setlist_rename`, `device_setlist_delete`,
`device_setlist_duplicate`. Descriptions state: dry-run semantics, the
never-orphan guarantee (setlist delete kills references, never pool presets),
and that the manifest-local `device_setlist_add/remove/list` are a different
(local) surface.

## Safety / invariants

- **Never-orphan:** setlist delete/duplicate operate on *references*; no path
  ever deletes a pool preset (only `ir-prune` deletes anything, and only IRs).
- **Dry-run-first:** `ir-prune` mutates nothing without `--yes`; *protected*
  IRs additionally require `--force`.
- **Non-activating:** all reads use `get_content`/listings (mental-model #4);
  notes writes use `SetContentData` on the stored blob, not the edit buffer.
- **Prompt propagation:** every write batch runs inside `client.mutating()`.
- HW validation used only `ZZC-`-prefixed artifacts, all cleaned up after.

## Post-implementation findings (HW validation round, same day)

Validating the shipped verbs end-to-end surfaced three more device
behaviours around IR deletion (all handled in code; see
`helix-protocol.md` §6 DELETE notes):

1. **Lazy file GC.** `/RemoveContent(-11)` unregisters an IR immediately,
   but its `ir/*.wav` lingers for minutes until the device garbage-collects
   it; during that window `/IrPathForHashGet` still resolves and `push_ir`
   false-positives "already on device". Fix: `delete-ir`/`ir-prune` also
   remove the backing file over SFTP (best-effort; ENOENT = the device beat
   us), making the delete complete immediately.
2. **Listing lag.** The `-11` container listing lags several seconds behind
   a write, so a just-pushed IR can be invisible to one `list_irs`. Fix:
   `resolve_device_ir_live` retries under a 2001 subscription (ambiguity
   still fails fast).
3. **Delete → quick re-import wedge.** Re-importing the SAME IR while the
   lazy GC is pending can leave the device with the file + path index
   resolving but no `-11` entry (sometimes for a long time). Fix:
   `delete_device_ir` detects the hash-addressed file-only state and removes
   the file alone; the device's watcher then settles back to clean.

**Adversarial-review hardening (PR #37 review):** `ir_prune` now (a) uses
**strict** listings everywhere it plans (a `/GetContainerContents` timeout or
truncated chunked reply raises instead of reading as an empty/partial
container — an "empty pool" would have orphaned every user IR), (b)
cross-checks the pool listing against every setlist's `rcid`s, (c) counts the
live **edit buffer** as a reference source, (d) surfaces unverifiable local
tones as `warnings` and refuses to execute over them without `force`, and (e)
re-scans + re-verifies the plan immediately before deleting. The wedge
cleanup is gated behind an explicit `--force-wedge`/`force_wedge` (a
lagging-but-healthy just-imported IR is indistinguishable from the wedge at
resolve time), and it is reachable from the CLI. `create_setlist` retries the
re-list before trusting the (unreliable) create-reply cid.

CLI/live validation evidence (all `ZZC-` artifacts, all cleaned up): push →
`rename-ir` → `ir-prune` dry-run (junk IR the only acted-on candidate, via
`--only`) → `delete-ir` → registry + path index + filesystem all clean;
`set-info --color green --notes …` read back exactly (colr 6 + pm__ text);
`setlist create` (cid 1194) → `rename` → `duplicate` (1 ref copied) →
`delete` ×2 with the referenced pool preset intact (never-orphan).

## Out of scope / deferred

- `.hss` setlist-bundle import/export — **needs a sample `.hss`** exported
  from the Stadium app (none exists on this machine). Backlog entry added.
- IR folders / move-to-folder (matrix §7) — content-path surface not RE'd.
- Active-preset select (#1) — excluded by scope.
- Setlist reorder on the device (`/ReorderContainerContent`) — arg shape
  still uncaptured.
