# Spec: device slot ledger + ordering + sync — helixgen

**Status:** approved design, phased build · **Written:** 2026-07-12
**Target:** lib 0.6.x + plugin release (new `device slots` verbs)

## TL;DR

When helixgen puts a tone onto the Helix (`device install` / `save` / `push` /
`create`), record **which tone landed in which slot** in a local ledger. This
lets you (1) see what helixgen placed where — offline — and detect drift from
the live device, (2) **put a tone back in the same spot**, and (3) later,
maintain a desired **order** locally and **sync** it to the device (reorganize).

Rollout is two phases:

- **Phase 1 (this release):** record + list + `--verify` drift check + `restore`
  ("put back in same spot"). No destructive device reorg.
- **Phase 2 (next release):** local `reorder` + `sync` — reconcile the device's
  slot order to the ledger, dry-run first, backup-first. Device-mutating.

## Motivation

Today the write-path commands (`cli.py` `device install/save/push/create`)
push to the device, echo `... as cid N in <setlist> slot <pos>`, and persist
**nothing**. The only local record of device slots is `device backup`'s
`manifest.json`, which is a point-in-time *snapshot* of what's on the device,
not a running record of what helixgen authored where. So there's no answer to
"which of my authored `.hsp` tones is in slot 4A" without re-reading the device,
and no way to reproduce a layout.

## Design decisions (confirmed)

- **Scope:** all placement commands record; `rename` updates the name; `delete`
  removes the entry. The ledger tracks every helixgen-driven slot change.
- **Multi-device:** single global ledger (assume one device). Documented
  limitation: pointing helixgen at a second Helix mixes records.
- **Drift:** the ledger is **advisory** (record-only). `device slots --verify`
  cross-checks the live device and flags drift; nothing auto-mutates the ledger.

## Storage

- Default `~/.helixgen/device-slots.json`; override wholesale with
  `$HELIXGEN_DEVICE_SLOTS`. Created lazily on first write. Atomic save (temp +
  `os.replace`), same pattern as `mapping.json` / `irhash.json` /
  `device-backups/manifest.json`.
- Follows the established helixgen env convention (cf. `$HELIXGEN_DEVICE_BACKUPS`
  in `device/backup.py`).

### Format (JSON)

```json
{
  "version": 1,
  "entries": [
    {
      "order": 0,
      "name": "White Limo Lead — LP Jr",
      "setlist": "user",
      "posi": 12,
      "slot_label": "4A",
      "cid": 147,
      "source_kind": "hsp",
      "source_path": "/abs/White Limo Lead — LP Jr.hsp",
      "model": "stadium_xl",
      "created_at": "2026-07-12T05:00:00+00:00",
      "updated_at": "2026-07-12T05:00:00+00:00"
    }
  ]
}
```

- **Identity of an entry = `(setlist, posi)`** — a device slot holds one preset.
  Recording the same slot again upserts (updates name/cid/source, keeps its
  `order`). `cid` and `name` are stored for corroboration + verify, not as the
  primary key (a cid can change across device operations).
- `order` — dense 0..N-1 sequence, the desired display/reorg order. New
  placements append (max order + 1). Phase 2's `reorder` renumbers.
- `source_kind` ∈ `{"hsp", "sbe", "edit-buffer", "copy"}` — how the tone got
  there, so `restore` knows whether a local source exists to re-push:
  - `hsp` (`device install`) → `source_path` is the `.hsp`; re-installable.
  - `sbe` (`device push`) → `source_path` is the `.sbe` blob; re-pushable.
  - `edit-buffer` (`device save`) / `copy` (`device create --from`) → no local
    source file; `source_path` is null and `restore` reports it's not locally
    reproducible (back it up first).
- Timestamps are ISO-8601 UTC strings injected by the CLI (`now`), never
  produced inside the module — mirrors `backup.py`, keeps the module
  deterministic/testable.

## Module: `src/helixgen/device/ledger.py`

Pure stdlib. Offline read side needs no device. Mirrors `backup.py`'s shape.

```python
def default_ledger_path() -> Path: ...        # $HELIXGEN_DEVICE_SLOTS or ~/.helixgen/device-slots.json

class SlotLedger:
    entries: list[dict]

    @classmethod
    def load(cls, path: Path | None = None) -> "SlotLedger":
        """Tolerant: missing/corrupt/unknown-version file → empty ledger, no raise."""

    def record(self, *, setlist: str, posi: int, name: str, cid: int | None,
               source_kind: str, source_path: str | None = None,
               model: str | None = None, now: str | None = None) -> dict:
        """Upsert the entry for (setlist, posi). New slot → appended with next
        order + created_at=now. Existing slot → fields updated, order kept,
        updated_at=now. Returns the entry."""

    def rename(self, *, setlist: str, posi: int | None = None,
               cid: int | None = None, new_name: str, now: str | None = None) -> bool:
        """Update the name of the entry matching (setlist,posi) or cid. False if none."""

    def remove(self, *, setlist: str, posi: int | None = None,
               cid: int | None = None) -> bool:
        """Drop the matching entry and re-densify order. False if none matched."""

    def entries_in_order(self) -> list[dict]:   # sorted by order

    def find(self, *, name: str | None = None, setlist: str | None = None,
             posi: int | None = None) -> dict | None:

    def verify(self, device_presets: list[dict]) -> list[dict]:
        """Given the device's current presets (from client.list_presets across
        tracked setlists), return one status record per ledger entry plus any
        untracked device presets. Pure — takes data, not a client."""

    def save(self) -> None:                     # atomic temp + os.replace
    # Phase 2:
    def set_order(self, new_order: list[...]) -> None
```

