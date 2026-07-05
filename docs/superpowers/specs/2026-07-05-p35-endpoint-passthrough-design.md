# P35 branch-lane endpoint structural passthrough (design)

**Date:** 2026-07-05
**Status:** Approved — implementation pre-approved (subagent-reviewed).
**Predecessor:** `2026-07-03-decompiler-round-trip-residuals.md` (Category 2, the
sole substantial residual after the 2026-07-04 snapshot-fidelity + IR-edge pass).
**Baseline:** real-export round-trip **194/211** on the current loose metric
(`tests/test_decompile_acceptance.py`, which skips `b00`/`b13`, `xfail`).

## Problem

When a preset has a parallel split, the device materializes a second lane per DSP
path. Each lane carries its own input/output **endpoint** blocks with `P35_*`
model IDs. Main-lane endpoints (`b00` input, `b13` output) are tolerated today
(the input is driven by the `input` spec field; the output is copied from the
chassis). The **branch-lane** endpoints are not: `decompile._reconstruct_path_blocks`
calls `library.load_block` on them and raises
`KeyError: No block with model_id 'P35_OutputPath2B'` — they are routing
infrastructure, not catalogued user blocks.

15 exports fail this way; 2 more fail on unrelated one-offs. Full breakdown
(measured 2026-07-04, one shared chassis = first-ingested `2 Guitar Rig.hsp`):

| bucket | count | detail |
|---|---|---|
| P35 branch-endpoint `KeyError` | 15 | branch **inputs** `P35_InputInst1`/`InputMic`/`InputNone`; branch **outputs** `P35_OutputPath2A`/`2B`/`Matrix` |
| `US_UK Stereo` capacity guard | 1 | 10 main-lane + 3 branch-lane user blocks = 13 `BlockEntry`, on **separate lanes** — guard counts per-path not per-lane |
| `test.hsp` ambiguous IR basename | 1 | decompiled `ir` basename maps to >1 registered wav |

### Field census (all 211 `data/*.hsp`)

`type` is a perfect discriminator for endpoints and never collides with user
blocks:

- endpoints: `type in {"input","output"}` — models
  `P35_Input{None,Inst1,Inst1_2,Inst2,Mic}` (always at lane pos 0),
  `P35_Output{Matrix,Path2A,Path2B,XLR,SPDIF,QtrInch,None}` (always at lane pos 13).
- split/join: `type in {"split","join"}` (`P35_AppDSP*`) — already handled.
- loopers: `type == "looper"` (`P35_Looper*`) — **real user blocks, catalogued, must not be skipped.**

Endpoints always sit at **pos 0 (input) / pos 13 (output)** of a lane. Their slot
carries routing metadata that is *not* derivable from split topology:

```json
"b27": {"type":"output","position":13,"path":1,"endpoint":"b07","harness":{...},
        "slot":[{"model":"P35_OutputPath2B","params":{"gain":{"value":0.0},"pan":{"value":0.5}}}]}
```

The branch **output** model varies by topology (`Path2A`/`2B`/`Matrix` all observed
on branch lanes), and `endpoint`/`path` are back-pointers into the routing graph.

## Decision: structural passthrough (Option A)

Decompile records each endpoint slot **verbatim** (the whole `bNN` wire dict) as a
new structural spec entry, exactly as split/join are structural today; generate
re-emits it verbatim. Byte-faithful, zero topology reverse-engineering, correct
routing pointers for the hardware check.

**Rejected — Option B (reconstruct from topology):** the branch output model isn't
derivable, and the `endpoint`/`path`/`branch` back-pointers would need the full
routing graph reverse-engineered. Not worth it.

### Correction (2026-07-05, post-review): orphaned / cross-path split-join

Two independent spec reviews found — and the field census confirms — that **12 of
the 15 branch-endpoint presets (including `Black Keys`) carry a split/join whose
partner is an *endpoint*, not the complementary block**, frequently routing across
DSP paths. The P35 `KeyError` merely *masks* this today. Example (`Black Keys`):

- path0: `b07` split, `endpoint=b27` — but `b27` is an **output** endpoint
  (`P35_OutputPath2B`, `path`-field 1 → routes to path 2), not a join. → 1 split, 0 join.
