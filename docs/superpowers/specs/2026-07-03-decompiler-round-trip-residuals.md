# Decompiler round-trip residuals (follow-up)

**Date:** 2026-07-03
**Status:** Open — follow-up after the parallel-routing + surgical-hardening effort
**Baseline:** real-preset round-trip 127/211 (60%), up from 65/211 (31%). Measured by
`tests/test_decompile_acceptance.py` (compares slot model placement; `xfail`).

## Context

The parallel-routing + hardening effort (design/plan `2026-07-03-parallel-routing-
and-hardening*`) shipped: flat `lane`/`pos` block model + `split`/`join`, generate/
decompile round-trip for splits, coordinate-addressed duplicate blocks (FS/EXP +
CLI), loopers, one-switch-many-blocks, native-unit + inverted expression, IR
pass-through. Real two-split presets round-trip.

The remaining ~40% of the author's exports fail, dominated by three bigger items
that are more than relaxations. They overlap (a failing preset usually has more
than one), so they must be tackled together to move the number.

## Residual categories (measured 2026-07-03)

### 1. Snapshot references to duplicate-named blocks (~25 presets) — needs a spec-format change
FS/EXP references became coordinate-aware (`lane`/`pos`); **snapshot references
did not**. `Snapshot.disable` is a `list[str]` and `Snapshot.params` is
`dict[str, dict]` keyed by bare block name. When a snapshot references a block
whose display name is ambiguous (many real blocks humanize to generic names like
"Stereo"/"Mono"), `generate._resolve_spec_block` raises "matches multiple placed
blocks". Fix requires a **data-model change** to snapshot refs:
- `disable`: allow entries to be `{"block": name, "lane": L, "pos": P}` in
  addition to bare strings.
- `params`: the bare-name key can't carry a coordinate — switch to a list of
  `{"block": ..., "lane": ..., "pos": ..., "params": {...}}` (or a compound key).
- Thread the coordinate through `_build_snapshot_overrides` → `_resolve_spec_block`
  (already coordinate-capable), and have the decompiler emit coordinates for
  ambiguous snapshot refs (mirror `decompile._ref`).
This is the biggest single bucket and the one design decision worth doing first.

### 2. P35 branch-lane I/O routing (~15 presets) — a sub-feature
Some split branch lanes carry their own `P35_Input*` / `P35_Output*` /
`P35_OutputPath2A`/`2B` / `P35_OutputMatrix` endpoint blocks in `b14+` slots.
`decompile._block_entry` / `_reconstruct_path_blocks` call `library.load_block`
on them and raise `KeyError` (they're routing infrastructure, not user blocks).
Two-part fix:
- **decompile:** treat `P35_Input*`/`P35_Output*` models as structural (skip like
  b00/b13 endpoints, wherever they appear — not just at slot 0/13).
- **generate:** if a faithful round-trip needs those branch-lane endpoints
  emitted, `_emit_splits` must produce them; otherwise verify the device accepts
  a branch without explicit endpoints. Decide by regenerating a fixed preset onto
  hardware.

### 3. IR block with no assigned IR (~17 presets) — an edge
Some `HX2_ImpulseResponse*` slots carry **no** `irhash` (no IR loaded). The
cataloged block's `default_irhash` is also None, so `generate._resolve_irhash`
raises "IR block requires an `ir` field". Options: allow an IR block to generate
with an empty/absent irhash when the source had none (match the source), or emit
a sentinel. Decide against real "empty IR block" exports.

### Smaller edges
- A few footswitch refs with an empty `"block"` name (2 presets).
- The acceptance test compares slot *model placement*, not the full body — tighten
  it to a `strip_provenance` body compare once the categories above shrink, so the
  scoreboard means byte-fidelity.

## Sequencing recommendation

1. Snapshot coordinate refs (#1) — biggest bucket; brainstorm the format first.
2. P35 branch-lane I/O (#2) — mechanical decompile skip + a generate/device check.
3. IR-no-assignment (#3) — small, needs a device check.
4. Tighten the acceptance test to a body compare; aim to flip `xfail → xpass`.

Each is its own spec → plan → implement cycle. Known non-blocking Minors from the
build (logged in the SDD ledger) can be swept alongside: `_bnn_keys` duplicated in
`user_keys`/`_name_index`; a few import-inside-function nits; `_validate_splits`
dead `depth != 0` branch; the per-lane capacity guard message (`_HSP_BNN_RANGE`).
