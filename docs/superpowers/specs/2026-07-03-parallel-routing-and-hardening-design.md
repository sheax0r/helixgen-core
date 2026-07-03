# Parallel routing + surgical hardening

**Date:** 2026-07-03
**Status:** Design approved, pending implementation plan
**Supersedes/absorbs:** `docs/superpowers/specs/2026-07-02-decompiler-real-preset-hardening.md`
(that doc's six items are the scope here)

## Problem

The surgical-edit feature (decompiler + patch verbs + sidecar + CLI/MCP,
merged at `main` 28d7d0e) round-trips only ~65 of 211 real device exports. The
gap is dominated by v1 model limits the synthetic tests never exercised:

- **Parallel routing** — `P35_AppDSPSplit*` / `P35_AppDSPJoin` blocks in user
  slots make `decompile._block_entry` call `library.load_block` on an
  uncatalogued model and crash. ~70 presets have splits (63 with one, 7 with
  two; never more).
- **Duplicate same-model block instances** referenced by controllers — two
  `HD2_VolPanVol` "Vol" blocks (one per lane/path), each on its own footswitch,
  can't be told apart by a name-based reference.
- **Loopers** — `P35_LooperHelix*` (56 occurrences), same crash as splits.
- **Expression min/max outside `[0,1]`** — real EXP sweeps native-unit params
  (Hz/ms/cents) with values from `-120` to `1800`; `spec._parse_expression_target`
  rejects them.
- **Unregistered/unresolvable IR** — an orphan preset's `irhash` that is neither
  registered nor the block default makes `generate._resolve_irhash` raise.
- **Misc over-rejections** — empty block name, duplicate switch, non-numeric min.

The user wants the full corpus round-tripping so the surgical-edit loop can be
wired into the `tone` skill with confidence. This is one effort covering all six
threads.

## Goal

Make decompile↔generate round-trip faithful across the full real-preset corpus
(flip `tests/test_decompile_acceptance.py` from `xfail` toward `xpass`), and
integrate the trustworthy surgical-edit loop into the `tone` skill. Serial
presets that work today must continue to generate byte-identically.

## How the `.hsp` encodes a split (reverse-engineered from `A7X (2).hsp`)

Within one DSP (`preset.flow[i]`), blocks are slot-keyed `b00..b13` for the main
lane and `b14+` for the branch lane. Each block carries `path` (lane: `0`=main,
`1`=branch) and `position`. A split is three parts:

- **Split block** `P35_AppDSPSplitY` (type `split`): `branch` → first branch-lane
  slot key (e.g. `b15`), `endpoint` → the join slot key (e.g. `b08`).
- **Branch-lane blocks** — keys `b14+`, carrying `path: 1`.
- **Join block** `P35_AppDSPJoin` (type `join`): `branch` → same branch key,
  `endpoint` → the split slot key (back-pointer).

Split variants observed: `P35_AppDSPSplitY` (simple Y), `P35_AppDSPSplitAB`,
`P35_AppDSPSplitXOver` (crossover), `P35_AppDSPSplitDyn` (dynamic) — each with
its own params.

## Design

### ① Spec model — flat blocks with lanes + split/join

Each block entry gains two **optional** fields:

- `lane` — int, default `0` (main). Branch-lane blocks use `1`.
- `pos` — int, default = next free position in that lane (assigned by the
  generator in list order). Given explicitly by the decompiler for faithful
  round-trip; omitted in hand/skill-authored serial specs.

Splits are two new entry kinds within the same `blocks` list:

```json
{"split": {"type": "y", "params": {...}}, "lane": 0, "pos": 6}
{"join":  {},                              "lane": 0, "pos": 8}
```

`split.type` ∈ `{y, ab, xover, dyn}` maps to the `P35_AppDSPSplit*` model;
`params` carries the split block's parameters (e.g. crossover frequency,
balance). The generator derives the `.hsp` `branch`/`endpoint` pointers and the
`b14+` branch keys from the lane layout — **the author never writes pointers or
slot keys.**

Constraints:
- Up to **2 split regions** per DSP path (matches the data; a 3rd is refused with
  a clear error).
- A `join` must follow its `split` in the same path; branch-lane (`lane: 1`)
  blocks belong to the open split region.
- **Backward compatibility:** a spec with no `lane`/`pos`/`split`/`join`
  generates byte-identically to today. This is a hard test requirement.

Loopers (④) are ordinary catalogued blocks placed in the list like any other.

### ② Coordinate-aware block references

A placed block is identified by `(lane, pos)` within its path. Everything that
references a block — snapshots, footswitches, expression, and the surgical CLI —
accepts a coordinate; a **bare name remains valid only when it resolves to
exactly one placed block**.

Reference shape (snapshots/FS/EXP): the existing `"block": "<name>"` stays, and
gains optional disambiguator fields `"lane": L, "pos": P` (and `"path": N` when
the ref could span DSP paths) — mirroring the block-entry fields in ① for
consistency. The parser resolves name→coordinate, requiring uniqueness when no
coordinate is given. (A single canonical form; no alternate `"at": [...]`
syntax.)

