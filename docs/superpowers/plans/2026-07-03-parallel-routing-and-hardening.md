# Parallel Routing + Surgical Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the decompile↔generate round-trip faithful across the full real-preset corpus — parallel splits, loopers, coordinate-addressed duplicate blocks, native-unit expression, and pass-through IRs — then wire the trustworthy surgical-edit loop into the `tone` skill.

**Architecture:** Extend the spec's block model with optional `lane`/`pos` fields and `split`/`join` marker entries that mirror the `.hsp` flat slot encoding (slot key `bNN` where `NN = 14×lane + position`). The generator computes the `.hsp` `branch`/`endpoint` pointers from the lane layout; the decompiler reverses them. Block references (snapshots/footswitches/expression + surgical CLI) become coordinate-aware so duplicate same-model blocks are individually addressable. Several independent validation relaxations (EXP range, loopers, IR pass-through) round out the corpus.

**Tech Stack:** Python 3 stdlib + `click` (CLI) + `mcp`/FastMCP (server). Tests: `pytest`.

## Global Constraints

- Pure stdlib + `click` only for runtime; MCP server may add `mcp`. No other runtime deps. (CLAUDE.md)
- TDD throughout: failing test first, confirm it fails, minimal implementation, confirm pass, commit. (CLAUDE.md)
- Run tests with `PYTHONPATH=$PWD/src python -m pytest` (an editable global install can shadow the bundled code). (memory: editable-install-shadows-bundled)
- **Backward compatibility is a hard requirement:** a spec with no `lane`/`pos`/`split`/`join` must generate a byte-identical `.hsp` to today. Every generate change must preserve this.
- Slot-key rule (verbatim): main lane `b00..b13` = positions 0–13; branch lane key `bNN` where `NN = 14 + position` (so branch positions 1,2,3 → `b15,b16,b17`). General rule: `NN = 14*lane + pos`.
- Split pointers (verbatim): the split block's `branch` = the *first* branch-lane slot key of its region; the join block's `branch` = the *last* branch-lane slot key; `split.endpoint` = join key, `join.endpoint` = split key. A branch-lane block belongs to the split region whose `[split_pos, join_pos]` position span contains its position.
- Split variants observed: `P35_AppDSPSplitY`, `P35_AppDSPSplitAB`, `P35_AppDSPSplitXOver`, `P35_AppDSPSplitDyn`; join is always `P35_AppDSPJoin`. Split/join blocks are NOT in the library — their `model` + `params` are carried inline in the spec.
- Max 2 split regions per DSP path; a 3rd is refused with a clear error.
- `.hsp` model IDs use the Stadium namespace; translate with `hsp.translate_to_hsp` (write) / `hsp._translate_model_id` (read). Param values coerce to the schema type via `generate._coerce_param_value`.
- Real-export integration tests are skip-gated on `data/*.hsp` presence. `tests/test_decompile_acceptance.py` is the live scoreboard and is currently `xfail(strict=False)`.

---

## File / responsibility map

- `src/helixgen/spec.py` — `lane`/`pos` on `BlockEntry`; new `SplitEntry`/`JoinEntry` parsing; path-level split validation; coordinate fields on snapshot/FS/EXP references; EXP range relaxation; FS-multiplicity relaxation.
- `src/helixgen/generate.py` — slot-key-from-(lane,pos) placement (backward-compatible for serial); split/join emission with pointer computation; coordinate-aware `_resolve_spec_block`; IR pass-through + None-guard.
- `src/helixgen/decompile.py` — lane/split/join reconstruction; coordinate reference emission; looper handling; explicit `lane`/`pos` output.
- `src/helixgen/hsp.py` — carve loopers out of the `CHASSIS_MODEL_PREFIX` filter.
- `src/helixgen/ingest.py` — `looper` category for `P35_LooperHelix*`.
- `src/helixgen/patch.py` / `src/helixgen/cli.py` — coordinate addressing in verbs.
- `.claude/skills/tone/SKILL.md` — skill integration.
- Tests under `tests/`.

---

# Phase 1 — Independent relaxations

## Task 1: Expression min/max accepts any numeric

**Files:**
- Modify: `src/helixgen/spec.py` — `_parse_expression_target` (around lines 252-270)
- Test: `tests/test_spec_expression.py`

**Interfaces:**
- Produces: `ExpressionTarget(min: float, max: float)` now accepts any float with `min ≤ max` (no `[0,1]` bound).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec_expression.py (append)
from helixgen.spec import parse_spec


def test_expression_accepts_native_unit_range():
    spec = parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "X", "param": "Time", "min": -120.0, "max": 1800.0}]}]})
    t = spec.expression[0].targets[0]
    assert t.min == -120.0 and t.max == 1800.0


