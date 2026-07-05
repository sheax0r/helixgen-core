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
- Add `EndpointEntry` (structural, parallel to `SplitEntry`/`JoinEntry`): fields
  `lane: int`, `pos: int`, `raw: dict` (verbatim `bNN` wire dict).
- Parse it from a spec entry shaped `{"endpoint": {...raw...}, "lane": L, "pos": P}`
  (key `"endpoint"` distinguishes it, mirroring `"split"`/`"join"`). It lives in
  `path.blocks` alongside the other structural entries.

### Decompile (`decompile.py`)
- Add `_is_endpoint(bnn) -> bool` = `bnn.get("type") in {"input","output"}`.
- `_reconstruct_path_blocks`: exclude **all** endpoint slots from `user_keys()`
  (this removes the `load_block` KeyError path). Then append one `EndpointEntry`
  per endpoint slot **except main input `b00`** (lane 0 pos 0 — already round-tripped
  by the `input` field). Captured endpoints: main output `b13`, branch input `b14`,
  branch output `b27`, and any other-lane variants. Store `lane`/`pos`/`raw`.
  Endpoints are appended after the block/split/join reconstruction; because they
  are not `BlockEntry`/`SplitEntry`/`JoinEntry`, list position does not affect
  split-region detection.
- Make `_iter_blocks`, `_name_index`, `_recover_snapshots`, `_recover_footswitches`,
  `_recover_expression` skip endpoint slots (extend the existing split/join skip).
- Main input `b00` continues to feed the `input` field via `_input_mode`.

**IR one-off (`_block_entry`):** when the resolved IR basename maps to more than
one registered wav, emit the 32-char **hash** instead of the basename (unambiguous;
mirrors `_ref` emitting coordinates only when ambiguous). Consult `IrMapping` for
the basename→wavs multiplicity.

### Generate (`generate.py`)
- New `_emit_endpoints(path_dict, path_entry)`: for each `EndpointEntry`, write
  `path_dict[f"b{14*lane+pos:02d}"] = copy.deepcopy(entry.raw)`. Key is computed
  from the entry's own `lane`/`pos` — **not** via `_assign_positions`, so it never
  perturbs the auto-position counter for real blocks. This overwrites the chassis
  `b13` with the correct per-preset main output and writes branch endpoints fresh.
- Dispatch in `_compose_preset_hsp`: `BlockEntry` → placement loop (unchanged);
  `Split`/`Join` → `_emit_splits` (unchanged); `EndpointEntry` → `_emit_endpoints`.
  `b00` main input still rewritten by `_rewrite_input_endpoint`.
- **Capacity-guard one-off:** replace the per-path `len(chain) > len(_HSP_BNN_RANGE)`
  check with a **per-lane** count — main-lane `BlockEntry` ≤ 12 and branch-lane
  `BlockEntry` ≤ 12 — using each entry's lane. Fixes `US_UK Stereo`.

### Test (`test_decompile_acceptance.py`)
- `_models`: drop the `k not in ("b00","b13")` exclusion so it compares the slot
  model at every flow `bNN`. Keep the skip-if-no-`data/` guard.
- Remove the `xfail` marker (the round-trip now xpasses on the endpoint-inclusive
  model bar). Update the scoreboard docstring/reason.

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