- `generate._resolve_spec_block` resolves by coordinate when present, else by
  unique name; ambiguous bare name → `GenerateError` naming the candidates'
  coordinates.
- `patch.resolve_block` gains the same coordinate path; the surgical CLI adds
  `--lane` alongside the existing `--path`/`--index`.
- The decompiler emits a coordinate reference when a name is ambiguous, else the
  plain name.

This is the mechanism that makes duplicate same-model blocks (two IRs, two
"Vol"s) individually addressable.

### ③ Transform changes

**`generate` (`_compose_preset_hsp`):**
- Place lane-1 blocks into `b14+` keys with `path: 1`.
- Emit `P35_AppDSPSplit*` / `P35_AppDSPJoin` blocks with computed
  `branch`/`endpoint` pointers.
- Place looper blocks like normal blocks.
- Apply the IR pass-through policy (⑥).

**`decompile` (`decompile_body` + helpers):**
- Reconstruct lanes and split/join regions from the `branch`/`endpoint`
  pointers and per-block `path`/`position` metadata.
- Catalog loopers as blocks (④), emit `lane`/`pos` explicitly.
- Emit coordinate references (②) when a name is ambiguous.
- No longer crashes on `P35_` — split/join become routing, loopers become
  blocks, input/output remain chassis/mode as today.

### ④ Loopers → catalogued blocks

Narrow the `P35_` filter so `P35_LooperHelix*` models are ingested as placeable
library blocks (their own category, e.g. `looper`). They carry params but no
tone-shaping role; the point is faithful round-trip for the 56 occurrences.
Other `P35_` infrastructure stays filtered.

### ⑤ Expression + validation relaxations

- `spec._parse_expression_target`: accept any numeric `min`/`max` with
  `min ≤ max`; drop the `[0,1]` bound. Pass values through to the device
  unchanged.
- Triage the remaining over-rejections against the offending presets: empty
  `"block"` name, duplicate switch (two controller sources resolving to one
  `FSn`), non-numeric `min`. Fix each to accept what the device actually stores
  or to fail with an accurate message.

### ⑥ IR policy

- `generate._resolve_irhash`: when `spec_ir` is a well-formed 32-hex hash that
  is not registered, **pass it through and emit a stderr warning** (the device
  already holds that IR), rather than raising. Registered hashes and basenames
  behave as today.
- Add a `None`-guard so a missing `IrMapping` yields a clear error instead of
  `AttributeError`.

### ⑦ Skill integration

Update the `tone` skill's "Adjusting an existing tone" section to:
- address duplicate blocks by coordinate,
- surface `refuse`/`warn` outcomes gracefully (e.g. ">2 splits unsupported",
  "IR passed through — register it to edit locally").

## Error handling

Anything unrepresentable is **refused with the reason**, never silently altered:
- \> 2 split regions in a path,
- an unknown `P35_` model,
- a `join` without a matching open `split`,
- an ambiguous bare-name reference with no coordinate.

The decompiler never linearizes a parallel preset silently — if it can't
represent the routing, it refuses and says why.

## Testing

- **Round-trip stability per construct:** single split, two splits, a looper, a
  coordinate-addressed duplicate block, a native-unit EXP sweep, a pass-through
  IR — each `compose → decompile → compose` reproduces the body.
- **Backward compatibility:** a representative serial spec generates a
  byte-identical `.hsp` before/after (no `lane`/`pos` leakage).
- **Real-export acceptance test** (`test_decompile_acceptance.py`): the live
  scoreboard. Target flipping `xfail → xpass`; if a residual remains, the test
  reports the count and the categories so the gap is explicit, and the marker
  stays until zero.
- **Per-item unit tests:** coordinate resolution (unique/ambiguous), EXP range
  acceptance, IR pass-through + None-guard, looper cataloguing, split-pointer
  computation and its inverse.

## Out of scope

- More than 2 split regions per path, or arbitrary nested DAG routing (no such
  presets in the corpus).
- Any preset the device itself will not round-trip.
- Changes to the patch-verb *set* (the existing verbs gain coordinate addressing;
  no new verbs).

## Project layout impact

- `spec.py` — lane/pos/split/join parsing; coordinate references; EXP range;
  misc-edge fixes.
- `generate.py` — branch/endpoint emission, lane placement, coordinate
  resolution, IR pass-through + guard.
- `decompile.py` — lane/split/join reconstruction, coordinate emission, looper
  handling.
- `ingest.py` / `hsp.py` — looper carve-out from the `P35_` filter.
- `patch.py` / `cli.py` — coordinate addressing in verbs.
- `.claude/skills/tone/SKILL.md` — skill integration.
- Tests under `tests/` following the established round-trip + skip-guarded
  fixture patterns.