def test_expression_still_requires_min_le_max():
    import pytest
    from helixgen.spec import SpecError
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}],
            "expression": [{"pedal": "EXP1", "targets": [
                {"block": "X", "param": "Time", "min": 5.0, "max": 1.0}]}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_spec_expression.py -q -k native_unit`
Expected: FAIL — current code raises on `max > 1.0`.

- [ ] **Step 3: Implement — drop the [0,1] bound**

Replace the validation loop in `_parse_expression_target` (the block that currently checks `val < 0.0 or val > 1.0`):

```python
    mn = data.get("min", 0.0)
    mx = data.get("max", 1.0)
    for label, val in (("min", mn), ("max", mx)):
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise _err(source, f'"{label}" must be a number.')
    if mn > mx:
        raise _err(source, f'"min" must be <= "max" (got min={mn}, max={mx}).')
    return ExpressionTarget(block=block, param=param, min=float(mn), max=float(mx))
```

(Remove only the `if val < 0.0 or val > 1.0` check; keep the type check and the `min <= max` check.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_spec_expression.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec_expression.py
git commit -m "feat(spec): expression min/max accepts any numeric (native-unit sweeps)"
```

---

## Task 2: IR hash pass-through + None-guard

**Files:**
- Modify: `src/helixgen/generate.py` — `_resolve_irhash` (lines 38-62)
- Test: `tests/test_ir_generate.py`

**Interfaces:**
- Consumes: `_HASH_RE` (module-level, matches 32-hex).
- Produces: `_resolve_irhash(block_default, spec_ir, irs)` — a well-formed 32-hex `spec_ir` that is not registered is returned as-is with a stderr warning; a `None` `irs` with a hash `spec_ir` still returns the hash (no crash); an unregistered basename still raises `GenerateError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ir_generate.py (append)
import re
from helixgen.generate import _resolve_irhash
from helixgen.ir import IrMapping


def test_resolve_irhash_passthrough_unregistered_hash(capsys):
    h = "deadbeef" * 4  # 32 hex chars, not registered
    irs = IrMapping(irs_dir=__import__("pathlib").Path("/tmp"), entries={})
    out = _resolve_irhash(block_default=None, spec_ir=h, irs=irs)
    assert out == h
    assert "warning" in capsys.readouterr().err.lower()


def test_resolve_irhash_none_irs_with_hash_ok():
    h = "deadbeef" * 4
    out = _resolve_irhash(block_default=None, spec_ir=h, irs=None)
    assert out == h
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_ir_generate.py -q -k passthrough`
Expected: FAIL — current code calls `irs.resolve_by_hash(...)` which raises `IrMappingError` for the unregistered hash (and `AttributeError` when `irs is None`).

- [ ] **Step 3: Implement pass-through + guard**

Replace the `if spec_ir is not None:` branch of `_resolve_irhash`:

```python
    import sys
    from helixgen.ir import IrMappingError  # local import to avoid cycle

    if spec_ir is not None:
        if _HASH_RE.fullmatch(spec_ir):
            h = spec_ir.lower()
            if irs is not None:
                try:
                    irs.resolve_by_hash(h)
                except IrMappingError:
                    print(
                        f"warning: IR hash {h} is not registered; passing it "
                        f"through unchanged (the device must already hold this IR).",
                        file=sys.stderr,
                    )
            return h
        # basename form still requires a registered mapping
        if irs is None:
            raise GenerateError(
                f"cannot resolve IR basename {spec_ir!r}: no IR mapping available"
            )
        try:
            h, _ = irs.resolve_by_basename(spec_ir)
        except IrMappingError as e:
            raise GenerateError(str(e)) from e
        return h
    if block_default is not None:
        return block_default
    raise GenerateError(
        "IR block requires an `ir` field (no canonical irhash available); "
        "see `helixgen list-irs`"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_ir_generate.py -q`
Expected: PASS

- [ ] **Step 5: Run full suite (no regressions to existing IR tests)**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_ir_generate.py
git commit -m "feat(generate): pass through well-formed unregistered IR hash + None-guard"
```

---

## Task 3: Loopers catalogued as placeable blocks

**Files:**
- Modify: `src/helixgen/hsp.py` — `extract_blocks_from_hsp` P35 filter (line ~164)
- Modify: `src/helixgen/ingest.py` — add `("P35_LooperHelix", "looper")` to `_CATEGORY_PREFIXES`
- Test: `tests/test_ir_ingest.py` (or `tests/test_ingest.py`)

**Interfaces:**
- Produces: `P35_LooperHelix*` models are ingested as blocks with category `"looper"` (not filtered).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest.py (append)
import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path, infer_category
from helixgen.library import Library


def test_looper_is_catalogued(tmp_path, sample_serial_preset_hsp):
    # inject a looper block into path 0
    body = json.loads(json.dumps(sample_serial_preset_hsp))
    body["preset"]["flow"][0]["b01"] = {
        "type": "fx", "position": 1, "path": 0,
        "slot": [{"model": "P35_LooperHelixStereo",
                  "@enabled": {"value": True}, "params": {}}],
    }
    p = tmp_path / "loop.hsp"
    p.write_bytes(HSP_MAGIC + json.dumps(body).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(p, lib)
    models = [b.model_id for b in lib.list_blocks()]
    assert "P35_LooperHelixStereo" in models
    assert infer_category("P35_LooperHelixStereo") == "looper"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_ingest.py -q -k looper`
Expected: FAIL — the looper is filtered by `CHASSIS_MODEL_PREFIX` and never catalogued.

- [ ] **Step 3: Implement — carve loopers out of the P35 filter + add category**

In `src/helixgen/hsp.py`, change the filter in `extract_blocks_from_hsp` (the `if slot["model"].startswith(CHASSIS_MODEL_PREFIX): continue` line) to keep loopers:

```python
                model = slot["model"]
                if model.startswith(CHASSIS_MODEL_PREFIX) and not model.startswith("P35_LooperHelix"):
                    continue
```

In `src/helixgen/ingest.py`, add to `_CATEGORY_PREFIXES` (before the final `]`):

```python
    ("P35_LooperHelix", "looper"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_ingest.py -q -k looper`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS (existing ingest tests unaffected — only loopers now pass the filter)

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/hsp.py src/helixgen/ingest.py tests/test_ingest.py
git commit -m "feat(ingest): catalogue P35_LooperHelix* as 'looper' blocks"
```

---

## Task 4: Footswitch multiplicity + validation-edge relaxations

**Files:**
- Modify: `src/helixgen/spec.py` — `_parse_footswitches` (lines 162-184): allow one switch to drive multiple blocks
- Test: `tests/test_spec_footswitches.py`

**Interfaces:**
- Produces: a footswitch `switch` may appear on multiple assignments (one switch → many blocks); a block still appears on at most one switch.

**Context:** Real presets assign one physical footswitch to several blocks at once (a "scene" stomp). The current parser rejects a duplicate `switch`. Relax that: duplicate switches are allowed; only duplicate *blocks* remain rejected.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec_footswitches.py (append)
from helixgen.spec import parse_spec
import pytest
from helixgen.spec import SpecError


def test_one_switch_may_drive_multiple_blocks():
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "A"}, {"block": "B"}]}],
        "footswitches": [
            {"switch": "FS1", "block": "A"},
            {"switch": "FS1", "block": "B"}]})
    assert [f.block for f in spec.footswitches] == ["A", "B"]
    assert all(f.switch == "FS1" for f in spec.footswitches)


def test_block_still_limited_to_one_switch():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [{"block": "A"}]}],
            "footswitches": [
                {"switch": "FS1", "block": "A"},
                {"switch": "FS2", "block": "A"}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_spec_footswitches.py -q -k multiple_blocks`
Expected: FAIL — parser raises "duplicate switch 'FS1'".

- [ ] **Step 3: Implement — drop the switch-uniqueness check, keep block-uniqueness**

In `_parse_footswitches`, remove the `seen_switches` check and its set; keep `seen_blocks`:

```python
def _parse_footswitches(raw: Any, *, source: str) -> list[FootswitchAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"footswitches" must be a list.')
    out: list[FootswitchAssignment] = []
    seen_blocks: set[str] = set()
    for i, entry in enumerate(raw):
        fs = _parse_footswitch(entry, source=f"{source} footswitches[{i}]")
        if fs.block in seen_blocks:
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate block {fs.block!r}; one block per footswitch.",
            )
        seen_blocks.add(fs.block)
        out.append(fs)
    return out
```

Note: block-level uniqueness by *name* will be revisited in Task 9 (coordinate refs) so duplicate same-name blocks on different switches disambiguate by coordinate. For now, name-level block uniqueness stays.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_spec_footswitches.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec_footswitches.py
git commit -m "feat(spec): allow one footswitch to drive multiple blocks"
```

---

# Phase 2 — Flat lane/pos model + split/join

## Task 5: Spec parsing for lane/pos + split/join entries

**Files:**
- Modify: `src/helixgen/spec.py` — `BlockEntry` (line 13); new `SplitEntry`/`JoinEntry` dataclasses; `PathEntry` to hold a heterogeneous entry list; `_parse_block_entry` / `_parse_path`
- Test: `tests/test_spec.py`

**Interfaces:**
- Produces:
  - `BlockEntry` gains `lane: int = 0`, `pos: int | None = None`.
  - `SplitEntry(model: str, params: dict, lane: int = 0, pos: int | None = None)` — parsed from `{"split": {"model": "...", "params": {...}}, "lane": L, "pos": P}`.
  - `JoinEntry(model: str = "P35_AppDSPJoin", params: dict, lane: int = 0, pos: int | None = None)` — parsed from `{"join": {...}}`.
  - `PathEntry.blocks: list[BlockEntry | SplitEntry | JoinEntry]` in list order.
  - Validation: at most 2 `SplitEntry` per path; each `SplitEntry` must have a following `JoinEntry`; `lane ∈ {0, 1}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec.py (append)
from helixgen.spec import parse_spec, SplitEntry, JoinEntry, BlockEntry, SpecError
import pytest


def test_parse_lane_pos_on_block():
    s = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Pitch", "lane": 1, "pos": 1}]}]})
    b = s.paths[0].blocks[0]
    assert isinstance(b, BlockEntry) and b.lane == 1 and b.pos == 1


def test_parse_split_join_entries():
    s = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Amp"},
        {"split": {"model": "P35_AppDSPSplitY", "params": {}}, "lane": 0, "pos": 6},
        {"block": "Pitch", "lane": 1, "pos": 1},
        {"join": {}, "lane": 0, "pos": 8},
        {"block": "Reverb"}]}]})
    kinds = [type(b).__name__ for b in s.paths[0].blocks]
    assert kinds == ["BlockEntry", "SplitEntry", "BlockEntry", "JoinEntry", "BlockEntry"]
    assert s.paths[0].blocks[1].model == "P35_AppDSPSplitY"


