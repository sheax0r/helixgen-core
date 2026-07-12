# Multi-setlist support + device-client refactor — design spec

Status: **approved design, ready for implementation plan** (2026-07-12).
Supersedes the findings/handoff note
`2026-07-12-helix-content-model-multisetlist-refactor.md` (kept as the
reverse-engineering reference). Builds fresh from `main`; the prior
setlist-sync attempt was backed out and nothing from it is reused.

Companion protocol reference: `docs/helix-protocol.md`. Content-model facts
(containers, `cctp`, reference semantics, the 2001-subscription requirement)
are taken as given from the findings note and are not re-derived here.

## 1. Goal

Model setlists the way the device does — a **preset pool** in container `-2`
plus named **setlists** under `-5` that hold **references** (`cctp 1003`,
`rcid`→pool preset) — so a single authored tone can live in multiple setlists.
Deliver a manifest-driven `device sync <setlist>` that rebuilds a setlist's
references pool-first and never orphans a still-referenced preset, and refactor
the device client so an agent cannot misuse the raw protocol.

## 2. Scope

**In scope**

1. A single local file, `~/.helixgen/setlists.json`, that folds the existing
   slot ledger into a manifest of desired setlist membership + observed device
   placement (§3).
2. Device-client refactor: container/cctp enums, the `-5` correction,
   privatized raw primitives, model-correct high-level ops, and a
   `client.mutating()` 2001-subscription context (§4).
3. Reference-based sync engine: `device sync <setlist>` and
   `device sync --all` (§5).
4. Management + authoring surfaces: CLI verbs, MCP tools, and skill updates —
   the manifest is **never hand-edited** (§6).
5. Quick-win: `preferences._validate_device_model` accepts the `stadium_xl`
   MCP token so the user's real `preferences.json` stops throwing (§7).

**Out of scope / deferred** (tracked in `docs/device-backlog.md`)

