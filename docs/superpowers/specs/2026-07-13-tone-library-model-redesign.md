# Tone-library model redesign — retire the ledger, one tone-centric manifest

**Date:** 2026-07-13
**Status:** design — approved, pending implementation plan
**Supersedes / finishes:** the "fold the slot ledger into the manifest" work from
`2026-07-12-multisetlist-support-design.md` §3, which shipped as a *co-location*
(a redundant `entries` section alongside `tones`/`setlists`/`observed`) rather
than the intended *replacement*. This spec completes that replacement.

## 1. Goal

Make the **tone** — not the `.hsp` file, and not a slot-ledger row — the
first-class managed entity. For every tone helixgen authors (or the user
imports), the library records:

1. the tone's **content** (its `.hsp`, or nothing if it originated on the
   device), plus identity and provenance;
2. **what slot it occupies on the Helix** (or none — meaning it is not on the
   device); and
3. **which setlists it belongs to**, in order.

`device sync` then reconciles the device to this desired state — installing,
updating, reordering, **and removing** the tones helixgen manages — while never
disturbing presets it does not manage.

This retires `SlotLedger` entirely: its data folds losslessly into the manifest
(the codebase already proves this via `SetlistManifest._migrate_from_ledger`), so
there is exactly **one** management model with **one** writer.

## 2. Why the ledger goes away

`SlotLedger` (`src/helixgen/device/ledger.py`) writes an `entries` section into
`~/.helixgen/setlists.json` keyed by `(setlist, posi)`. Every field it carries —
`name`, `cid`, `posi`/`slot_label`, `source_kind`, `source_path` — already maps
onto the manifest's `tones` (path/source/content_hash), `setlists` (ordered
membership), and `observed` (cid/posi) sections. `_migrate_from_ledger` performs
exactly that decomposition today. The `entries` section is therefore pure
redundancy: two records of the same facts, in one file, that can drift.

The only thing `entries` uniquely tags is the pathless origin of a tone placed
from the live edit buffer (`save`) or an on-device copy (`create`); that survives
as a `source` value on the `tones` record (§3).

## 3. Data model

One file, `~/.helixgen/setlists.json` (override `$HELIXGEN_SETLISTS`) — the
**tone library manifest**. `version` bumps to **2**. `SlotLedger` and the
`entries` section are deleted. Three sections:

```jsonc
{
  "version": 2,

  // The tone registry (the library). A tone appears exactly once, keyed by name.
  "tones": {
    "White Limo Lead — Les Paul Jr": {
      // CONTENT — source of truth is the file (or the device, if pathless)
      "path":         "/…/White_Limo_Lead_Les_Paul_Jr.hsp",   // null = pathless
      "content_hash": "sha256:…",   // of the .hsp body; lets sync skip unchanged. null if pathless
      "doc":          "/…/White_Limo_Lead_Les_Paul_Jr.md",    // optional companion description; null if none

      // PROVENANCE — how the tone entered the library
      "source": "authored",   // "authored" | "import-local" | "import-device" | "save" | "create"

      // DESIRED PLACEMENT — NOT derivable from the .hsp
      "slot":   "5A",         // "5A".."8D" = on device at that address;
                              //   "auto"    = wants device, address chosen by next sync;
                              //   null      = not on device

      // OBSERVED — rebuilt from a fresh device listing on every sync; advisory
      "device": { "cid": 1000, "posi": 17 }   // or null when off device / never synced
    }
  },

  // Named setlists: ordered membership + whether they are mirrored to the device.
  "setlists": {
    "helixgen": { "tones": ["White Limo Lead — Les Paul Jr", "…"], "synced": true },
    "metal-drafts": { "tones": ["…"], "synced": false }
  }
}
```

### 3.1 A tone is content + identity + management state

| Layer | Fields | Source of truth |
|---|---|---|
| **Content** | `path`, `content_hash`, `doc` | the `.hsp`/`.md` file — or the device, if `path` is null |
| **Identity** | the `tones` key (name) | the manifest |
| **Management state** | `source`, `slot`, `device`, setlist memberships | the manifest — *not* in the `.hsp` |