def test_reject_three_splits():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}}, {"join": {}},
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}}, {"join": {}},
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}}, {"join": {}}]}]})


def test_reject_split_without_join():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}},
            {"block": "X", "lane": 1}]}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_spec.py -q -k "lane_pos or split"`
Expected: FAIL — `SplitEntry`/`JoinEntry` don't exist; `lane`/`pos` not parsed.

- [ ] **Step 3: Implement the dataclasses + parsing**

Add to `spec.py` near the other dataclasses:

```python
@dataclass
class SplitEntry:
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    lane: int = 0
    pos: int | None = None


@dataclass
class JoinEntry:
    model: str = "P35_AppDSPJoin"
    params: dict[str, Any] = field(default_factory=dict)
    lane: int = 0
    pos: int | None = None
```

Extend `BlockEntry` (keep existing fields; add):

```python
@dataclass
class BlockEntry:
    block: str
    params: dict[str, Any] = field(default_factory=dict)
    ir: str | None = None
    enabled: bool | None = None
    lane: int = 0
    pos: int | None = None
```

Add a helper to parse lane/pos (used by all three entry kinds):

```python
def _parse_lane_pos(data: dict, *, source: str) -> tuple[int, int | None]:
    lane = data.get("lane", 0)
    if lane not in (0, 1):
        raise _err(source, f'"lane" must be 0 or 1 (got {lane!r}).')
    pos = data.get("pos")
    if pos is not None and (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0):
        raise _err(source, f'"pos" must be a non-negative integer if provided (got {pos!r}).')
    return lane, pos
```

In `_parse_block_entry`, dispatch on `split`/`join` keys and thread lane/pos. Replace the body so it handles the three entry kinds:

```python
def _parse_path_entry(data: Any, *, source: str):
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    if "split" in data:
        sd = data["split"]
        if not isinstance(sd, dict) or not isinstance(sd.get("model"), str):
            raise _err(source, '"split" must be an object with a "model" string.')
        lane, pos = _parse_lane_pos(data, source=source)
        return SplitEntry(model=sd["model"], params=dict(sd.get("params", {})), lane=lane, pos=pos)
    if "join" in data:
        jd = data["join"] or {}
        lane, pos = _parse_lane_pos(data, source=source)
        return JoinEntry(model=jd.get("model", "P35_AppDSPJoin"),
                         params=dict(jd.get("params", {})), lane=lane, pos=pos)
    # plain block (existing logic) + lane/pos
    if "parallel" in data:
        raise _err(source, '"parallel" entries not supported; use split/join.')
    name = data.get("block")
    if not isinstance(name, str) or not name:
        raise _err(source, '"block" is required and must be a non-empty string.')
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise _err(source, '"params" must be an object if provided.')
    ir = data.get("ir")
    if ir is not None and not isinstance(ir, str):
        raise _err(source, '"ir" must be a string if provided.')
    enabled = data.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise _err(source, '"enabled" must be a boolean if provided.')
    lane, pos = _parse_lane_pos(data, source=source)
    return BlockEntry(block=name, params=dict(params), ir=ir, enabled=enabled, lane=lane, pos=pos)
```

In `_parse_path`, call `_parse_path_entry` for each raw block, then validate split structure:

```python
    blocks = [_parse_path_entry(b, source=f"{source} blocks[{i}]")
              for i, b in enumerate(blocks_raw)]
    _validate_splits(blocks, source=source)
    return PathEntry(blocks=blocks, input=inp, output=out)


def _validate_splits(entries: list, *, source: str) -> None:
    n_split = sum(1 for e in entries if isinstance(e, SplitEntry))
    n_join = sum(1 for e in entries if isinstance(e, JoinEntry))
    if n_split > 2:
        raise _err(source, f"at most 2 split regions per path (got {n_split}).")
    if n_split != n_join:
        raise _err(source, f"unbalanced split/join ({n_split} split, {n_join} join).")
    # each split must precede a join in list order
    depth = 0
    for e in entries:
        if isinstance(e, SplitEntry):
            depth += 1
        elif isinstance(e, JoinEntry):
            depth -= 1
            if depth < 0:
                raise _err(source, "join without a matching open split.")
    if depth != 0:
        raise _err(source, "split without a matching join.")
```

Keep `PathEntry.blocks` typed loosely (`list`); other code checks `isinstance`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_spec.py -q`
Expected: PASS

