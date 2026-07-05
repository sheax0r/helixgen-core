# Dense snapshot arrays (round-trip residual #4 — user-reported recall bug)

**Date:** 2026-07-04
**Status:** Approved — implementation pending
**Parent:** `2026-07-03-decompiler-round-trip-residuals.md` (new category, surfaced
by a user hardware report on `thunder-kiss-65.hsp`)
**Sibling:** `2026-07-04-snapshot-coordinate-refs-design.md` — shares the snapshot
code path; implemented in the same bundle.

## Problem

`generate._wrap_value_with_snapshots` copies the per-snapshot overrides array
verbatim, so a block disabled/overridden in only some snapshots emits a **sparse**
array — e.g. a flanger disabled only in snapshot 0 becomes
`@enabled.snapshots = [false, null, null, null, null, null, null, null]`.

The Stadium device expects a **dense** array: an explicit value for every *valid*
snapshot, `null` only beyond the valid-snapshot count. Empirically, 144/149 real
device exports satisfy `dense_boundary == count(valid == true)`; the 5 outliers
are hand-authored presets with non-contiguous `valid` (middle snapshots deleted
on-device), which helixgen does not generate.

`null` on a *live* snapshot is undefined recall state. Symptom (user-reported):
switching snapshots away and back does not restore the block's on/off state —
order-dependent, "doesn't turn back on."

helixgen's `_build_snapshot_metadata` already marks **all 8** snapshots
`valid: true`, so arrays must be dense to 8 to be internally consistent.

## Design

One-line densify in `generate._wrap_value_with_snapshots` (generate.py:335):

```python
# before
wrapped["snapshots"] = list(snapshot_overrides)
# after
wrapped["snapshots"] = [base if o is None else o for o in snapshot_overrides]
```

`base` is the value passed by the caller — the block's base enabled-state for
`@enabled`, the composed param value for a param. Filling `null` with `base`
means "recall the base value in that snapshot," which is the correct no-op for
snapshots that don't diverge. `snapshot_overrides` is always length 8, so the
result is dense to 8.

The existing guard stays: `if snapshot_overrides and any(o is not None ...)`. A
block with **no** snapshot variation still emits a plain `{"value": x}` (no array)
— unchanged.

### Scope decisions

- **Densify to all 8; keep all 8 `valid`.** No change to `_build_snapshot_metadata`.
  Users rely on 8 usable snapshot slots (documented in CLAUDE.md); reducing
  `valid` to `len(spec.snapshots)` would hide slots with no correctness benefit.
  Densify-to-8 keeps `dense == valid-count` and is a zero-UX-change fix.
- **Only varying blocks carry arrays** (unchanged). The device also writes dense
  arrays for non-varying blocks; that is a byte-fidelity gap that does not affect
  recall. Deferred to the future full-body-compare tightening, not this fix.

## Testing (TDD)

1. A single-snapshot `disable` → `@enabled.snapshots == [false, true, true, true,
   true, true, true, true]` (not `[false, null×7]`).
2. A single-snapshot param override → dense param array, base value in the
   non-overridden slots.
3. A block with no snapshot variation → plain `{"value": x}`, no `snapshots` key.
4. Round-trip preserves slot placement (existing acceptance test stays green).

## Hardware verification

Regenerate `thunder-kiss-65.hsp` with the flanger `disable` moved onto the **Lead**
snapshot (the preset's own content was authored on the wrong snapshot — a separate
issue from this code fix) and confirm on the Stadium XL that toggling
Distortion↔Lead restores the flanger reliably.

## Out of scope

- Reducing `valid` to the spec snapshot count.
- Dense arrays for non-varying blocks (full byte-fidelity).
- Correcting individual presets' snapshot authoring (content, not code).
