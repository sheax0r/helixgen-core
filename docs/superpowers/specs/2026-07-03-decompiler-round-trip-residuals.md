# Decompiler round-trip residuals (follow-up)

**Date:** 2026-07-03 (updated 2026-07-05)
**Status:** Categories 1, 2, 3 and the Minors + both one-offs **DONE**. Category 2
(P35 branch-lane I/O) closed 2026-07-05 on branch
`hardening/p35-endpoint-passthrough` (see
`docs/superpowers/specs/2026-07-05-p35-endpoint-passthrough-design.md`). **Category
5 items #1 (block bypass read at the bNN `@enabled` level) and #3 (verbatim
per-block `raw` = harness + extra dual-cab slots + constant `favorite: 0`)** are
now also **DONE**, on branch `hardening/category5-bypass-and-dualcab` (see
`docs/superpowers/specs/2026-07-05-category5-bypass-and-dualcab-design.md`).
Category 5 items #2 and #4, the snapshot enable-override (Case B), and the
top-level unmodeled state remain open — see the Category 5 section below.
**Baseline:** real-preset round-trip was 127/211 (60%) → 194/211 → **now 211/211**
on the tightened **endpoint-inclusive model** bar (`tests/test_decompile_acceptance.py`,
`xfail` removed 2026-07-05). NOTE: that bar compares slot *model* placement, not
sonic fidelity. A new, separate sonic-fidelity scoreboard
(`tests/test_decompile_sonic_fidelity.py`) now covers per-block base bypass,
effective per-snapshot bypass, every slot's model, every slot's param values,
`harness`, and `favorite` — also **211/211** as of items #1/#3 above.

## Status update (2026-07-05) — Category 5 #1/#3 DONE (bypass + dual-cab raw)

Closed (branch `hardening/category5-bypass-and-dualcab`, see
`docs/superpowers/specs/2026-07-05-category5-bypass-and-dualcab-design.md`):

- **Item #1 (block bypass read at the wrong level)** — DONE. `decompile`/`generate`
  now read and write bypass at the `bNN.@enabled` level (base value, snapshot
  overrides, footswitch controller) instead of the inert `slot[0].@enabled`.
  Base value and effective per-snapshot bypass round-trip; bypass-footswitch
  assignments are preserved.
- **Item #3 (dual-cab slots + harness dropped)** — DONE. New `BlockEntry.raw`
  field (`{"harness": ..., "slots": [...]}`) preserves the bNN `harness` dict
  (non-deterministic, present on 24/55 real dual-cabs and 166/206 single WithPan
  cabs — must round-trip verbatim, never synthesized) and any slots beyond
  `slot[0]` (the second cab of a dual-cab). `generate` also now always emits
  `bnn["favorite"] = 0`, matching the constant `0` observed across all 2172
  bNNs in the corpus.
- **New scoreboard.** `tests/test_decompile_sonic_fidelity.py` ingests all
  `data/*.hsp` into one shared library, round-trips each, and asserts per-user-block
  equality of base bypass, effective per-snapshot bypass (over named snapshots,
  with an explicit source-null skip for the ~30 presets with undefined recall in
  a named, base-`False` slot), every slot's model, every slot's param values,
  `harness`, and `favorite` — **211/211**.
- **Confirmed harmless:** the one anomalous slot-level `@enabled: False` in the
  corpus (Megadeth b05, a single-slot IR cab with bNN base `True`) is discarded
  by item #1's rewrite of slot[0]'s `@enabled` to the inert exemplar; pending
  hardware confirmation that this IR cab still sounds identical (see
  post-implementation hardware-verify step in the design doc).

Still deferred (see Category 5 section below for detail):
- **Item #2** — input-block params still inherited from the chassis rather than
  the source b00 slot.
- **Item #4** — `preset.params` (tempo, `inst1Z`, `activeexpsw`, …) still
  inherited from the chassis rather than the source body.
- **Snapshot enable-override / source-null recall ("Case B").** A base-`False`
  block that is enabled in some named snapshot with **zero** named explicit
  `False` entries would emit no snapshots array and read base `False` in every
  snapshot — silently wrong. 0/211 in the corpus today but not structurally
  impossible; the decompiler warns if it's ever encountered. The related
  `null`-at-named-snapshot recall (~30 presets, "unreliable recall" per Category
  4) is deliberately not asserted by the new scoreboard (regen densifies to a
  `True` fill) and remains an open semantic gap pending the snapshot
  enable-override landing.
- **Top-level unmodeled state** — `sources` scribble labels/colors/`fs_topidx`,
  `meta.info`, `preset.xyctrl`, snapshot `valid`/`expsw` — keeps the full-body
  compare at 0/211.

## Status update (2026-07-05) — Category 2 DONE + Category 5 opened

Closed (branch `hardening/p35-endpoint-passthrough`):
- **Category 2 (P35 branch-lane I/O)** — DONE. New `StructuralEntry(raw, lane, pos)`
  captures endpoints AND orphaned/cross-path split-join verbatim; generate re-emits
  them. Detection: split/join whose `endpoint` partner is an input/output endpoint
  (not the complementary block) is "orphaned" → verbatim; balanced pairs stay
  semantic. Acceptance test tightened to compare every flow `bNN` model (incl.
  b00/b13); `xfail` removed → **211/211**.