- path1: `b01` join, `endpoint=b14` — `b14` is an **input** endpoint
  (`P35_InputNone`). → 0 split, 1 join.

Removing only the endpoint KeyError un-masks `_validate_splits` raising
`unbalanced split/join`, so the naive design lands at **199/211**, not 211, and
removing `xfail` would redden the suite. The endpoint work is necessary but not
sufficient.

**Detection rule (total across all 211, zero exceptions):** a split's `endpoint`
partner is *only ever* `type` `join` (66, balanced) or `output` (11, orphaned); a
join's is *only ever* `split` (66) or `input` (6). So classify each split/join
slot by its partner's type:

- partner is the **complementary type** (`split`↔`join`) → keep the existing
  **semantic** `SplitEntry`/`JoinEntry` (branch reconstruction, computed pointers).
  60 presets; unchanged; no test/representation risk.
- partner is an **endpoint** (`output`/`input`) → **orphaned**; capture the slot
  **verbatim** as a structural passthrough entry, identical treatment to endpoints.
  12 presets.

The 2 presets with both (`Dream On`, `Space Cadet`) keep the balanced pair and the
orphaned slot in **different paths**, so per-path (where `_validate_splits` counts)
the semantic split/join always balance — no in-path mixing. The orphaned split's
branch blocks (e.g. `Black Keys` `b15`) are already handled by the existing
"unclaimed lane-1 blocks appended with explicit lane/pos" fallback in
`_reconstruct_path_blocks`; their placement key matches the verbatim split's
`branch` pointer, so routing stays valid.

### Scoreboard bar (chosen)

A full-body `strip_provenance` compare is **0/211** and a full-`flow`-subtree
compare is **0/194** — both blocked by whole subsystems helixgen does not model
(`sources` scribble labels/colors/`fs_topidx`, `meta.info`, `preset.xyctrl`,
snapshot `valid`/`expsw`). Those are a separate, much larger effort and are
explicitly **out of scope** here.

The achievable, meaningful tightening is an **endpoint-inclusive model compare**:
compare the slot model at *every* flow `bNN` (stop skipping `b00`/`b13`). Today
that is **92/194** — the entire gap is the main output `b13`, which generate
currently copies from the shared chassis so any preset whose main output differs
mismatches. Capturing main output structurally closes it. New xpass bar:
**"every block + endpoint model round-trips to the correct slot."**

## Implementation

### Spec model (`spec.py`)
- Add `StructuralEntry` (parallel to `SplitEntry`/`JoinEntry`): fields
  `lane: int`, `pos: int`, `raw: dict` (verbatim `bNN` wire dict). It represents a
  routing-skeleton slot captured verbatim — used for **both** endpoints and
  **orphaned** split/join.
- Parse from a spec entry shaped `{"structural": {...raw bNN...}, "lane": L, "pos": P}`
  (key `"structural"` distinguishes it, mirroring `"split"`/`"join"`). Dispatch in
  `_parse_path_entry` **before** `_validate_splits`. It lives in `path.blocks`
  alongside the other entries. `_validate_splits` ignores it (counts only
  semantic `SplitEntry`/`JoinEntry`).

### Decompile (`decompile.py`)
- Add helpers: `_is_endpoint(bnn)` = `bnn.get("type") in {"input","output"}`;
  `_is_orphan_structural(path_dict, bnn)` = split/join slot whose
  `path_dict[bnn["endpoint"]].type` is **not** the complementary block type (i.e.
  partner is an `input`/`output` endpoint).
- `_reconstruct_path_blocks` / `_entry_for`: classify each `bNN` slot:
  - **main input `b00`** (lane 0 pos 0) → drives the `input` field (existing
    `_input_mode`); not emitted as a block.
  - **endpoint** (`type` input/output, any other slot) → `StructuralEntry(raw, lane, pos)`.
  - **split/join**: if balanced (partner is complementary type) → semantic
    `SplitEntry`/`JoinEntry` (existing branch reconstruction). If orphaned →
    `StructuralEntry(raw, lane, pos)`.
  - **user block** → `BlockEntry` (existing).
  - Exclude endpoints **and** orphaned split/join from the `load_block` path
    (removes the `KeyError`). Structural entries are `raw`-captured; `library`
    is never consulted for them.
  - The orphaned split's branch blocks fall through to the existing "unclaimed
    lane-1 block" append with explicit lane/pos — no special handling needed.