### verify() status vocabulary

For each ledger entry, look up the device preset at its `(setlist, posi)`:

- `ok` — a preset is there and its name matches (or cid matches).
- `changed` — a preset is there but name (and cid) differ → slot now holds
  something else, or it was renamed on the device.
- `missing` — nothing at that slot on the device.
- `moved` — the entry's `cid` is found on the device but at a **different**
  posi (helpful hint that it was reordered in the editor).

Plus `untracked` — device presets (in tracked setlists) at slots with no ledger
entry. `verify()` is a pure function over the device preset list so it's unit-
testable without a live device; the CLI fetches `client.list_presets(...)`.

## CLI (Phase 1)

`device slots` is a **Click group** (`invoke_without_command=True`) nested under
the existing `device` group; invoking it bare runs `list`.

- `helixgen device slots list [--verify] [--json]` (also the bare
  `helixgen device slots`) — list ledger entries in order.
  `slot_label  name  cid  source`. `--verify` adds a `status` column (needs the
  device). `--json` emits the raw entries (or verify records). Works offline
  without `--verify`.
- `helixgen device slots restore <name-or-slot> [--pos N] [--setlist S]` —
  **put a tone back in the same spot.** Look up the entry; re-install its
  recorded source to its recorded `(setlist, posi)` (or an override). `hsp` →
  reuse `install` path; `sbe` → reuse `push` path; `edit-buffer`/`copy` →
  error: no local source recorded. Refuses if the target slot is occupied
  unless `--force` (parity with `install`/`save`).

### Recording hooks (Phase 1)

After each device write succeeds in `cli.py`, one ledger call (load → record/
rename/remove → save). Failures to write the ledger warn on stderr but never
fail the device command (the device op already succeeded).

| Command  | Ledger action | source_kind | source_path |
|----------|---------------|-------------|-------------|
| `install`| record        | `hsp`       | the `.hsp` path |
| `push`   | record        | `sbe`       | the `.sbe` path |
| `save`   | record        | `edit-buffer` | null |
| `create` | record        | `copy`      | null (src cid noted in name/log) |
| `rename` | rename (by cid) | —         | — |
| `delete` | remove (by cid) | —         | — |

`create`/`rename`/`delete` are addressed by `cid`; `install`/`save`/`push` know
their `(setlist, posi)` directly. When only a cid is known and no slot, the
ledger matches on cid.

## CLI (Phase 2 — next release, device-mutating)

Added as subcommands of the same `device slots` group:

- `helixgen device slots reorder <name> --to <order-index>` — local-only: move
  an entry in the desired order, re-densify. No device write.
- `helixgen device slots sync [--dry-run] [--no-backup]` — reconcile the device
  so each tracked preset sits at the slot its ledger `order` implies.
  - Compute a plan: for each entry, target posi = order position within its
    setlist; diff against current device posi.
  - `--dry-run` (default-on for safety in the first cut): print the plan
    (`4A → 4B`, scratch usage) with **zero** device writes.
  - Real run: **back up the affected setlist first** (`device backup`), then
    execute moves via content read (`get_edit_buffer`/blob) + `push_to_slot`
    into empty targets, using the `throwaway` setlist as scratch to resolve
    cycles/collisions, `delete` the vacated originals, verifying each step.
  - No native move primitive exists (`client.py` audit: only
    create_copy/create_from/push_to_slot/set_content_data/delete), so moves are
    synthesized. This is why it's isolated to its own phase with dry-run +
    backup-first.

## Testing (TDD)

Phase 1 (no device needed for the module + hooks):
- `ledger.py` unit tests: record appends with order; re-record upserts + keeps
  order; rename/remove; remove re-densifies; load tolerates
  missing/corrupt/unknown-version; atomic save; `entries_in_order`; `find`.
- `verify()` pure-function tests over synthetic device-preset lists: ok /
  changed / missing / moved / untracked.
- CLI hook tests: each write-path command records the right entry. Drive the
  device commands with a **fake HelixClient** (the device tests already use one
  — reuse the pattern in `tests/test_device_cli.py`), asserting the ledger file
  gains the expected entry. Ledger isolated per-test via `$HELIXGEN_DEVICE_SLOTS`
  (autouse conftest fixture, like the IR-hash-cache isolation).
- `device slots` list/verify/json + `restore` CLI tests against the fake client.

Phase 2:
- `sync` plan computation is a pure function (ledger + device state → move list)
  → exhaustively unit-tested incl. cycles. Execution tested against the fake
  client asserting the emitted device calls; `--dry-run` asserts zero writes.

Run: `PYTHONPATH=$PWD/src python -m pytest`.

## Non-goals

- Not a replacement for `device backup`/`manifest.json` (device snapshot stays).
- Not multi-device (single global ledger; documented).
- Phase 1 does **no** destructive device reorg — that's Phase 2, gated on
  dry-run + backup.
- No new runtime deps (stdlib json + os). The `device` extra (pyzmq/msgpack) is
  only needed for the live `--verify`/`restore`/`sync` paths, not the offline
  list.

## Acceptance

Phase 1:
- After `device install foo.hsp "Foo" --pos 12`, `device slots` shows Foo at 4A
  with source = the `.hsp` — offline, Helix disconnected.
- `device slots --verify` flags an entry whose slot the editor emptied/renamed.
- `device slots restore "Foo"` re-installs foo.hsp to slot 12.
- `rename`/`delete` keep the ledger consistent.
- Full suite green; new tests added; lib + plugin version bumped, released.