The `.hsp` remains the **audio** source of truth (CLAUDE.md's ".hsp is
canonical" principle is unchanged). The manifest is the **management** source of
truth that wraps it. Placement and membership are *relationships between a tone
and the device/library*, not properties of the sound, so they cannot and must
not live in the `.hsp`.

A tone need not have an `.hsp`: `save`/`create`-origin tones have `path: null`
and `content_hash: null`. They are still first-class library tones (listed,
placeable, referenceable) but are never candidates for content-hash re-push
(there is nothing local to hash).

### 3.2 The `slot` field carries the on-device intent

- `null` — the tone is **not** on the device.
- `"auto"` — the tone **should** be on the device; the address is unassigned and
  the next sync picks the first free user slot and rewrites this to the concrete
  label. The user never has to type a slot.
- `"5A".."8D"` — the tone is on the device at that concrete address (either
  sync-assigned, or explicitly pinned by the user via `device add --slot 5A`).

"On the device" ⟺ `slot != null`. This is the model the user described: the user
setlist *is* the on-device population, one tone per slot, at most one tone per
slot. User-setlist **order does not matter** (slots are just addresses);
**named-setlist order does** (a curated sequence).

### 3.3 Identity

A tone is identified by its **name** (the `tones` key), which is also the device
preset's name — the only reliable device-side key (returned cids are unreliable;
see the multisetlist spec §4). Names are unique within the manifest. Renaming a
tone is an explicit operation (updates the key, the on-device preset name, and
every setlist membership), never a side effect of editing `meta.name` in the
`.hsp`.

### 3.4 Invariant

**A tone that is a member of any `synced` setlist must have a non-null `slot`.**
A synced setlist is fully present on the device, so all its tones must be too.
The invariant is enforced at the mutation points (§4), not merely at sync time.

## 4. Sync semantics — a managed-set mirror

`device sync [<setlist>|--all]` reconciles the device to the manifest. It is a
mirror of **the tones helixgen manages**, not of the whole device.

**Managed vs. untracked.** A device preset is *managed* iff its name matches a
manifest tone. Everything else on the device is *untracked*: sync never modifies
it, never deletes it, and never lets auto-assign reuse its slot — sync fills
around it.

**Reconcile (pool + user population).** For each manifest tone:

- `slot != null`, not on device → **install** it (transcode `.hsp` → `_sbepgsm`,
  `SetContentData` into the pool), resolving `"auto"` → first free user slot and
  recording the concrete label + observed `device`.
- `slot != null`, on device, `content_hash` changed → **re-push** its content.
- `slot != null`, on device, unchanged → **skip** (idempotent).
- `slot == null`, but observed on device from a prior sync → **delete** it from
  the device (keep it in the library).