- [ ] **Step 5: Run full suite (existing serial specs unaffected)**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec.py
git commit -m "feat(spec): lane/pos fields + split/join entries with validation"
```

---

## Task 6: Generate — slot key from (lane, pos), backward-compatible

**Files:**
- Modify: `src/helixgen/generate.py` — `_compose_preset_hsp` block-placement loop (lines ~633-679) and `_to_hsp_bnn` `position`
- Test: `tests/test_generate.py`

**Interfaces:**
- Consumes: `BlockEntry.lane`, `BlockEntry.pos` (Task 5).
- Produces: each placed block's slot key is `f"b{14*lane + pos:02d}"`; for a serial spec (all `lane=0`, `pos=None`) positions auto-assign to `1,2,3,…`, yielding byte-identical output to today. `bNN` `position` field = the resolved pos; branch blocks (`lane=1`) get `path: 1`.

**Context:** This task handles ONLY plain `BlockEntry` placement with lanes/positions. Split/Join emission is Task 7. This task must keep serial output byte-identical (the backbone constraint).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_generate.py (append; uses hsp_library fixture from conftest)
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def test_serial_output_unchanged_with_lane_pos_absent(hsp_library):
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Brit Amp"}]}]})
    preset = compose_preset(spec, hsp_library, source="t")
    keys = [k for k in preset["preset"]["flow"][0] if k.startswith("b") and k[1:].isdigit()]
    assert "b01" in keys and "b02" in keys  # serial keys unchanged


def test_branch_lane_block_uses_b15(hsp_library):
    # a lane-1 block at pos 1 must land at slot b15 with path:1
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Tube Drive"},
        {"block": "Brit Amp", "lane": 1, "pos": 1}]}]})
    preset = compose_preset(spec, hsp_library, source="t")
    path0 = preset["preset"]["flow"][0]
    assert "b15" in path0
    assert path0["b15"]["path"] == 1
    assert path0["b15"]["slot"][0]["model"] == "HD2_AmpBrit"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_generate.py -q -k "b15 or serial_output_unchanged"`
Expected: FAIL — placement uses `chain_idx + 1` for the key and always `path: path_index`, so `b15`/`path:1` never appear.

- [ ] **Step 3: Implement (lane, pos) placement**

In `_compose_preset_hsp`, the block-placement loop currently computes `slot_index = chain_idx + 1` and `key = f"b{slot_index:02d}"`. Replace the position/key computation to honor lane/pos with per-lane auto-assignment. Before the loop, initialize per-lane counters; inside, resolve pos:

```python
        path_entry = spec.paths[path_index]
        next_pos = {0: 1, 1: 1}  # auto-assign counter per lane
        for chain_idx, (block, user_params) in enumerate(chain):
            block_entry = path_entry.blocks[chain_idx]
            lane = getattr(block_entry, "lane", 0)
            pos = block_entry.pos if block_entry.pos is not None else next_pos[lane]
            next_pos[lane] = max(next_pos[lane], pos + 1)
            slot_index = 14 * lane + pos
            key = f"b{slot_index:02d}"
            resolved_irhash = None
            if block.model_id.startswith(IR_MODEL_PREFIX):
                resolved_irhash = _resolve_irhash(
                    block_default=block.default_irhash, spec_ir=block_entry.ir, irs=irs)
            path_dict[key] = _to_hsp_bnn(
                block, user_params,
                position=pos,
                path_index=lane,
                enabled_base=block_entry.enabled,
                enabled_overrides=enabled_map.get((path_index, chain_idx)),
                param_overrides=param_map.get((path_index, chain_idx)),
                fs_controller=fs_map.get((path_index, chain_idx)),
                exp_controllers={pname: ctrl
                    for (pi, ci, pname), ctrl in exp_map.items()
                    if pi == path_index and ci == chain_idx} or None,
                irhash=resolved_irhash,
            )
```

Note the two changes vs. today: `position=pos` (was `slot_index`) and `path_index=lane` (was `path_index`) — for serial specs `lane=0` and `pos=chain_idx+1`, so `position` and the `path` field are identical to before, and `key = f"b{pos:02d}"` matches the old `b{chain_idx+1:02d}`. (`resolve_blocks` still returns a chain per `BlockEntry`; split/join entries are handled in Task 7 and must be skipped here — see Task 7.)

**Important for this task:** since Task 7 introduces split/join entries into `path_entry.blocks`, and `resolve_blocks` currently maps every entry to a library block, guard `resolve_blocks` to skip non-`BlockEntry` entries now so this task's zip stays aligned:

In `resolve_blocks` (generate.py ~76-85), skip split/join:

```python
def resolve_blocks(spec, library):
    from helixgen.spec import BlockEntry
    resolved = []
    for path in spec.paths:
        chain = []
        for entry in path.blocks:
            if not isinstance(entry, BlockEntry):
                continue
            block = library.find_block(entry.block)
            chain.append((block, entry.params))
        resolved.append(chain)
    return resolved
```

And in the placement loop, iterate the BlockEntry subset in lockstep. Replace `block_entry = path_entry.blocks[chain_idx]` with a pre-filtered list:

```python
        block_entries = [e for e in path_entry.blocks if isinstance(e, BlockEntry)]
        ...
        for chain_idx, (block, user_params) in enumerate(chain):
            block_entry = block_entries[chain_idx]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_generate.py -q -k "b15 or serial_output_unchanged"`
Expected: PASS

