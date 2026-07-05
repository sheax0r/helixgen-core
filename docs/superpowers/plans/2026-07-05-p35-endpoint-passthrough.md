# P35 Endpoint + Orphaned Split-Join Structural Passthrough — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make arbitrary real-export parallel-split presets round-trip through decompile→generate by capturing the routing skeleton (input/output endpoints + orphaned/cross-path split-join) verbatim, and tighten the acceptance scoreboard to an endpoint-inclusive model compare (194/211 → 211/211).

**Architecture:** A new `StructuralEntry(raw, lane, pos)` spec entry carries a verbatim `bNN` wire dict. Decompile classifies each slot: main-input `b00`→`input` field; endpoints & orphaned split/join→`StructuralEntry`; balanced split/join→existing semantic `SplitEntry`/`JoinEntry`; user blocks→`BlockEntry`. Generate re-emits each `StructuralEntry` verbatim at its `bNN` key. Two one-offs (per-lane capacity guard; ambiguous-IR-basename→hash) are folded in.

**Tech Stack:** Python stdlib + `click`; `pytest` (run with `PYTHONPATH=$PWD/src pytest`).

## Global Constraints

- Run tests with `PYTHONPATH=$PWD/src pytest` (an editable global install may shadow the source tree).
- Pure stdlib + `click` only; no new runtime deps.
- All work on branch `hardening/p35-endpoint-passthrough`. **`main` must stay at `a046c62`** — never commit to `main`. Verify with `git log --oneline -1 main` after each task.
- TDD: failing test first, minimal implementation, green, commit.
- `data/*.hsp` (211 real exports) are gitignored and present only locally; the acceptance test skips when absent. Unit tests must NOT depend on `data/` — use hand-built dicts/fixtures.
- Endpoint slots are discriminated by `bnn["type"] in {"input","output"}`; split/join by `{"split","join"}`; these never collide with user blocks (verified across all 211). Endpoints are always at pos 0 (input) / pos 13 (output) of a lane; lane ∈ {0,1}; `bNN` key = `f"b{14*lane+pos:02d}"`.

---

### Task 1: `StructuralEntry` spec model + parsing

**Files:**
- Modify: `src/helixgen/spec.py` (add dataclass near line 36; add parse branch in `_parse_path_entry` ~line 414)
- Test: `tests/test_spec.py`

**Interfaces:**
- Produces: `StructuralEntry(raw: dict, lane: int = 0, pos: int | None = None)`; parsed from a path-`blocks` entry shaped `{"structural": {<raw bNN dict>}, "lane": L, "pos": P}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_spec.py`:

```python
def test_parse_structural_entry():
    from helixgen.spec import parse_spec, StructuralEntry
    raw = {"type": "output", "position": 13, "path": 1, "endpoint": "b07",
           "slot": [{"model": "P35_OutputPath2B", "params": {"gain": {"value": 0.0}}}]}
    spec = parse_spec({
        "name": "t",
        "paths": [{"blocks": [
            {"block": "Some Block"},
            {"structural": raw, "lane": 1, "pos": 13},
        ]}],
    })
    entries = spec.paths[0].blocks
    assert isinstance(entries[1], StructuralEntry)
    assert entries[1].raw == raw
    assert entries[1].lane == 1
    assert entries[1].pos == 13


def test_structural_entry_ignored_by_split_balance():
    # A lone structural (orphaned split captured verbatim) must NOT trip
    # _validate_splits, which only counts semantic Split/Join entries.
    from helixgen.spec import parse_spec
    parse_spec({
        "name": "t",
        "paths": [{"blocks": [
            {"structural": {"type": "split", "slot": [{"model": "P35_AppDSPSplitY"}]},
             "lane": 0, "pos": 7},
        ]}],
    })  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py::test_parse_structural_entry tests/test_spec.py::test_structural_entry_ignored_by_split_balance -v`
Expected: FAIL (`ImportError: cannot import name 'StructuralEntry'`).

- [ ] **Step 3: Add the dataclass**

In `src/helixgen/spec.py` after the `JoinEntry` dataclass (after line 36):

```python
@dataclass
class StructuralEntry:
    """A routing-skeleton slot (endpoint or orphaned split/join) captured
    verbatim. `raw` is the exact bNN wire dict; generate re-emits it as-is at
    `b{14*lane+pos:02d}`. Never consulted against the block library."""
    raw: dict[str, Any]
    lane: int = 0
    pos: int | None = None
```