**Named setlists.** For each `synced` setlist, rebuild its device references to
match manifest order (add / remove / reorder), **never orphaning** a pool preset
that another synced setlist still references. `unsynced` setlists are pure local
drafts and are never touched on the device. Setlist *creation* on the device is
still deferred (backlog #8): syncing a synced setlist the device lacks errors
clearly ("create '<name>' in the Stadium app first").

**Unsync cascade.** `device unsync <tone>` sets `slot: null`. If the tone is
still a member of any `synced` setlist, it is **auto-removed** from those
setlists too (preserving the §3.4 invariant), and the next sync deletes it from
the device. It remains in the library; re-adding it (`device add`) restores a
slot on the next sync.

**Resilience.** Sync is idempotent and re-runnable after the Stadium's flaky
network drops mid-operation (per the multisetlist spec's reconnect behavior).

## 5. Surfaces (CLI + MCP)

Mostly rewiring existing verbs onto the new model:

- **Authoring auto-registers.** `generate` and the `tone` skill, after writing
  the `.hsp` (+ optional `.md`), record the tone in the manifest
  (`source: "authored"`, `slot: null`). Every tone helixgen creates appears in
  the library, **off the device by default**. Adding it to the device is a
  separate step.
- **`register <tone.hsp> [--doc <md>]`** — import an existing local `.hsp` into
  the library (`source: "import-local"`).
- **`device add <tone> [--slot auto|5A]`** — mark a tone for the device
  (default `--slot auto`). **`device unsync <tone>`** — clear its slot (cascades
  per §4).
- **`setlist create|add|remove`** — manage named-setlist membership + order.
  **`setlist sync-on|sync-off <name>`** — flip the `synced` flag (turning it on
  marks all members for the device via the §3.4 invariant).
- **`device sync [<setlist>|--all]`** — the managed-set mirror (§4). One engine.
- **`library list` / `device slots list [--verify]`** — the read view: every
  tone with its slot, on/off-device state, and setlist memberships; `--verify`
  cross-checks the live device and flags drift.

MCP mirrors these (`device_setlist_*`, `device_sync_setlist`, `device_sync_all`,
plus the new register/add/unsync tools). The MCP install/delete parity gap
(backlog #6) closes naturally: the shared per-tone sync core records the manifest
and uploads IRs, so MCP and CLI reach identical behavior.

## 6. Migration (v1 → v2)

`SetlistManifest.load()` upgrades an existing `setlists.json` in place:

1. For each `entries`/`observed` placement, set `tones[name].slot` from the
   observed slot label and `tones[name].source` from `source_kind`; populate
   `tones[name].device` from the observed cid/posi.
2. Convert `setlists: {name: [tone, …]}` → `setlists: {name: {tones: [tone, …],
   synced: <true iff the setlist was observed on the device>}}`.
3. **Drop the `entries` section.** Bump `version` to 2.

The legacy `~/.helixgen/device-slots.json` read-once path is retired entirely
(it was already migrated into `entries` on first load under v1). Migration is
covered by a unit test against a v1 fixture (a `setlists.json` carrying both
`entries` and the three sections).

## 7. Retire `SlotLedger` + reframe `device slots`

Delete `src/helixgen/device/ledger.py` (`SlotLedger`, `default_ledger_path`,
`_migrate_from_legacy`). Callers in `cli.py` (`_ledger_record`/`_rename`/
`_remove`, the `device slots` verbs) re-home onto the manifest:

- `device slots list [--verify]` → the library read view (§5).
- `device slots restore` → a thin "set desired slot + sync this tone" wrapper.
- `device slots reorder` → "edit setlist order + sync".
- `device slots sync` → folds into `device sync`.

Net result: one management model, one writer, one sync path.

## 8. Testing

- **Migration** — v1 fixture (`entries` + three sections) → v2, asserting each
  ledger fact lands on the right `tones`/`setlists` field and `entries` is gone.
- **Sync reconcile** — against a fake device client: install-when-absent,
  re-push-on-hash-change, skip-when-unchanged, delete-on-unsync,
  reorder-within-setlist, untracked-preset-left-alone, auto-slot assignment,
  unsync-cascade removal from synced setlists.
- **Invariant** — mutating a tone into a synced setlist without a slot is
  rejected/repaired; `unsync` of a tone in a synced setlist cascades.
- **Auto-registration** — `generate`/authoring writes a `tones` record with
  `source: "authored"`, `slot: null`.

Follow existing TDD conventions (`PYTHONPATH=$PWD/src python -m pytest`).

## 9. Scope

**In this spec:** the tone-centric data model, the managed-set sync engine, local
`register`, auto-registration on authoring, migration v1→v2, and the retirement
of `SlotLedger`.

**Fast-follow spec (separate):** **device-import** — `register --from-device
<slot>`: a non-activating content read (`client.get_content`, shipped 2.18.0) →
`_sbepgsm → recipe → .hsp` transcode → save `.hsp` → register with
`source: "import-device"` and the observed slot. This needs the reverse-transcode
round-trip validated independently and is cleanly separable, so it is not part of
this spec's implementation.

## 10. Backlog impact

- Closes the ledger-vs-manifest redundancy (this spec's premise).
- **#6** (single-tone install/delete parity) — subsumed: the shared sync core
  gives MCP the same IR-upload + manifest-record behavior as the CLI.
- **#7** (slot ordering) — reframed: ordering is now setlist order in the
  manifest + sync, not a separate destructive ledger reorg.
- Device-import remains open as the fast-follow spec (§9).
- **#8** (device-side setlist creation) is still deferred and still gates
  syncing a new named setlist.