- [ ] **Step 5: Backward-compat — full suite + a byte-identity check**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS — all existing generate/roundtrip tests still green (proves serial output unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat(generate): place blocks by (lane,pos) slot key; serial output unchanged"
```

---

## Task 7: Generate — emit split/join with branch/endpoint pointers

**Files:**
- Modify: `src/helixgen/generate.py` — `_compose_preset_hsp` (add split/join emission after block placement)
- Test: `tests/test_generate.py`

**Interfaces:**
- Consumes: `SplitEntry`, `JoinEntry` (Task 5); the `(lane,pos)`→key rule.
- Produces: for each `SplitEntry`/`JoinEntry` in a path, a `bNN` slot at `14*lane+pos` with the inline `model`/`params`, `type: "split"|"join"`, and computed `branch`/`endpoint`:
  - `split.branch` = key of the first `lane==1` block whose pos is in `(split_pos, join_pos)`; `split.endpoint` = join key.
  - `join.branch` = key of the last such branch block; `join.endpoint` = split key.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generate.py (append)
def test_split_join_pointers(hsp_library):
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 5},
        {"split": {"model": "P35_AppDSPSplitY", "params": {}}, "lane": 0, "pos": 6},
        {"block": "Brit Amp", "lane": 1, "pos": 1},   # branch block → b15
        {"join": {}, "lane": 0, "pos": 8}]}]})
    path0 = compose_preset(spec, hsp_library, source="t")["preset"]["flow"][0]
    assert path0["b06"]["type"] == "split"
    assert path0["b06"]["branch"] == "b15"
    assert path0["b06"]["endpoint"] == "b08"
    assert path0["b08"]["type"] == "join"
    assert path0["b08"]["branch"] == "b15"   # single branch block → first == last
    assert path0["b08"]["endpoint"] == "b06"
    assert path0["b06"]["slot"][0]["model"] == "P35_AppDSPSplitY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_generate.py -q -k split_join_pointers`
Expected: FAIL — split/join entries are skipped (not emitted) after Task 6.

- [ ] **Step 3: Implement split/join emission**

Add a helper and call it after the block-placement loop, per path. Helper:

**Region membership is by LIST ORDER, not position arithmetic.** Branch-lane
positions restart at 1 and are independent of main-lane positions (a branch
block at lane-1 pos 1 can sit under a split at main pos 6), so a branch block
belongs to whichever split it is *listed between* in `path_entry.blocks`. Collect
the lane-1 block keys that appear between a `SplitEntry` and its matching
`JoinEntry` in list order; `split.branch` = first such key, `join.branch` = last
(fallbacks: join key / split key when the branch is empty).

```python
def _emit_splits(path_dict, path_entry) -> None:
    """Emit split/join bNN slots with computed branch/endpoint pointers.

    Plain BlockEntry blocks are already placed by the caller. Effective
    positions are recomputed the same way the placement loop does so keys match.
    Region (branch-block) membership is determined by LIST ORDER: the lane-1
    blocks listed between a split and its join belong to that split.
    """
    from helixgen.spec import SplitEntry, JoinEntry, BlockEntry

    next_pos = {0: 1, 1: 1}
    eff = []  # (entry, lane, pos, key) in list order
    for e in path_entry.blocks:
        lane = getattr(e, "lane", 0)
        pos = e.pos if e.pos is not None else next_pos[lane]
        next_pos[lane] = max(next_pos[lane], pos + 1)
        eff.append((e, lane, pos, f"b{14 * lane + pos:02d}"))

    # Sequential (non-nested) split regions: collect lane-1 keys between each
    # split and its join, in list order.
    regions = []            # (s_entry, s_pos, s_key, j_entry, j_pos, j_key, [branch_keys])
    open_split = None       # (s_entry, s_pos, s_key)
    branch_keys: list[str] = []
    for (e, lane, pos, key) in eff:
        if isinstance(e, SplitEntry):
            open_split = (e, pos, key)
            branch_keys = []
        elif isinstance(e, JoinEntry):
            se, sp, sk = open_split
            regions.append((se, sp, sk, e, pos, key, branch_keys))
            open_split = None
            branch_keys = []
        elif lane == 1 and open_split is not None and isinstance(e, BlockEntry):
            branch_keys.append(key)

    for (se, sp, sk, je, jp, jk, bkeys) in regions:
        first_b = bkeys[0] if bkeys else jk
        last_b = bkeys[-1] if bkeys else sk
        path_dict[sk] = {
            "@enabled": {"value": True},
            "type": "split", "position": sp, "path": 0,
            "branch": first_b, "endpoint": jk,
            "slot": [{"model": se.model, "@enabled": {"value": True},
                      "params": {k: {"value": v} for k, v in se.params.items()}}],
        }
        path_dict[jk] = {
            "@enabled": {"value": True},
            "type": "join", "position": jp, "path": 0,
            "branch": last_b, "endpoint": sk,
            "slot": [{"model": je.model, "@enabled": {"value": True},
                      "params": {k: {"value": v} for k, v in je.params.items()}}],
        }
```

Call it in `_compose_preset_hsp` after the per-path block-placement loop, inside the same `for path_index, chain in enumerate(resolved):` structure — but it needs the full `path_entry.blocks`, so call after placement using `spec.paths[path_index]`:

```python
        _emit_splits(path_dict, spec.paths[path_index])
```

(Place this right after the block-placement `for chain_idx …` loop for that path.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_generate.py -q -k split_join_pointers`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat(generate): emit split/join blocks with branch/endpoint pointers"
```

---

## Task 8: Decompile — reconstruct lanes + split/join

**Files:**
- Modify: `src/helixgen/decompile.py` — `decompile_body` block iteration + new `_reconstruct_path_blocks`
- Test: `tests/test_decompile_advanced.py`

**Interfaces:**
- Consumes: the `.hsp` split encoding (`type`, `branch`, `endpoint`, per-block `path`/`position`); the `(lane,pos)` rule.
- Produces: `decompile_body` emits, per path, a flat `blocks` list containing `BlockEntry`-shaped dicts (with explicit `lane`/`pos`) and `{"split": {...}}` / `{"join": {}}` entries, ordered by main-lane position with branch blocks interleaved at their split region. Round-trip stable: `compose → decompile → compose` reproduces the split preset.

- [ ] **Step 1: Write the failing round-trip test**

```python
# tests/test_decompile_advanced.py (append)
from helixgen.decompile import decompile_body


def test_split_roundtrip_stable(hsp_library, strip_provenance):
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 5},
        {"split": {"model": "P35_AppDSPSplitY", "params": {}}, "lane": 0, "pos": 6},
        {"block": "Brit Amp", "lane": 1, "pos": 1},
        {"join": {}, "lane": 0, "pos": 8}]}]}
    from helixgen.generate import compose_preset
    from helixgen.spec import parse_spec
    p1 = compose_preset(parse_spec(spec), hsp_library, source="t")
    spec2 = parse_spec(decompile_body(p1, hsp_library))
    p2 = compose_preset(spec2, hsp_library, source="t")
    assert strip_provenance(p1) == strip_provenance(p2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_decompile_advanced.py -q -k split_roundtrip`
Expected: FAIL — decompile currently ignores split/join and lane metadata; recompose won't match.

- [ ] **Step 3: Implement reconstruction**

Replace the per-path block loop in `decompile_body` with a call to `_reconstruct_path_blocks`, which walks all `bNN` (both lanes), emits split/join and lane/pos:

**List order matters**: `_emit_splits` (Task 7) determines a branch block's
region by the lane-1 blocks listed *between* a split and its join. So decompile
must emit each region's branch blocks immediately after their split entry, not
sorted to the end by slot number. Reconstruct region membership from the `.hsp`
pointers: a split's branch blocks are the lane-1 slots whose key falls in
`[split.branch … join.branch]` (inclusive, by slot number).

```python
def _entry_for(key, bnn, library, irs):
    """Build a spec entry dict (block/split/join) with explicit lane/pos."""
    num = int(key[1:])
    lane = 1 if num >= 14 else 0
    pos = num - 14 * lane
    typ = bnn.get("type")
    slot = bnn["slot"][0]
    if typ == "split":
        entry = {"split": {"model": slot.get("model"),
                           "params": {k: _unwrap_value(v) for k, v in (slot.get("params") or {}).items()}}}
    elif typ == "join":
        entry = {"join": {"model": slot.get("model"),
                          "params": {k: _unwrap_value(v) for k, v in (slot.get("params") or {}).items()}}}
    else:
        entry = _block_entry(slot, library, irs)
    entry["lane"] = lane
    entry["pos"] = pos
    return entry


def _reconstruct_path_blocks(path_dict, library, irs):
    """Ordered spec `blocks` list for one .hsp path: lane-0 blocks in position
    order, with each split's branch (lane-1) blocks inserted right after the
    split entry so region membership survives the round-trip."""
    def user_keys():
        return [k for k in path_dict
                if isinstance(k, str) and k.startswith("b") and k[1:].isdigit()
                and k not in _ENDPOINT_KEYS
                and isinstance(path_dict[k], dict) and path_dict[k].get("slot")]

    keys = user_keys()
    lane0 = sorted((k for k in keys if int(k[1:]) < 14), key=lambda k: int(k[1:]))
    lane1 = sorted((k for k in keys if int(k[1:]) >= 14), key=lambda k: int(k[1:]))

    # region branch keys for each split: [split.branch .. join.branch] by number
    def branch_span(bnn):
        b0, b1 = bnn.get("branch"), path_dict.get(bnn.get("endpoint"), {}).get("branch")
        if not b0 or not b1:
            return []
        lo, hi = sorted((int(b0[1:]), int(b1[1:])))
        return [k for k in lane1 if lo <= int(k[1:]) <= hi]

    out = []
    for k in lane0:
        bnn = path_dict[k]
        out.append(_entry_for(k, bnn, library, irs))
        if bnn.get("type") == "split":
            for bk in branch_span(bnn):
                out.append(_entry_for(bk, path_dict[bk], library, irs))
    # any lane-1 blocks not claimed by a split (shouldn't happen for valid
    # presets) are appended so nothing is silently dropped
    claimed = {e_key for k in lane0 if path_dict[k].get("type") == "split"
               for e_key in branch_span(path_dict[k])}
    for bk in lane1:
        if bk not in claimed:
            out.append(_entry_for(bk, path_dict[bk], library, irs))
    return out
```

In `decompile_body`, replace the inner block loop:

```python
    for path_dict in flow:
        if not isinstance(path_dict, dict):
            continue
        blocks = _reconstruct_path_blocks(path_dict, library, irs)
        path_entry = {"blocks": blocks}
        mode = _input_mode(path_dict, device_id)
        if mode is not None:
            path_entry["input"] = mode
        paths.append(path_entry)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_decompile_advanced.py -q -k split_roundtrip`
Expected: PASS

- [ ] **Step 5: Run full suite (existing decompile tests unaffected)**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile_advanced.py
git commit -m "feat(decompile): reconstruct lanes + split/join from .hsp pointers"
```

---

# Phase 3 — Coordinate-aware references

## Task 9: Coordinate resolution in generate + spec references

**Files:**
- Modify: `src/helixgen/generate.py` — `_resolve_spec_block` (lines 476-491) to accept coordinates
- Modify: `src/helixgen/spec.py` — snapshot/FS/EXP reference parsing to carry optional `lane`/`pos`/`path`
- Test: `tests/test_generate_footswitches.py`, `tests/test_spec_footswitches.py`

**Interfaces:**
- Consumes: `BlockEntry.lane`, `.pos`; `resolve_blocks` chain.
- Produces: `_resolve_spec_block(name, resolved, *, path=None, lane=None, pos=None)` resolves by coordinate when given; a bare name resolves only if it maps to exactly one placed block, else `GenerateError` naming the candidate coordinates. Snapshot/FS/EXP reference dicts accept optional `lane`/`pos`/`path` keys, threaded to the resolver.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generate_footswitches.py (append; needs a lib with 2 same-name blocks)
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path


def _dup_ir_lib(tmp_path, sample_serial_preset_hsp):
    chassis = tmp_path / "c.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(model_id="HX2_ImpulseResponseWithPan", category="cab",
        display_name="With Pan", params={"Mix": {"type": "float"}},
        exemplar={"@model": "HX2_ImpulseResponseWithPan", "@type": "cab", "@enabled": True, "Mix": 1.0},
        first_seen={"preset": "_", "firmware": "_", "date": "x"}, default_irhash="a"*32))
    return lib


def test_footswitch_targets_duplicate_block_by_coordinate(tmp_path, sample_serial_preset_hsp):
    lib = _dup_ir_lib(tmp_path, sample_serial_preset_hsp)
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 1},
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 2}]}],
        "footswitches": [{"switch": "FS1", "block": "With Pan", "pos": 2}]})
    preset = compose_preset(spec, lib, source="t")
    # the FS controller must be attached to the pos-2 slot (b02), not b01
    assert "controller" in preset["preset"]["flow"][0]["b02"]["@enabled"]
    assert "controller" not in preset["preset"]["flow"][0]["b01"]["@enabled"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_generate_footswitches.py -q -k duplicate_block_by_coordinate`
Expected: FAIL — `_resolve_spec_block` raises "matches multiple placed blocks" and FS parsing ignores `pos`.

- [ ] **Step 3: Implement coordinate resolution + ref parsing**

Rewrite `_resolve_spec_block` to accept coordinates and index the resolved chain by `(lane, pos)` (recomputing effective positions the same way generation does):

```python
def _resolve_spec_block(name, resolved, *, spec=None, path=None, lane=None, pos=None):
    """Locate a block in the resolved chains by name and optional coordinate.

    When lane/pos given, match on them. Otherwise require a unique name match.
    Returns (path_idx, chain_idx, Block).
    """
    matches = []
    for path_idx, chain in enumerate(resolved):
        if path is not None and path_idx != path:
            continue
        # recompute effective (lane,pos) per BlockEntry to compare against coords
        for chain_idx, (block, _p) in enumerate(chain):
            if block.model_id == name or block.display_name == name:
                matches.append((path_idx, chain_idx, block))
    if lane is not None or pos is not None:
        # filter matches by coordinate using the spec's block entries
        coord_matches = []
        for (pi, ci, block) in matches:
            entry = [e for e in spec.paths[pi].blocks
                     if isinstance(e, __import__("helixgen.spec", fromlist=["BlockEntry"]).BlockEntry)][ci]
            e_lane = getattr(entry, "lane", 0)
            e_pos = entry.pos
            if (lane is None or e_lane == lane) and (pos is None or e_pos == pos):
                coord_matches.append((pi, ci, block))
        matches = coord_matches
    if not matches:
        raise GenerateError(f"reference to block {name!r} matches no placed block.")
    if len(matches) > 1:
        raise GenerateError(
            f"block {name!r} matches multiple placed blocks; disambiguate with lane/pos.")
    return matches[0]
```

Thread `spec` and coordinates through the callers `_build_snapshot_overrides`, `_build_fs_assignments`, `_build_exp_assignments` — each currently calls `_resolve_spec_block(name, resolved)`; change to pass `spec=spec` and the ref's `lane`/`pos`/`path`. Those builders receive `spec` already. For FS: `_resolve_spec_block(fs.block, resolved, spec=spec, path=fs.path, lane=fs.lane, pos=fs.pos)`.

In `spec.py`, add optional `path`/`lane`/`pos` fields to `FootswitchAssignment`, `ExpressionTarget`, and snapshot param/disable references, parsed from the ref dicts. Example for `FootswitchAssignment`:

```python
@dataclass
class FootswitchAssignment:
    switch: str
    block: str
    behavior: str = "latching"
    path: int | None = None
    lane: int | None = None
    pos: int | None = None
```

and in `_parse_footswitch`, read them: `path=data.get("path"), lane=data.get("lane"), pos=data.get("pos")`. Do the same for `ExpressionTarget` and for snapshot references (snapshot `disable` list entries and `params` keys reference by name today; add a parallel coordinate form — a `disable` entry may be `{"block": "...", "pos": N}` in addition to a bare string). Keep bare-string/bare-name forms working.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_generate_footswitches.py -q -k duplicate_block_by_coordinate`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py src/helixgen/spec.py tests/test_generate_footswitches.py tests/test_spec_footswitches.py
git commit -m "feat: coordinate-aware block references (lane/pos) for FS/EXP/snapshots"
```

---

## Task 10: Decompile emits coordinate references when ambiguous

**Files:**
- Modify: `src/helixgen/decompile.py` — `_recover_footswitches`, `_recover_expression`, `_recover_snapshots`
- Test: `tests/test_decompile_advanced.py`

**Interfaces:**
- Consumes: the per-block `(lane,pos)` computed in Task 8.
- Produces: when a recovered reference's display name is ambiguous among placed blocks, the emitted ref dict includes `lane`/`pos`; otherwise it stays a bare name. Round-trip stable for a preset with two same-name blocks on different footswitches.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decompile_advanced.py (append)
def test_duplicate_block_footswitches_roundtrip(tmp_path, sample_serial_preset_hsp, strip_provenance):
    from tests.test_generate_footswitches import _dup_ir_lib  # reuse helper
    lib = _dup_ir_lib(tmp_path, sample_serial_preset_hsp)
    from helixgen.generate import compose_preset
    from helixgen.spec import parse_spec
    from helixgen.decompile import decompile_body
    spec = {"name": "n", "paths": [{"blocks": [
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 1},
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 2}]}],
        "footswitches": [{"switch": "FS1", "block": "With Pan", "pos": 1},
                         {"switch": "FS2", "block": "With Pan", "pos": 2}]}
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    spec2 = parse_spec(decompile_body(p1, lib))
    p2 = compose_preset(spec2, lib, source="t")
    assert strip_provenance(p1) == strip_provenance(p2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_decompile_advanced.py -q -k duplicate_block_footswitches`
Expected: FAIL — decompiled FS refs are bare "With Pan" for both → `parse_spec` rejects duplicate block, or recompose attaches both controllers to the wrong slot.

- [ ] **Step 3: Implement ambiguity-aware emission**

Add a helper that, given the flow, builds a display-name → list-of-(lane,pos) index and a resolver that returns a ref dict with coordinates only when the name is ambiguous:

```python
def _name_index(flow, library):
    from collections import defaultdict
    idx = defaultdict(list)
    for pi, path in enumerate(flow):
        if not isinstance(path, dict): continue
        for key in path:
            if not (isinstance(key,str) and key.startswith("b") and key[1:].isdigit()
                    and key not in _ENDPOINT_KEYS): continue
            bnn = path[key]
            if not isinstance(bnn,dict) or bnn.get("type") in ("split","join") or not bnn.get("slot"): continue
            num=int(key[1:]); lane=1 if num>=14 else 0; pos=num-14*lane
            try:
                name = library.load_block(_translate_model_id(bnn["slot"][0].get("model",""))).display_name
            except Exception:
                continue
            idx[name].append((pi, lane, pos))
    return idx


def _ref(name, pi, lane, pos, idx):
    ref = {"block": name}
    if len(idx.get(name, [])) > 1:
        ref["lane"] = lane; ref["pos"] = pos
        if pi: ref["path"] = pi
    return ref
```

Update `_recover_footswitches` / `_recover_expression` / `_recover_snapshots` to compute `(lane,pos)` from each block's slot key (as in Task 8) and emit refs via `_ref(...)`. They currently emit `{"switch": name, "block": display_name, ...}`; change `block` to the dict-merged `_ref(...)` fields (i.e. include `lane`/`pos` when ambiguous). Pass `idx = _name_index(flow, library)` in from `decompile_body` or compute once per recover call.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_decompile_advanced.py -q -k duplicate_block_footswitches`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile_advanced.py
git commit -m "feat(decompile): emit lane/pos coordinates for ambiguous block references"
```

---

## Task 11: Coordinate addressing in patch verbs + CLI

**Files:**
- Modify: `src/helixgen/patch.py` — `resolve_block` to accept `lane`
- Modify: `src/helixgen/cli.py` — add `--lane` to the surgical verbs
- Test: `tests/test_patch.py`, `tests/test_patch_cli.py`

**Interfaces:**
- Consumes: `BlockEntry.lane`/`.pos` in spec dicts.
- Produces: `patch.resolve_block(spec, name, path, index, *, lane=None, pos=None)` — filters candidates by `lane`/`pos` when given; CLI verbs accept `--lane`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_patch.py (append)
import pytest
from helixgen import patch


def test_resolve_block_by_pos():
    spec = {"name":"n","paths":[{"blocks":[
        {"block":"With Pan","pos":1},{"block":"With Pan","pos":2}]}]}
    assert patch.resolve_block(spec, "With Pan", None, None, pos=2) == (0, 1)
    with pytest.raises(patch.PatchError):
        patch.resolve_block(spec, "With Pan", None, None)  # ambiguous
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_patch.py -q -k by_pos`
Expected: FAIL — `resolve_block` has no `pos` kwarg.

- [ ] **Step 3: Implement**

Extend `patch.resolve_block` signature and matching:

```python
def resolve_block(spec, name, path, index, *, lane=None, pos=None):
    matches = []
    for pi, p in enumerate(spec.get("paths", [])):
        for bi, b in enumerate(p.get("blocks", [])):
            if b.get("block") != name:
                continue
            if lane is not None and b.get("lane", 0) != lane:
                continue
            if pos is not None and b.get("pos") != pos:
                continue
            matches.append((pi, bi))
    if path is not None and index is not None:
        if (path, index) in matches:
            return (path, index)
        raise PatchError(f"No block {name!r} at path {path} index {index}.")
    if not matches:
        raise PatchError(f"Block {name!r} not found (with the given lane/pos).")
    if len(matches) > 1:
        raise PatchError(f"Block {name!r} matches {len(matches)} placements; disambiguate with --lane/--index/pos.")
    return matches[0]
```

In `cli.py`, add `@click.option("--lane", type=int, default=None)` to each surgical verb and thread it into the verb call (`lane=lane` where the pure verb accepts it — extend `set_param`/`set_enabled`/etc. to forward `lane`/`pos` to `resolve_block`, mirroring the existing `path`/`index`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_patch.py tests/test_patch_cli.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/patch.py src/helixgen/cli.py tests/test_patch.py tests/test_patch_cli.py
git commit -m "feat(patch): coordinate addressing (lane/pos) in verbs + CLI --lane"
```

---

# Phase 4 — Integration + scoreboard

## Task 12: Skill integration

**Files:**
- Modify: `.claude/skills/tone/SKILL.md`
- Test: `tests/test_skills.py`

**Interfaces:** Documentation — the surgical section covers coordinate addressing for duplicates and refuse/warn outcomes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skills.py (append)
from pathlib import Path


def test_tone_skill_documents_coordinates():
    txt = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "tone" / "SKILL.md").read_text()
    assert "lane" in txt and "pos" in txt
    assert "duplicate" in txt.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_skills.py -q -k coordinates`
Expected: FAIL.

- [ ] **Step 3: Implement — append to the "Adjusting an existing tone" section**

Append:

```markdown
### Addressing duplicate blocks

When a preset has two blocks with the same name (e.g. two IR "With Pan" blocks,
one per lane, or a volume block per split lane), reference the specific one by
its coordinate: add `"pos": N` (and `"lane": 0|1`, `"path": 0|1`) to the
`patch_preset` operation or the snapshot/footswitch/expression reference. A bare
name only works when it is unique in the preset.

If `decompile_preset` refuses a preset (more than two parallel splits, or an
unknown routing block), tell the user it's an unsupported routing shape rather
than editing it blindly. If `generate_preset` warns that an IR hash was passed
through unregistered, mention the user must `register-irs` that WAV to edit it
locally.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_skills.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/tone/SKILL.md tests/test_skills.py
git commit -m "docs(tone): coordinate addressing + refuse/warn handling in surgical loop"
```

---

## Task 13: Acceptance scoreboard — measure the flip

**Files:**
- Modify: `tests/test_decompile_acceptance.py` — keep the measuring body; the xfail marker stays until the residual is zero
- Test: itself

**Interfaces:** The real-export test is the scoreboard. This task runs it against `data/` (on a machine that has exports), records the new round-trip count, and — if the residual is zero — removes the `xfail` marker; otherwise updates the marker reason with the new count and the remaining categories.

- [ ] **Step 1: Run the scoreboard and read the residual**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_decompile_acceptance.py -q -rX`
Expected: XFAIL (or XPASS). Note the assertion message's `ok/total` count and the "First few" failures.

- [ ] **Step 2: If residual is zero — remove the marker**

If the test XPASSes (all exports round-trip), delete the `@pytest.mark.xfail(...)` decorator so it becomes a hard guarantee, and run:

Run: `PYTHONPATH=$PWD/src python -m pytest tests/test_decompile_acceptance.py -q`
Expected: PASS

- [ ] **Step 3: If residual remains — update the marker reason**

If some exports still fail, update the `xfail` `reason=` string to the current `ok/total` count and the remaining failure categories (from the "First few" output), and leave the marker in place. Keep the measuring body unchanged.

- [ ] **Step 4: Run full suite**

Run: `PYTHONPATH=$PWD/src python -m pytest -q`
Expected: PASS (with the acceptance test either PASS or XFAIL, never FAIL).

- [ ] **Step 5: Commit**

```bash
git add tests/test_decompile_acceptance.py
git commit -m "test(decompile): update real-export scoreboard after hardening"
```

---

## Self-Review

**Spec coverage:**
- ① flat lane/pos + split/join model → Tasks 5 (spec), 6 (generate placement), 7 (generate split emission), 8 (decompile). ✓
- ② coordinate-aware references → Tasks 9 (generate + spec), 10 (decompile), 11 (patch/CLI). ✓
- ③ transform changes → Tasks 6/7/8 (generate+decompile). ✓
- ④ loopers catalogued → Task 3. ✓
- ⑤ EXP range + validation edges → Tasks 1 (EXP), 4 (FS multiplicity). ✓
- ⑥ IR pass-through + None-guard → Task 2. ✓
- ⑦ skill integration → Task 12. ✓
- Error handling (refuse >2 splits, unbalanced) → Task 5 validation. ✓
- Backward compatibility (serial byte-identical) → Task 6 Step 5 + full-suite gates throughout. ✓
- Testing / scoreboard → Task 13. ✓

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". The one open-ended item — misc validation edges (empty block name, non-numeric min) — is bounded: the FS-multiplicity case is fixed concretely in Task 4; empty-name and non-numeric-min are guarded by the existing type/emptiness checks in `_parse_path_entry` (Task 5) and `_parse_expression_target` (Task 1). If Task 13's scoreboard surfaces a residual category not covered here, it is recorded in the marker reason (Task 13 Step 3), not silently dropped.

**Type consistency:** `_resolve_spec_block(name, resolved, *, spec=None, path=None, lane=None, pos=None)` is consistent between Task 9's definition and its callers. `resolve_block(spec, name, path, index, *, lane=None, pos=None)` matches between Task 11 and the CLI. `SplitEntry(model, params, lane, pos)` / `JoinEntry(model, params, lane, pos)` are consistent across Tasks 5, 7, 8. Slot-key rule `14*lane+pos` is used identically in Tasks 6, 7, 8, 10. `_emit_splits`/`_reconstruct_path_blocks` names are stable within their tasks.

**Note for the executor:** Tasks 6–10 are the tightly-coupled core; run the full suite after each (the backward-compat and round-trip tests are the safety net). Task 13 is a measurement/decision task — its outcome (flip vs. update-marker) depends on live `data/`, which is only present on the user's machine.