- [ ] **Step 4: Add the parse branch**

In `src/helixgen/spec.py`, in `_parse_path_entry`, add BEFORE the `"split"` branch (before line 414 `if "split" in data:`):

```python
    if "structural" in data:
        raw = data["structural"]
        if not isinstance(raw, dict):
            raise _err(source, '"structural" must be an object (verbatim bNN dict).')
        lane, pos = _parse_lane_pos(data, source=source)
        return StructuralEntry(raw=raw, lane=lane, pos=pos)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -v`
Expected: PASS (all, including the two new tests). `_validate_splits` counts only `SplitEntry`/`JoinEntry`, so the structural entry is transparently ignored.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec.py
git commit -m "feat(spec): add StructuralEntry for verbatim routing-skeleton slots

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
git log --oneline -1 main   # must still be a046c62
```

---

### Task 2: generate `_emit_structural` + dispatch

**Files:**
- Modify: `src/helixgen/generate.py` (import at line 32; new function near `_emit_splits` ~line 676; call site in `_compose_preset_hsp` ~line 819)
- Test: `tests/test_generate.py` (or `tests/test_generate_hsp.py` if that is where hsp generate tests live — use the existing hsp generate test module)

**Interfaces:**
- Consumes: `StructuralEntry` from Task 1.
- Produces: `_emit_structural(path_dict: dict, path_entry) -> None` — writes `path_dict[f"b{14*lane+pos:02d}"] = copy.deepcopy(entry.raw)` for each `StructuralEntry` in `path_entry.blocks`.

- [ ] **Step 1: Write the failing test**

Find the module holding hsp generate unit tests (`grep -l "_compose_preset_hsp\|_emit_splits\|_to_hsp_bnn" tests/*.py`). Add there:

```python
def test_emit_structural_writes_raw_verbatim():
    from helixgen.generate import _emit_structural
    from helixgen.spec import StructuralEntry, PathEntry
    raw = {"type": "output", "position": 13, "path": 1, "endpoint": "b07",
           "slot": [{"model": "P35_OutputPath2B", "params": {"gain": {"value": 0.0}}}]}
    path_dict = {"b00": {"type": "input"}}
    pe = PathEntry(blocks=[StructuralEntry(raw=raw, lane=1, pos=13)])
    _emit_structural(path_dict, pe)
    assert path_dict["b27"] == raw
    assert path_dict["b27"] is not raw  # deep-copied, not aliased
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_emit_structural_writes_raw_verbatim -v`
Expected: FAIL (`ImportError: cannot import name '_emit_structural'`).

- [ ] **Step 3: Implement `_emit_structural` and import**

In `src/helixgen/generate.py` line 32, extend the spec import:

```python
from helixgen.spec import BlockEntry, JoinEntry, SplitEntry, StructuralEntry, Spec, parse_spec
```

Add after `_emit_splits` (after line 722):

```python
def _emit_structural(path_dict: dict[str, Any], path_entry) -> None:
    """Write each StructuralEntry (endpoint or orphaned split/join) verbatim to
    its bNN key. Key is computed from the entry's own lane/pos — deliberately
    NOT via _assign_positions — so it never perturbs the block auto-position
    counter. Overwrites the chassis endpoint slots (e.g. b13 main output) with
    the correct per-preset routing block."""
    for e in path_entry.blocks:
        if isinstance(e, StructuralEntry):
            key = f"b{14 * e.lane + e.pos:02d}"
            path_dict[key] = copy.deepcopy(e.raw)
```

- [ ] **Step 4: Wire the call site**

In `src/helixgen/generate.py` in `_compose_preset_hsp`, immediately after the `_emit_splits(path_dict, path_entry, eff)` line (line 819), add:

```python
        _emit_structural(path_dict, path_entry)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_emit_structural_writes_raw_verbatim -v`
Expected: PASS.

Also confirm no regression to `_assign_positions`/`_emit_splits`: `PYTHONPATH=$PWD/src pytest tests/test_generate*.py -q` → all pass. (`StructuralEntry` carries an explicit `pos`, so `_assign_positions` records it in `eff` without corrupting real-block auto positions, and the `.hlx` path at line 225 filters to `BlockEntry` so structural entries are naturally dropped there.)

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/
git commit -m "feat(generate): emit StructuralEntry routing slots verbatim

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
git log --oneline -1 main
```

---

### Task 3: decompile classification (endpoints + orphaned split/join)

**Files:**
- Modify: `src/helixgen/decompile.py` (helpers near line 53; `_reconstruct_path_blocks` line 270; `_iter_blocks` line 53/65; `_name_index` line 87; `_recover_snapshots`/`_recover_footswitches`/`_recover_expression` type skips)
- Test: `tests/test_decompile.py`

**Interfaces:**
- Consumes: `StructuralEntry` (Task 1), `_emit_structural` (Task 2).
- Produces: decompiled specs whose routing-skeleton slots are `StructuralEntry`; no `library.load_block` call on endpoints or orphaned split/join.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_decompile.py` (hand-built path dicts — no `data/` dependency):

```python
def _endpoint_output(model, pos=13, path=0, endpoint="b00"):
    return {"@enabled": {"value": True}, "type": "output", "position": pos,
            "path": path, "endpoint": endpoint,
            "slot": [{"@enabled": {"value": True}, "model": model,
                      "params": {"gain": {"value": 0.0}, "pan": {"value": 0.5}}}]}

def _endpoint_input(model, pos=0, path=0, endpoint="b13"):
    return {"@enabled": {"value": True}, "type": "input", "position": pos,
            "path": path, "endpoint": endpoint,
            "slot": [{"@enabled": {"value": True}, "model": model, "params": {}}]}

def test_reconstruct_captures_branch_endpoints_no_keyerror(hsp_library):
    from helixgen.decompile import _reconstruct_path_blocks
    from helixgen.spec import StructuralEntry
    lib = hsp_library
    # A branch lane with an input endpoint (b14) and an output endpoint (b27)
    # that library.load_block cannot resolve — must NOT raise, must capture.
    path_dict = {
        "b00": _endpoint_input("P35_InputNone"),
        "b13": _endpoint_output("P35_OutputPath2A"),
        "b14": _endpoint_input("P35_InputNone", pos=0, path=1, endpoint="b01"),
        "b27": _endpoint_output("P35_OutputPath2B", pos=13, path=1, endpoint="b07"),
    }
    blocks = _reconstruct_path_blocks(path_dict, lib, None)
    structurals = [b for b in blocks if isinstance(b, StructuralEntry)]
    models = {f"b{14*b.lane+b.pos:02d}": b.raw["slot"][0]["model"] for b in structurals}
    # b00 is NOT captured (drives the `input` field); b13/b14/b27 are.
    assert models == {"b13": "P35_OutputPath2A", "b14": "P35_InputNone",
                      "b27": "P35_OutputPath2B"}

def test_reconstruct_orphaned_split_is_structural_balanced_is_semantic(hsp_library):
    from helixgen.decompile import _reconstruct_path_blocks
    from helixgen.spec import StructuralEntry, SplitEntry, JoinEntry
    lib = hsp_library
    # Orphaned split: endpoint points at an OUTPUT endpoint (b27), not a join.
    orphan = {
        "b00": _endpoint_input("P35_InputNone"),
        "b07": {"type": "split", "position": 7, "path": 0, "branch": "b15",
                "endpoint": "b27", "slot": [{"model": "P35_AppDSPSplitY", "params": {}}]},
        "b13": _endpoint_output("P35_OutputPath2A"),
        "b27": _endpoint_output("P35_OutputPath2B", pos=13, path=1, endpoint="b07"),
    }
    blocks = _reconstruct_path_blocks(orphan, lib, None)
    assert any(isinstance(b, StructuralEntry) and b.raw.get("type") == "split" for b in blocks)
    assert not any(isinstance(b, (SplitEntry, JoinEntry)) for b in blocks)
```

(If `hsp_library` isn't the right fixture name, use the one other `test_decompile.py` tests use for a `Library`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py::test_reconstruct_captures_branch_endpoints_no_keyerror tests/test_decompile.py::test_reconstruct_orphaned_split_is_structural_balanced_is_semantic -v`
Expected: FAIL — the first with `KeyError: No block with model_id 'P35_OutputPath2B'`, the second with an unbalanced/KeyError or a `SplitEntry` present.

- [ ] **Step 3: Add classification helpers**

In `src/helixgen/decompile.py` near line 53, add:

```python
def _is_endpoint(bnn: dict) -> bool:
    return isinstance(bnn, dict) and bnn.get("type") in ("input", "output")

def _is_orphan_structural(path_dict: dict, bnn: dict) -> bool:
    """True for a split/join whose `endpoint` partner is NOT the complementary
    block type (i.e. the partner is an input/output endpoint). Such split/join
    slots cannot be reconstructed semantically and are captured verbatim."""
    typ = bnn.get("type")
    if typ not in ("split", "join"):
        return False
    partner = path_dict.get(bnn.get("endpoint"))
    partner_type = partner.get("type") if isinstance(partner, dict) else None
    want = "join" if typ == "split" else "split"
    return partner_type != want

def _is_structural_slot(path_dict: dict, key: str, bnn: dict) -> bool:
    """A routing-skeleton slot captured verbatim: any endpoint (except the main
    input b00, which drives the `input` field) or an orphaned split/join."""
    if _is_endpoint(bnn):
        return key != "b00"
    return _is_orphan_structural(path_dict, bnn)
```

- [ ] **Step 4: Rewrite `_reconstruct_path_blocks` to route structural slots**

Replace the body of `_reconstruct_path_blocks` (lines 270–306) with:

```python
def _reconstruct_path_blocks(path_dict, library, irs):
    """Ordered spec ``blocks`` list for one .hsp path.

    - Main input b00 → dropped here (drives the `input` field).
    - Endpoints (other than b00) and orphaned split/join → StructuralEntry
      (verbatim); library is never consulted for them.
    - Balanced split/join → semantic Split/Join with branch reconstruction.
    - User blocks → BlockEntry.
    """
    from helixgen.spec import StructuralEntry

    def all_bnn():
        return [k for k in path_dict
                if isinstance(k, str) and k.startswith("b") and k[1:].isdigit()
                and isinstance(path_dict[k], dict) and path_dict[k].get("slot")]

    def structural_entry(k):
        bnn = path_dict[k]
        num = int(k[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        return StructuralEntry(raw=copy.deepcopy(bnn), lane=lane, pos=pos)

    keys = all_bnn()
    structural_keys = {k for k in keys if _is_structural_slot(path_dict, k, path_dict[k])}
    # user_keys: real blocks + balanced split/join (b00 excluded as an endpoint,
    # structural keys excluded, but semantic split/join stay in).
    user_keys = [k for k in keys if k != "b00" and k not in structural_keys]
    lane0 = sorted((k for k in user_keys if int(k[1:]) < 14), key=lambda k: int(k[1:]))
    lane1 = sorted((k for k in user_keys if int(k[1:]) >= 14), key=lambda k: int(k[1:]))

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
    claimed = {e_key for k in lane0 if path_dict[k].get("type") == "split"
               for e_key in branch_span(path_dict[k])}
    for bk in lane1:
        if bk not in claimed:
            out.append(_entry_for(bk, path_dict[bk], library, irs))
    # Structural slots (endpoints + orphaned split/join), in key order.
    for k in sorted(structural_keys, key=lambda k: int(k[1:])):
        out.append(structural_entry(k))
    return out
```

Ensure `import copy` is present at the top of `decompile.py` (add if missing).

- [ ] **Step 5: Skip endpoints in the metadata/recovery scans**

In `_iter_blocks` (line 65), broaden the skip:

```python
                if bnn.get("type") in ("split", "join", "input", "output"):
                    continue
```

In `_name_index` (line 87), broaden the same guard:

```python
            if not isinstance(bnn, dict) or bnn.get("type") in ("split", "join", "input", "output") or not bnn.get("slot"):
                continue
```

In `_recover_snapshots`, `_recover_footswitches`, `_recover_expression`: these iterate via `_iter_blocks` (verify with `grep -n "_iter_blocks" src/helixgen/decompile.py`). If any iterates `path_dict` keys directly and calls `library.load_block`, add the same `type in (...,"input","output")` skip there. (Orphaned split/join already keep `type` split/join, covered by the existing skip.)

- [ ] **Step 6: Add a REAL-generate-path round-trip test (reviewer-requested)**

The unit tests above check decompile classification and (Task 2) verbatim emit in
isolation. Add one test that drives a `StructuralEntry` through the *actual*
`compose_preset` path together with a balanced split, proving
`_assign_positions` / `_emit_splits` / `_emit_structural` cooperate (this is the
interaction the emulations could not exercise). In `tests/test_decompile.py`:

```python
def test_structural_entry_survives_real_compose(hsp_library):
    # A spec carrying BOTH a balanced split (semantic) AND a verbatim
    # StructuralEntry (orphaned output endpoint) must compose without raising
    # and place the structural slot at its exact key.
    from helixgen.spec import parse_spec
    from helixgen.generate import compose_preset
    raw_out = {"@enabled": {"value": True}, "type": "output", "position": 13,
               "path": 1, "endpoint": "b07",
               "slot": [{"@enabled": {"value": True}, "model": "P35_OutputPath2B",
                         "params": {"gain": {"value": 0.0}}}]}
    spec = parse_spec({"name": "t", "paths": [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 1},
        {"split": {"model": "P35_AppDSPSplitY"}, "lane": 0, "pos": 2},
        {"block": "Tube Drive", "lane": 1, "pos": 3},
        {"join": {}, "lane": 0, "pos": 4},
        {"structural": raw_out, "lane": 1, "pos": 13},
    ]}]})
    body = compose_preset(spec, hsp_library, source="t")
    flow0 = body["preset"]["flow"][0]
    assert flow0["b27"]["slot"][0]["model"] == "P35_OutputPath2B"
    assert flow0["b27"] == raw_out
```

(Substitute "Tube Drive" with a block present in the hsp fixture library, and
adjust the split/join placement if the fixture chassis constrains positions.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py tests/test_decompile_advanced.py -v`
Expected: PASS — the three new tests plus all existing decompile round-trip tests (the 60 balanced-split presets keep semantic Split/Join, so `test_decompile_advanced.py` is unaffected).

- [ ] **Step 8: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile.py
git commit -m "feat(decompile): capture endpoints + orphaned split-join as StructuralEntry

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
git log --oneline -1 main
```

---

### Task 4: per-lane capacity guard

**Files:**
- Modify: `src/helixgen/generate.py` (`_compose_preset_hsp`, the guard at lines 781–785)
- Test: `tests/test_generate*.py` (hsp module)

**Interfaces:**
- Consumes: `path_entry.blocks` with `BlockEntry.lane`.

- [ ] **Step 1: Write the failing test**

Add to the hsp generate test module (needs the hsp chassis fixture other hsp tests use — reuse it):

```python
def test_per_lane_capacity_allows_13_total_across_lanes(hsp_library):
    # 10 main-lane + 3 branch-lane blocks = 13 total but each lane <= 12: OK.
    from helixgen.spec import parse_spec
    from helixgen.generate import compose_preset
    main = [{"block": "Tube Drive", "lane": 0, "pos": i} for i in range(1, 8)]  # 7
    main += [{"block": "Tube Drive", "lane": 0, "pos": i} for i in (10, 11, 12)]  # +3 = 10
    split = [{"split": {"model": "P35_AppDSPSplitY"}, "lane": 0, "pos": 8},
             {"join": {}, "lane": 0, "pos": 9}]
    branch = [{"block": "Tube Drive", "lane": 1, "pos": i} for i in (1, 2, 3)]  # 3
    spec = parse_spec({"name": "t", "paths": [{"blocks": main + split + branch}]})
    compose_preset(spec, hsp_library, source="t")  # must NOT raise

def test_per_lane_capacity_rejects_13_on_one_lane(hsp_library):
    import pytest
    from helixgen.spec import parse_spec
    from helixgen.generate import compose_preset, GenerateError
    blocks = [{"block": "Tube Drive", "lane": 0, "pos": i} for i in range(1, 14)]  # 13 on lane 0
    spec = parse_spec({"name": "t", "paths": [{"blocks": blocks}]})
    with pytest.raises(GenerateError):
        compose_preset(spec, hsp_library, source="t")
```

(Use whatever block name exists in the hsp fixture library — `grep` an existing hsp generate test for the block name it uses; substitute for "Tube Drive" if needed.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_per_lane_capacity_allows_13_total_across_lanes -v`
Expected: FAIL — `GenerateError: Path 0 has 13 blocks; only 12 user slots` (the current per-path guard miscounts).

- [ ] **Step 3: Replace the guard with a per-lane count**

In `src/helixgen/generate.py`, delete the guard at lines 781–785:

```python
        if len(chain) > len(_HSP_BNN_RANGE):
            raise GenerateError(
                f"Path {path_index} has {len(chain)} blocks; only "
                f"{len(_HSP_BNN_RANGE)} user slots (b01..b12) available."
            )
```

and re-add it AFTER `block_entries = [...]` is computed (currently line 790), as a per-lane check:

```python
        block_entries = [e for e in path_entry.blocks if isinstance(e, BlockEntry)]
        for lane in (0, 1):
            n = sum(1 for e in block_entries if getattr(e, "lane", 0) == lane)
            if n > len(_HSP_BNN_RANGE):
                raise GenerateError(
                    f"Path {path_index} lane {lane} has {n} blocks; only "
                    f"{len(_HSP_BNN_RANGE)} user slots (b01..b12) per lane available."
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py -v`
Expected: PASS (both new tests + existing).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/generate.py tests/
git commit -m "fix(generate): count block-slot capacity per lane, not per path

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
git log --oneline -1 main
```

---

### Task 5: ambiguous IR basename → emit hash

**Files:**
- Modify: `src/helixgen/decompile.py` (`_block_entry`, lines 345–357)
- Test: `tests/test_ir_generate.py` (has the `stadium_library_with_ir` fixture cataloging the IR block `HX2_ImpulseResponseWithPan` / display name "With Pan")

**Interfaces:**
- Consumes: `IrMapping.entries` (hash→relative/abs path dict).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ir_generate.py` (reuses the module-local `stadium_library_with_ir` fixture):

```python
def test_block_entry_emits_hash_when_basename_ambiguous(stadium_library_with_ir, tmp_path):
    from helixgen.decompile import _block_entry
    from helixgen.ir import IrMapping
    # Two registered wavs share a basename → basename is ambiguous → emit hash.
    h1, h2 = "a" * 32, "b" * 32
    irs = IrMapping(irs_dir=tmp_path, entries={h1: "dirA/Same.wav", h2: "dirB/Same.wav"})
    slot = {"model": "HX2_ImpulseResponseWithPan", "irhash": h1,
            "params": {}, "@enabled": {"value": True}}
    entry = _block_entry(slot, stadium_library_with_ir, irs)
    assert entry["ir"] == h1   # the hash, not "Same.wav"

def test_block_entry_emits_basename_when_unique(stadium_library_with_ir, tmp_path):
    from helixgen.decompile import _block_entry
    from helixgen.ir import IrMapping
    h1 = "c" * 32
    irs = IrMapping(irs_dir=tmp_path, entries={h1: "dirA/Unique.wav"})
    slot = {"model": "HX2_ImpulseResponseWithPan", "irhash": h1,
            "params": {}, "@enabled": {"value": True}}
    entry = _block_entry(slot, stadium_library_with_ir, irs)
    assert entry["ir"] == "Unique.wav"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_ir_generate.py::test_block_entry_emits_hash_when_basename_ambiguous -v`
Expected: FAIL — `entry["ir"] == "Same.wav"` (current code emits the ambiguous basename).

- [ ] **Step 3: Guard the basename on uniqueness**

In `src/helixgen/decompile.py`, replace the irhash block (lines 345–357):

```python
    if model.startswith(IR_MODEL_PREFIX) and slot.get("irhash"):
        irhash = slot["irhash"]
        basename = None
        if irs is not None and irhash in irs.entries:
            cand = os.path.basename(irs.entries[irhash])
            # Emit the basename ONLY if it maps back to exactly one registered
            # wav; otherwise the basename is ambiguous and regeneration would
            # raise, so emit the unambiguous 32-hex hash instead.
            n = sum(1 for p in irs.entries.values() if os.path.basename(p) == cand)
            if n == 1:
                basename = cand
        entry["ir"] = basename if basename is not None else irhash
    elif model.startswith(IR_MODEL_PREFIX):
        entry["no_ir"] = True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_ir_generate.py -v`
Expected: PASS (both new tests + existing IR round-trip tests).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/decompile.py tests/test_ir_generate.py
git commit -m "fix(decompile): emit IR hash when the basename is ambiguous

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
git log --oneline -1 main
```

---

### Task 6: tighten the acceptance scoreboard + remove xfail

**Files:**
- Modify: `tests/test_decompile_acceptance.py`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Tighten `_models` and drop the xfail**

In `tests/test_decompile_acceptance.py`, change `_models` to compare every flow `bNN` (remove the `b00`/`b13` exclusion):

```python
def _models(b):
    out = []
    for path in (b.get("preset") or {}).get("flow") or []:
        for k in sorted(path):
            if k.startswith("b") and k[1:].isdigit():
                slot = path[k].get("slot", [{}])[0]
                out.append(slot.get("model"))
    return out
```

Remove the `@pytest.mark.xfail(...)` decorator on `test_real_export_decompile_roundtrip_stable`. Update the module/function docstring to state the new bar:

> Compares the slot **model** at every flow `bNN` (including the b00/b13 endpoints)
> for every real export in `data/`. Passes when all present exports round-trip.
> This bar does NOT assert endpoint routing-pointer or param fidelity, nor the
> unmodeled `sources`/`meta`/`xyctrl`/snapshot-validity fields (a future cycle);
> only the hardware step exercises routing.

- [ ] **Step 2: Run the acceptance test**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_acceptance.py -v`
Expected: PASS, `211/211` (only where `data/*.hsp` exist; otherwise skipped). If any preset still fails, the assertion prints `ok/total` and the first few `(name, error)` — debug those before proceeding (do NOT re-add xfail to mask them).

- [ ] **Step 3: Run the FULL suite**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: all green, zero regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_decompile_acceptance.py
git commit -m "test(decompile): tighten scoreboard to endpoint-inclusive models; xpass 211/211

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
git log --oneline -1 main
```

---

### Task 7: hardware verification (manual, with the user)

**Files:** none (device round-trip).

- [ ] **Step 1: Regenerate a fixed split preset**

```bash
PYTHONPATH=$PWD/src python -c "
from pathlib import Path
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp
from helixgen.decompile import decompile_body
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.ir import IrMapping
import tempfile, json
lib = Library(root=Path(tempfile.mkdtemp())/'lib')
for s in sorted(Path('data').glob('*.hsp')): ingest_path(s, lib)
src = Path('data/Black Keys.hsp')
spec = parse_spec(decompile_body(read_hsp(src), lib, irs=IrMapping.load()))
out = Path('/tmp/BlackKeys-regen.hsp')
from helixgen.generate import generate_preset
import helixgen.generate as g
# write via the spec→file path
sp = Path(tempfile.mktemp(suffix='.json')); sp.write_text(json.dumps(spec))
generate_preset(sp, out, lib, irs=IrMapping.load())
print('wrote', out)
"
open -R /tmp/BlackKeys-regen.hsp
```

- [ ] **Step 2: Ask the user to load it on the Stadium XL**

Present the regenerated preset (`SendUserFile` + `open -R`). Ask the user to import it and confirm the parallel split branch loads and routes correctly (both lanes audible / routed as in the original).

- [ ] **Step 3: On confirmation, finish the branch**

Use `superpowers:finishing-a-development-branch` to open the PR (do NOT move `stable`/tags). Update `docs/superpowers/specs/2026-07-03-decompiler-round-trip-residuals.md` to mark Category 2 DONE and the two one-offs closed.

---

## Self-Review

- **Spec coverage:** Task 1 = `StructuralEntry` model; Task 2 = generate emit + dispatch; Task 3 = decompile classification (endpoints + orphaned/balanced split-join) + recovery-scan skips; Task 4 = per-lane capacity guard; Task 5 = ambiguous-IR-basename→hash; Task 6 = test tightening + xfail removal; Task 7 = hardware verify. All spec sections mapped.
- **Type consistency:** `StructuralEntry(raw, lane, pos)` used identically in spec.py, generate `_emit_structural`, decompile `_reconstruct_path_blocks`. Key math `f"b{14*lane+pos:02d}"` identical in Task 2 and Task 3. `_is_structural_slot` used only in Task 3.
- **No `data/` dependency in unit tests:** Tasks 1–5 use hand-built dicts / synthetic IrMapping; only Task 6 touches `data/` (guarded skip).
- **`main` invariant:** every task ends with `git log --oneline -1 main` (must read `a046c62`).