- **One-off: US_UK Stereo** — DONE. Capacity guard now counts `BlockEntry` per lane
  (main ≤12, branch ≤12) instead of per path.
- **One-off: test.hsp** — DONE. Decompile emits the 32-hex IR hash when a wav
  basename is ambiguous.

**Hardware test (Black Keys, Stadium XL):** topology loaded correctly — the P35
cross-path split-join renders and routes as authored. **But the preset was silent**,
which exposed Category 5 below. Two facts from that investigation:
- **Input=None is faithful, not a bug.** Black Keys' source has path-0 input =
  `P35_InputNone` (one of only **2/211** presets — the other is `MUSE.hsp`; the
  other 209 use `InputInst1`/`InputInst1_2`). helixgen reproduced it faithfully;
  the source itself is silent on cold load (likely a shared preset that ships with
  input unset). Setting Input 1 on-device restores audio.
- The **211/211 model bar does not imply a working/sonic clone** — see Category 5.

### 5. Sonic-fidelity gaps (exposed 2026-07-05) — next cycle
A round-tripped preset reproduces routing + block *models* but is not a byte- or
sonic-faithful clone. Ranked by audio impact:
1. **Block bypass state read at the wrong level (highest impact).** — **DONE**
   (2026-07-05, branch `hardening/category5-bypass-and-dualcab`). A block's real
   bypass is at the `bNN` level (`bNN.@enabled.value`, plus a `targetbypass`
   footswitch controller and a per-block snapshot bypass array
   `bNN.@enabled.snapshots`). `decompile._block_entry` read the *slot* level
   (`slot[0].@enabled`, ~always `True`), so **bypassed blocks round-tripped as
   enabled**, and bypass-footswitch assignments + per-block snapshot bypass were
   dropped. (Black Keys b03/b04/b05/b06/b15 flipped off→on.) Fixed: decompile/generate
   now read/emit `bNN.@enabled` (value + snapshots + controller) instead of the
   slot level. Verified by the new `tests/test_decompile_sonic_fidelity.py`
   scoreboard (211/211). The snapshot enable-override / source-null recall
   ("Case B", see status update above) remains a deferred edge.
2. **Input-block params are chassis leftovers.** `_rewrite_input_endpoint` swaps the
   b00 model but keeps the *chassis* params (frankenstein `Pad:1`, `decay:0.1` on an
   `InputNone`). Should carry the source b00 slot's params. **Still open.**
3. **Dual-cab slots dropped.** — **DONE** (2026-07-05, branch
   `hardening/category5-bypass-and-dualcab`). Source `b10`/`b16` cab `slot`
   arrays are length 2 (dual cab); regen emitted length 1. Fixed via the new
   `BlockEntry.raw` field (`raw.slots` = verbatim `slot[1:]`, `raw.harness` =
   verbatim `bNN.harness` dict — non-deterministic, present on 24/55 real
   dual-cabs and 166/206 single WithPan cabs, so preserved rather than
   synthesized) plus a constant `favorite: 0` on every emitted bNN. Documented
   in `CLAUDE.md` under "Optional: per-block verbatim state (`raw`)". Verified
   by `tests/test_decompile_sonic_fidelity.py` (211/211).
4. **`preset.params` inherited from the chassis** (tempo 120 vs source 157; `inst1Z`
   impedance mode; `activeexpsw`). Should carry from the source body. **Still
   open.**

Plus the previously-noted unmodeled top-level state (`sources` scribble
labels/colors/`fs_topidx`, `meta.info`, `preset.xyctrl`, snapshot `valid`/`expsw`)
that keeps the full-body compare at 0/211. **Still open.**

## Status update (2026-07-04)

Closed this cycle (see `docs/superpowers/plans/2026-07-04-snapshot-fidelity-and-ir-edge.md`
and specs `2026-07-04-snapshot-coordinate-refs-design.md`,
`2026-07-04-dense-snapshot-arrays-design.md`):
- **Category 1 (snapshot dup-named refs)** — DONE. Coordinate-aware `disable`/`params`
  (dual-form), threaded through generate + decompile `_ref`.
- **Category 3 (IR-no-assign)** — DONE. `no_ir` marker round-trips hash-less IR slots.
- **Minors** — DONE. Empty-block-name → model_id fallback (`_ref_name`); expression
  recovery filters out non-EXP / bool-range controllers; `_ref` now emits `path`
  (incl. 0) on cross-path `(lane,pos)` collisions.
- **New Category 4 (dense snapshot arrays)** — DONE. Fixes a user-reported hardware
  recall bug: sparse `@enabled`/param `snapshots` arrays (`null` on live snapshots)
  are now densified (`null`→base). Decompile filters base-equal phantom overrides.

**Remaining (17/211):** Category 2 (P35 branch-lane I/O, ~15) below, plus two
one-off outliers (a 13-block path exceeding the 12 user slots; an ambiguous IR
basename). Tightening the acceptance test to a full-body `strip_provenance` compare
still waits until Category 2 lands.

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
