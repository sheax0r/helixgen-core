# Decompiler real-preset hardening (follow-up)

**Date:** 2026-07-02
**Status:** Open — follow-up to the "surgical preset edits" feature
**Trigger:** The real-export acceptance test (`tests/test_decompile_acceptance.py`),
which runs only when `data/*.hsp` device exports are present, revealed that the
decompiler round-trip is far less complete on real device presets than the
synthetic tests suggested.

## The finding

Across the author's **211 real device exports**, a full
decompile → `parse_spec` → `compose_preset` round-trip succeeds for **~65
(31%)**. The rest fail, in these categories (measured 2026-07-02):

| Count | Category | Cause |
|------:|----------|-------|
| ~106 | **Parallel split / looper routing** | Blocks with `P35_*` model ids (`P35_AppDSPSplitY` ×58, `P35_LooperHelixStereo` ×33, `P35_AppDSPSplitAB`, `P35_LooperHelix*`, `P35_Input*`) sit in user slots `b01..b12`. `decompile._block_entry` calls `library.load_block(model)` on them and raises `KeyError` — ingest deliberately *filters* `P35_*` (see `hsp.CHASSIS_MODEL_PREFIX`), so they are never cataloged. |
| ~15 | **Duplicate same-model block instances referenced by controllers** | A preset legitimately has two instances of the same model (e.g. an `HD2_VolPanVol` "Vol" per path in "2 Guitar Rig"), each assigned to its own footswitch/expression/snapshot. The decompiler emits those references by display name (or model_id), which cannot distinguish instances → `parse_spec` rejects the duplicate, and `generate._resolve_spec_block` would reject it too. |
| ~10 | **Unregistered / unresolvable IR** | An IR slot's `irhash` is neither registered in `mapping.json` nor equal to the block's ingest-time `default_irhash`; decompile emits the raw hash and `generate._resolve_irhash` raises `IrMappingError` → `GenerateError`. |
| ~4 | **Expression min/max outside [0, 1]** | Real EXP controllers carry ranges like `1.35`, `2.0`; `spec._parse_expression_target` requires `[0.0, 1.0]`. Decompile emits the raw controller `min`/`max`. |
| ~4 | **Misc spec-validation edges** | Duplicate switch (a controller source resolving to the same `FSn` twice), empty `"block"` name, `"min" must be a number`. Need per-case triage. |

The `AttributeError` seen during ad-hoc diagnosis was a **harness artifact**
(calling `compose_preset` without passing `irs`, so it defaulted to `None` and
`_resolve_irhash` dereferenced `None.resolve_by_basename`). The real CLI/MCP
paths always pass an `IrMapping`. Still worth a cheap guard (below).

## What this means for the shipped feature

The surgical-edit feature (decompiler + patch verbs + sidecar + CLI + MCP) is
**solid and well-tested for helixgen-generated presets** — which are serial,
uniquely-named, in-range, and always carry a sidecar spec so they never hit the
decompiler at all. It also handles the subset of real presets that fit v1's
model (serial routing, unique block names, in-range EXP, resolvable IRs).

The **orphan real-preset** path (decompile an arbitrary device export, edit,
regenerate) is only ~31% covered, and the gap is largely a function of v1's
*already-documented* limitations: **no parallel-path routing** and
**name-based block references** that assume block uniqueness.

## Proposed work (roughly by value / independence)

1. **Decompiler robustness: skip `P35_*` infrastructure (cheap, clearly correct).**
   `_block_entry` / the block-iteration in `decompile_body` should skip models
   starting with `hsp.CHASSIS_MODEL_PREFIX` exactly as `ingest` does, so
   decompile never crashes on splits/loopers/inputs. Note this does **not** make
   split presets round-trip (they need parallel routing), but it turns a crash
   into a graceful, partial spec — and lets a `log`/warning report the dropped
   infrastructure. Decide: warn-and-drop vs. refuse-with-clear-error for presets
   whose routing can't be represented.

2. **Parallel-path routing (large, separate feature).** Genuine support for
   `P35_AppDSPSplit*` / join requires extending the spec model beyond
   `paths: 1–2 serial chains` — this is the pre-existing v1 "parallel splits not
   supported" limitation (`spec._parse_block_entry` rejects `parallel`;
   `docs/features/parallel-paths.md`). Big; likely its own spec.

3. **Instance-addressed block references.** Extend footswitch/expression/snapshot
   references (and `generate._resolve_spec_block`, and the spec's uniqueness
   validation) to disambiguate duplicate same-model instances by `(path, index)`
   or an explicit position. The decompiler would emit the disambiguator when a
   name/model_id is ambiguous. Touches `spec.py`, `generate.py`, `decompile.py`.

4. **Expression range handling.** Either widen `spec._parse_expression_target`'s
   accepted `min`/`max` bounds to match what the device actually stores, or
   normalize on decompile. Verify against real controller values before picking.

5. **IR pass-through vs. require-registration (decision).** For an orphan whose
   IR is baked into the preset but not locally registered, decide between (a)
   `generate._resolve_irhash` passing through a well-formed 32-hex hash with a
   warning (so the device — which already has the IR — keeps working), or (b)
   keeping the loud failure and instructing the user to `register-irs` first.
   Also add a `None`-guard in `_resolve_irhash` so a missing `irs` yields a clear
   error rather than `AttributeError`.

6. **Triage the misc spec-validation edges** (duplicate switch, empty block name,
   non-numeric min) case by case against the offending presets.

## Test hook

`tests/test_decompile_acceptance.py` is marked `xfail(strict=False)` and now
*measures* the gap (counts round-trippable vs. failing exports). It:
- **SKIPS** on a clean checkout (no `data/`),
- **XFAILS** on a machine with real exports (documents the gap),
- **XPASSES** once every export round-trips — the signal to remove the marker.

Use its failure list as the running scoreboard while working the items above.