- **Device-side setlist *creation* (#8).** The 2002 command is uncaptured. The
  user creates a setlist by hand in the Stadium app; helixgen resolves it by
  name. `device sync` errors clearly when a manifest names a setlist the device
  doesn't have. No stub op that pretends to create one.

## 3. The local file: `~/.helixgen/setlists.json`

One file (override `$HELIXGEN_SETLISTS`) replaces `~/.helixgen/device-slots.json`.
On first load, if the old ledger exists and the new file does not, its entries
are migrated in and the old file is left in place (read-once, not deleted).

Two clearly separated halves keep desired-state and observed-state from
tangling:

```jsonc
{
  "version": 1,

  // DESIRED — the explicit path map (tone registry). A tone appears once.
  "tones": {
    "White Limo Lead — Les Paul Jr": {
      "path": "/Users/…/presets/White_Limo_Lead_Les_Paul_Jr.hsp",
      "content_hash": "sha256:…",   // of the .hsp body; lets sync skip unchanged
      "source": "hsp"               // "hsp" | "save" | "create" | "push"
    }
  },

  // DESIRED membership, ordered (order == slot order within the setlist)
  "setlists": {
    "helixgen": ["White Limo Lead — Les Paul Jr", "…"]
  },

  // OBSERVED — rebuilt from a fresh device listing on every sync. Never trusted
  // as input to a delete; always cross-checked by re-listing (findings gotcha 5).
  "observed": {
    "pool": {
      "White Limo Lead — Les Paul Jr": { "cid": 1000, "posi": 3 }
    },
    "setlists": {
      "helixgen": {
        "cid": 42,
        "refs": {
          "White Limo Lead — Les Paul Jr": { "ref_cid": 1003, "posi": 0 }
        }
      }
    }
  }
}
```

**Identity.** A manifest tone is identified by its **name** (the key in
`tones`), which is also the device pool preset's name — the only reliable
device-side key (returned cids are unreliable; §4). The `path` is an explicit
map entry, so the displayed label is decoupled from the filename. Names must be
unique within the file; `device setlist add` rejects a colliding name with a
clear message.

**Pathless placements.** Tones placed from the live edit buffer (`save`) or an
on-device copy (`create`) have no `.hsp` source: `path: null`, `source` tags the
origin. They still appear in `device slots` and are never candidates for a
content-hash re-push (nothing to hash), but can be referenced into setlists.

**Migration from `device-slots.json`.** Each old ledger entry
(`{setlist, posi, name, cid, source_kind, source_path}`) becomes a `tones` entry
(`name → {path: source_path, source: source_kind}`) plus a membership entry in
`setlists[setlist]` and an `observed` placement. Migration is covered by a unit
test against a fixture of the current ledger shape.

## 4. Device-client refactor

All in `src/helixgen/device/client.py` unless noted.

### 4.1 Enums + the `-5` correction

Replace the loose module constants with two enums (kept import-compatible via
module-level aliases so existing call sites don't break in one commit):

```python
class Container(IntEnum):
    FACTORY = -1
    POOL = -2            # the only container that accepts /CreateContent
    SETLISTS_ROOT = -5   # holds setlist items (cctp==1001); NOT a setlist itself
    USER_IRS = -11

class Cctp(IntEnum):
    PRESET = 1000
    SETLIST = 1001
    REFERENCE = 1003
    TEMPLATE = 1002
```

**Correction:** today `THROWAWAY = -5` is wrong. `-5` is the setlists *root*;
`Throwaway` is a child setlist with its own positive cid enumerated under `-5`.
`list_setlists()` is rewritten to enumerate `cctp==1001` items under
`SETLISTS_ROOT` (plus resolve their friendly names), replacing the hard-coded
`(FACTORY, USER, THROWAWAY)` sweep.

### 4.2 Privatize the raw primitives

Move the composable, model-blind primitives behind a `client._raw` namespace
(a small helper object holding the current bound methods): `create_content`,
`create_copy`, `create_from`, `set_content_data`, `delete`, `save_preset_with_cid`,
`save_edit_buffer_to`, `push_to_slot`. They keep working (used internally) but
are no longer the ergonomic public surface.

**Guardrails at the danger points:**

- `_raw.create_content(container, …)` raises `HelixError` if
  `container != Container.POOL` ("setlists reject CreateContent → -47; use
  `reference_into_setlist`").
- `_raw.create_copy` docstring + a runtime note: "creates a **reference**, not a
  copy; deleting the referenced pool preset orphans it → -21."

### 4.3 Model-correct high-level ops (the public write surface)

- `install_into_pool(body, name, *, template_blob, irs=…) -> pool_cid`
  — CreateContent in `-2`, SetContentData; re-list `-2` by name to recover the
  real cid.
- `reference_into_setlist(setlist_cid, pool_cid, pos) -> ref_cid`
  — AddContentsToContainer → a `1003` reference; re-list to recover `ref_cid`.
- `remove_reference(setlist_cid, ref_cid) -> bool`
  — RemoveContent of the **reference only** (never the pool preset).
- `mirror_setlist(setlist_cid, ordered_pool_cids) -> None`
  — reconcile a setlist's references to exactly `ordered_pool_cids` in order:
  add missing, remove extra, reorder as needed; never orphans.
- `resolve_setlist_cid(name) -> Optional[int]` and
  `list_user_setlists() -> [{cid, name, …}]` — enumerate `-5`.

### 4.4 `client.mutating()` context

```python
with client.mutating():
    ...  # every device write flow runs here
```

Opens a `HelixSubscriber(ip, ports=(2001,))`, subscribes to all topics, settles
~0.6 s, then yields; closes the subscriber on exit. This activates the device's
watched-index propagation (the `push_ir` pattern, generalized) so
create/copy/delete land promptly instead of against a lagging container index
(findings gotcha 4). `install_into_pool` / `reference_into_setlist` /
`remove_reference` assert they are called within an active `mutating()` (or open
one themselves if not). `push_ir` is refactored to reuse this context rather
than hand-rolling its own subscription.

## 5. Sync engine

New module `src/helixgen/device/setlist_sync.py` (the old `sync.py`
directory-mirror-into-`-2` path is retired; its reusable IR-upload helper moves
here). Pure reconcile logic is separated from device I/O so it unit-tests
against a fake client.

`device sync <setlist>` (one setlist) and `device sync --all [--gc]`:

1. **Resolve the setlist** by name under `-5`. If absent → `HelixError`
   ("create '<name>' in the Stadium app first, then re-sync"; deferred #8).
2. **Reconcile the pool** (inside `client.mutating()`), for the union of tones
   referenced by the setlist(s) being synced:
   - No pool preset with that name → upload its IRs, then `install_into_pool`.
   - Exists but `content_hash` changed → `SetContentData` re-push (+ IR upload
     if new IRs).
   - Exists, hash unchanged → skip (idempotent, fast).
3. **Rebuild references**: `mirror_setlist(setlist_cid, [pool_cid for tone in
   manifest order])` — adds missing, removes extra, orders to match.
4. **Garbage-collect the pool** — **only on `--all`** (this is the fast,
   whole-library reconcile). A pool preset referenced by no setlist in the
   manifest *and* not present in `tones` is deleted. Anything still referenced
   anywhere is never touched → no orphans. Single-setlist `device sync <name>`
   never GCs.
5. **Refresh `observed`** from a final device listing and save the file.

Result dict: `{ok, setlist(s), pool:{installed,updated,skipped},
references:{added,removed}, gc:{deleted}, irs:[…], errors:[…]}`. Per-tone
failures append to `errors[]` without aborting the run (matches the current
sync's resilience contract).

**Hardware spike — validate before building step 3.** The findings confirm
`RemoveContent → -21` only on an *already-orphaned* reference; clean removal of
a **live** reference (pool preset still alive) is untested. `mirror_setlist`
depends on it. First implementation task is a device spike: reference a pool
preset into the `helixgen` setlist, then `RemoveContent(setlistCid, [refCid])`
with the pool preset alive, and confirm success + that the pool preset survives.
If it fails, capture the app's real remove command (`tcpdump` port 2002) before
continuing. Expected to pass.

## 6. Surfaces (CLI + MCP + skills; no hand-editing)

**CLI** (`src/helixgen/cli.py`, `device` group):
- `device setlist list [--json]` — manifest setlists + tone counts + observed
  drift.
- `device setlist add <setlist> <tone.hsp> [--pos N]` — register the tone's path
  (reading its `meta.name`) and append to the setlist's membership.
- `device setlist remove <setlist> <tone>` — drop membership (keeps the tone in
  `tones` if other setlists use it).
- `device setlist create-local <setlist>` — create an empty setlist in the
  manifest (device-side creation deferred #8).
- `device sync <setlist>` / `device sync --all [--gc]` — the engine above.
- Existing `device slots …` verbs read the new file's `observed`/`tones`.

**MCP** (`src/helixgen/mcp_server.py`) mirrors: `device_setlist_list`,
`device_setlist_add`, `device_setlist_remove`, `device_sync_setlist`,
`device_sync_all`. Path-based like the rest (no base64).

**Skills:**
- `tone` — after authoring a `.hsp`, offer "add to a setlist?" and, on yes, call
  `device setlist add` so authoring flows straight into membership.
- `device` — the sync section is rewritten around the pool+reference model and
  drives `device sync <setlist>`; the destructive whole-`-2`-mirror framing is
  removed.

## 7. Quick-win: `device.model` load fix

`src/helixgen/preferences.py::_validate_device_model` accepts both display forms
("Stadium XL") and MCP tokens ("stadium_xl") case/separator-insensitively,
normalizing to the display form. ~10 lines + a unit test with the user's real
value. Shippable on its own; included here so the real `preferences.json` loads.

## 8. Testing

TDD throughout (failing test first), following the existing suite conventions
(`PYTHONPATH=$PWD/src python -m pytest`, fixtures under skip-if-not-present
guards):

- **Manifest unit tests**: load/save, migration from a `device-slots.json`
  fixture, add/remove membership, content-hash change detection, unique-name
  enforcement, pathless (`save`/`create`) entries.
- **Reconcile unit tests** (fake client): pool install/update/skip decisions;
  reference add/remove/reorder diff; GC only-on-`--all`; never-orphan invariant
  (a tone in two setlists, removed from one, survives in the pool + the other).
- **Client unit tests**: enum values, `-5` setlist enumeration, guardrail raises,
  `mutating()` opens/closes a subscriber (injected fake).
- **Preferences unit test**: `stadium_xl` and `Stadium XL` both load.
- **Hardware validation** (pre-approved): the remove-reference spike (§5), then
  one full `device sync helixgen` round-trip against the user's Stadium XL —
  confirm the setlist shows the right tones in order, a tone shared across two
  setlists installs once in the pool, and re-running is a no-op (idempotent).

## 9. Release

After green tests + hardware validation, bump the version in
`.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` (and the lib
version in `pyproject.toml` + `src/helixgen/__init__.py`), commit
`release X.Y.Z — multi-setlist support`, PR, merge to `main`; the automated
workflow tags + fast-forwards `stable`. Do not move `stable`/tags by hand.

## 10. Deferred / follow-ups (in `docs/device-backlog.md`)

- **#8** device-side setlist creation (uncaptured 2002 command; tcpdump RE).
- If the §5 spike fails, a captured reference-removal command becomes a
  prerequisite (currently expected unnecessary).
- The benign orphaned reference in the `helixgen` setlist from prior RE clears
  on the first `device sync helixgen` rebuild.