- Make `_iter_blocks`, `_name_index`, `_recover_snapshots`, `_recover_footswitches`,
  `_recover_expression` skip endpoint slots too (they already skip `type` split/join;
  add `input`/`output`). Orphaned split/join keep `type` split/join, so the existing
  skip already covers them.

**IR one-off (`_block_entry`):** when the resolved IR basename maps to more than
one registered wav, emit the 32-char **hash** instead of the basename (unambiguous;
mirrors `_ref` emitting coordinates only when ambiguous). `IrMapping` exposes no
multiplicity API — count over its public `entries` mapping and emit the raw
`irhash` (already registered, so generate's hash path resolves it).

### Generate (`generate.py`)
- New `_emit_structural(path_dict, path_entry)`: for each `StructuralEntry`, write
  `path_dict[f"b{14*lane+pos:02d}"] = copy.deepcopy(entry.raw)`. Key is computed
  from the entry's own `lane`/`pos` — **not** via `_assign_positions`. This
  overwrites the chassis `b13` with the correct per-preset main output, writes
  branch endpoints fresh, and re-emits orphaned split/join verbatim (routing
  pointers intact).
- Leave `_assign_positions` and `_emit_splits` iterating `path_entry.blocks`
  **unchanged**: they must still see `StructuralEntry` in `eff` (so `_emit_splits`'s
  `eff[id(e)]` lookup never `KeyError`s) and rely on every decompiled entry
  carrying an explicit `pos` (so the `next_pos` perturbation is never read). Do
  **not** add a "skip StructuralEntry" branch to `_assign_positions`.
- Dispatch in `_compose_preset_hsp`: `BlockEntry` → placement loop (unchanged);
  balanced `Split`/`Join` → `_emit_splits` (unchanged); `StructuralEntry` →
  `_emit_structural`. `b00` main input still rewritten by `_rewrite_input_endpoint`.
- **Capacity-guard one-off:** replace the per-path `len(chain) > len(_HSP_BNN_RANGE)`
  check with a **per-lane** count of `BlockEntry` from `path_entry.blocks` (main-lane
  ≤ 12 and branch-lane ≤ 12) — `chain` carries no lane info, so count over
  `path_entry.blocks` `BlockEntry.lane`. The check moves after `block_entries` is
  computed. Fixes `US_UK Stereo` (10 main + 3 branch).

### Test (`test_decompile_acceptance.py`)
- `_models`: drop the `k not in ("b00","b13")` exclusion so it compares the slot
  model at every flow `bNN`. Keep the skip-if-no-`data/` guard.
- Remove the `xfail` marker (the round-trip now xpasses on the endpoint-inclusive
  model bar). Update the scoreboard docstring/reason.
- **Scope note:** this bar compares the slot *model* at every `bNN` — it does **not**
  assert endpoint `harness`/`endpoint`/`branch`/`path` back-pointer or param
  fidelity. Verbatim capture preserves those, but only the hardware step actually
  exercises routing. State this in the docstring so the green bar isn't over-read.
- The 211 must be reached with **zero regressions** to the existing synthetic
  suite (`test_decompile*.py`, `test_spec.py`) — the 60 balanced-split presets keep
  their semantic representation, so those tests are unaffected by construction.

## Verification

- `PYTHONPATH=$PWD/src pytest` — full suite green; the acceptance test xpasses
  (211/211 on the endpoint-inclusive model bar).
- Confirm `git log main` is unchanged (all work on
  `hardening/p35-endpoint-passthrough`).
- **Hardware:** regenerate `Black Keys.hsp` (or another fixed split preset), load
  on the Stadium XL, confirm the parallel branch loads and routes correctly.

## Non-goals

- Modeling `sources` (footswitch scribble labels/colors), `meta.info`,
  `preset.xyctrl`, or snapshot `valid`/`expsw` — the full-body compare residuals.
  A future cycle.
- Reconstructing endpoints from topology.
