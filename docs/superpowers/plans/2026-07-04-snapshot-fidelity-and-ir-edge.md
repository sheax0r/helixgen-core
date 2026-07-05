# Snapshot Fidelity + IR Edge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three of the decompiler round-trip residual categories — dense
snapshot arrays (a user-reported recall bug), coordinate-aware snapshot references
to duplicate-named blocks, and IR blocks with no assigned IR — plus two minor
decompile-emit bugs, then re-measure the real-export scoreboard.

**Architecture:** All changes are in the snapshot/IR paths of three modules:
`spec.py` (data model + parsing), `generate.py` (spec → `.hsp` body), and
`decompile.py` (`.hsp` body → spec). The through-line is the existing
"clean unless it has to carry a coordinate" pattern already used by
footswitch/expression references via the `decompile._ref` helper and the
coordinate-capable `generate._resolve_spec_block`.

**Tech Stack:** Python 3, pure stdlib + `click`; `pytest` (run with
`PYTHONPATH=$PWD/src`).

## Global Constraints

- Run every test command with `PYTHONPATH=$PWD/src` (an editable install may
  otherwise shadow the worktree source).
- TDD: failing test first, then minimal implementation.
- Pure stdlib + `click` only; no new runtime dependencies.
- Do NOT commit to `main`. All commits land on the current feature branch
  (`hardening/snapshot-coordinate-refs`). After any subagent-driven task, verify
  `git log main` is unchanged.
- Never edit `gen/` directories.
- Design specs this plan implements:
  `docs/superpowers/specs/2026-07-04-snapshot-coordinate-refs-design.md` and
  `docs/superpowers/specs/2026-07-04-dense-snapshot-arrays-design.md`.

## Test fixtures — where to get libraries and bodies

There is no `test_generate_snapshots.py`; snapshot generate tests live in
`tests/test_generate.py` and `tests/test_generate_combined.py`. Use these:

- **Synthetic Library:** the `hsp_library` fixture in `tests/conftest.py` — a
  Stadium chassis plus two synthetic blocks, `"Tube Drive"` (`HD2_DistTube`,
  params `Gain`/`Tone`) and `"Brit Amp"` (`HD2_AmpBrit`, params `Drive`/`Master`).
  This is the canonical library for generate/decompile/spec unit tests. Where a
  task example names a block/param not in this library (e.g. "Teardrop 310" /
  "Position"), substitute `"Tube Drive"`/`"Gain"` (or `"Brit Amp"`/`"Drive"`).
- **Duplicate-named / split bodies:** copy the body-construction and split
  pattern from `tests/test_decompile_advanced.py` (it already builds lane-1
  branch bodies). A duplicate display name is produced by placing the SAME block
  twice — e.g. two `"Tube Drive"` entries at different `pos`, or one on lane 0 and
  one on lane 1 across a split.
- **Pure-function tests** (Task 1's `_wrap_value_with_snapshots`) need no fixture;
  a new `tests/test_generate_snapshots.py` file is fine for them.
- **Real-export integration checks** are always skip-if-absent (guard with
  `pytest.skip` when `data/*.hsp` is missing), mirroring
  `tests/test_generate_combined.py::_library` and
  `tests/test_decompile_acceptance.py`.
- When a task's example uses a named fixture that does not exist
  (`dup_named_snapshot_body`, `real_lib`, `ir_no_hash_body`, etc.), build it
  inline from `hsp_library` + the patterns above; the names in the examples are
  descriptive, not pre-existing fixtures.

---

### Task 1: Dense snapshot arrays (Category 4)

Fixes the user-reported recall bug: helixgen emitted sparse per-snapshot arrays
(`[false, null, null, ...]`) where the device expects dense (`[false, true, true,
...]`). `null` on a live snapshot is undefined recall state.

**Files:**
- Modify: `src/helixgen/generate.py` (`_wrap_value_with_snapshots`, ~line 327-337)
- Test: `tests/test_generate_snapshots.py` (add tests; create if absent — check
  first with `ls tests/ | grep -i snapshot`)

**Interfaces:**
- Consumes: `_wrap_value_with_snapshots(base, snapshot_overrides) -> dict`.
- Produces: same signature; the returned `["snapshots"]` array now contains no
  `None` when emitted (every slot is `base` where the override was `None`).

- [ ] **Step 1: Write the failing test**

Add to the snapshot generate test file:

```python
from helixgen.generate import _wrap_value_with_snapshots

def test_wrap_densifies_enabled_array():
    # base True (block enabled), disabled only in snapshot 0
    overrides = [False, None, None, None, None, None, None, None]
    wrapped = _wrap_value_with_snapshots(True, overrides)
    assert wrapped["snapshots"] == [False, True, True, True, True, True, True, True]

def test_wrap_densifies_param_array():
    overrides = [None, 0.30, None, None, None, None, None, None]
    wrapped = _wrap_value_with_snapshots(0.12, overrides)
    assert wrapped["snapshots"] == [0.12, 0.30, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12]

def test_wrap_no_variation_stays_plain():
    assert _wrap_value_with_snapshots(0.5, [None]*8) == {"value": 0.5}
    assert _wrap_value_with_snapshots(0.5, None) == {"value": 0.5}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate_snapshots.py -k densif -v`
Expected: FAIL — array still contains `None`.

- [ ] **Step 3: Write minimal implementation**

In `generate.py`, replace the array assignment inside `_wrap_value_with_snapshots`:

```python
    wrapped: dict[str, Any] = {"value": base}
    if snapshot_overrides and any(o is not None for o in snapshot_overrides):
        wrapped["snapshots"] = [base if o is None else o for o in snapshot_overrides]
    return wrapped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate_snapshots.py -k densif -v`
Expected: PASS.

- [ ] **Step 5: Update any existing tests that asserted sparse output**

Run: `PYTHONPATH=$PWD/src pytest tests/ -k snapshot -q` and
`grep -rn "None, None, None" tests/ | grep -i snap`. For any test that asserted a
sparse `[x, None, ...]` snapshots array as *generate output*, update the
expectation to the dense form (base value in the previously-`None` slots).
Decompile-side tests that build override arrays are unaffected.

- [ ] **Step 6: Run the full generate + decompile suites**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate_snapshots.py tests/test_decompile*.py -q`
Expected: PASS (0 failures).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "fix(generate): densify snapshot arrays (null->base) for reliable device recall"
```

---

### Task 2: Snapshot reference data model (Category 1, part 1)

Make snapshot `disable`/`params` carry optional `(path, lane, pos)` coordinates,
normalizing both the existing bare form and the new coordinate form to one
internal representation.

**Files:**
- Modify: `src/helixgen/spec.py` (`Snapshot` dataclass ~line 45-52; `_parse_snapshot`
  ~line 160+)
- Test: `tests/test_spec.py` (add; confirm filename with `ls tests/ | grep spec`)

**Interfaces:**
- Produces (used by Tasks 3 and 4):
  ```python
  @dataclass
  class SnapshotBlockRef:
      block: str
      path: int | None = None
      lane: int | None = None
      pos:  int | None = None

  @dataclass
  class SnapshotParamOverride:
      ref: SnapshotBlockRef
      params: dict[str, Any]

  @dataclass
  class Snapshot:
      name: str
      disable: list[SnapshotBlockRef] = field(default_factory=list)
      params:  list[SnapshotParamOverride] = field(default_factory=list)
  ```
- Parser accepts, for one snapshot:
  - `disable`: list whose entries are each a `str` or
    `{"block": str, "lane"?: int, "pos"?: int, "path"?: int}`.
  - `params`: EITHER a dict `{"<block>": {"<param>": v, ...}, ...}` OR a list
    `[{"block": str, "lane"?, "pos"?, "path"?, "params": {...}}, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
from helixgen.spec import parse_spec, SnapshotBlockRef, SnapshotParamOverride

_BASE = {"name": "P", "paths": [{"blocks": [{"block": "Stereo"}]}]}

def _spec(snapshots):
    return parse_spec({**_BASE, "snapshots": snapshots})

def test_disable_bare_string_normalizes_to_ref():
    s = _spec([{"name": "A", "disable": ["Stereo"]}])
    assert s.snapshots[0].disable == [SnapshotBlockRef(block="Stereo")]

def test_disable_coordinate_dict():
    s = _spec([{"name": "A", "disable": [{"block": "Stereo", "lane": 1, "pos": 2}]}])
    assert s.snapshots[0].disable == [SnapshotBlockRef(block="Stereo", lane=1, pos=2)]

def test_params_dict_form_normalizes_to_list():
    s = _spec([{"name": "A", "params": {"Stereo": {"Mix": 0.3}}}])
    ov = s.snapshots[0].params
    assert ov == [SnapshotParamOverride(ref=SnapshotBlockRef(block="Stereo"),
                                        params={"Mix": 0.3})]

def test_params_list_form_with_coordinates():
    s = _spec([{"name": "A", "params": [
        {"block": "Stereo", "lane": 1, "pos": 2, "params": {"Mix": 0.3}}]}])
    ov = s.snapshots[0].params
    assert ov == [SnapshotParamOverride(
        ref=SnapshotBlockRef(block="Stereo", lane=1, pos=2), params={"Mix": 0.3})]

def test_params_list_entry_requires_params_object():
    import pytest
    from helixgen.spec import SpecError
    with pytest.raises(SpecError):
        _spec([{"name": "A", "params": [{"block": "Stereo"}]}])
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -k "disable or params_dict or params_list" -v`
Expected: FAIL (ImportError on `SnapshotBlockRef` / `SnapshotParamOverride`, then
assertion failures).

- [ ] **Step 3: Implement the data model + parser**

Replace the `Snapshot` dataclass and add the two ref dataclasses (place them just
above `Snapshot`):

```python
@dataclass
class SnapshotBlockRef:
    block: str
    path: int | None = None
    lane: int | None = None
    pos:  int | None = None


@dataclass
class SnapshotParamOverride:
    ref: SnapshotBlockRef
    params: dict[str, Any]


@dataclass
class Snapshot:
    name: str
    disable: list[SnapshotBlockRef] = field(default_factory=list)
    params:  list[SnapshotParamOverride] = field(default_factory=list)
```

In `_parse_snapshot`, parse the two fields. Add these helpers and use them (the
existing name-string validation for `name` stays):

```python
def _parse_snapshot_ref(entry: Any, *, source: str) -> "SnapshotBlockRef":
    if isinstance(entry, str):
        if not entry:
            raise _err(source, '"block" must be a non-empty string.')
        return SnapshotBlockRef(block=entry)
    if not isinstance(entry, dict):
        raise _err(source, "must be a string or a {block, lane, pos} object.")
    block = entry.get("block")
    if not isinstance(block, str) or not block:
        raise _err(source, '"block" is required and must be a non-empty string.')
    return SnapshotBlockRef(
        block=block,
        path=_opt_int(entry.get("path"), source=f"{source} path"),
        lane=_opt_int(entry.get("lane"), source=f"{source} lane"),
        pos=_opt_int(entry.get("pos"),  source=f"{source} pos"),
    )


def _opt_int(v: Any, *, source: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise _err(source, "must be an integer.")
    return v
```

(If an `_opt_int`-equivalent already exists for FS/EXP parsing, reuse it instead
of adding a duplicate — check with `grep -n "def _opt_int\|must be an integer" src/helixgen/spec.py`.)

Then in `_parse_snapshot`:

```python
    disable_raw = data.get("disable", [])
    if not isinstance(disable_raw, list):
        raise _err(source, '"disable" must be a list.')
    disable = [_parse_snapshot_ref(e, source=f"{source} disable[{i}]")
               for i, e in enumerate(disable_raw)]

    params_raw = data.get("params", {})
    params: list[SnapshotParamOverride] = []
    if isinstance(params_raw, dict):
        for block_name, ov in params_raw.items():
            if not isinstance(ov, dict):
                raise _err(source, f'params[{block_name!r}] must be an object.')
            params.append(SnapshotParamOverride(
                ref=SnapshotBlockRef(block=block_name), params=ov))
    elif isinstance(params_raw, list):
        for i, e in enumerate(params_raw):
            if not isinstance(e, dict):
                raise _err(source, f'params[{i}] must be an object.')
            pov = e.get("params")
            if not isinstance(pov, dict):
                raise _err(source, f'params[{i}]: "params" must be an object.')
            ref = _parse_snapshot_ref(
                {k: v for k, v in e.items() if k != "params"},
                source=f"{source} params[{i}]")
            params.append(SnapshotParamOverride(ref=ref, params=pov))
    else:
        raise _err(source, '"params" must be an object or a list.')

    return Snapshot(name=name, disable=disable, params=params)
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -k "disable or params" -v`
Expected: PASS.

- [ ] **Step 5: Run the full spec + generate suites to catch fallout**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py tests/test_generate*.py -q`
Expected: likely FAILURES in `generate` and older snapshot tests that read
`snap.disable` as `list[str]` or `snap.params` as `dict` — these are fixed in
Task 3. Note them; do not fix generate here beyond confirming the *spec* tests
pass.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(spec): coordinate-aware snapshot disable/params (dual-form params)"
```

---

### Task 3: Thread snapshot coordinates through generate (Category 1, part 2)

**Files:**
- Modify: `src/helixgen/generate.py` (`_build_snapshot_overrides`, ~line 544-581)
- Test: `tests/test_generate_snapshots.py`

**Interfaces:**
- Consumes: `Snapshot.disable: list[SnapshotBlockRef]`,
  `Snapshot.params: list[SnapshotParamOverride]` (Task 2);
  `_resolve_spec_block(name, resolved, *, spec, path, lane, pos)` (existing).
- Produces: unchanged return shape of `_build_snapshot_overrides`.

- [ ] **Step 1: Write the failing test**

Build a spec with two blocks that share a display name across lanes (a split), a
snapshot that disables one by coordinate and param-overrides the other by
coordinate, and assert the override lands on the right chain index.

```python
from helixgen.generate import _build_snapshot_overrides, resolve_blocks
from helixgen.spec import parse_spec
from helixgen.library import Library

def test_snapshot_override_resolves_by_coordinate(tmp_path):
    lib = Library(root=tmp_path / "lib")  # populate via a fixture or ingest; see
    # existing snapshot tests for the established library-construction helper.
    spec = parse_spec({
        "name": "P",
        "paths": [{"blocks": [
            {"block": "Teardrop 310", "lane": 0, "pos": 1},
            {"block": "Teardrop 310", "lane": 0, "pos": 2},
        ]}],
        "snapshots": [{"name": "A", "params": [
            {"block": "Teardrop 310", "lane": 0, "pos": 2, "params": {"Position": 0.4}}]}],
    })
    resolved = resolve_blocks(spec, lib)
    _enabled, param_map = _build_snapshot_overrides(spec, resolved)
    # chain index 1 (pos 2) carries the override, not chain index 0
    assert (0, 1) in param_map and (0, 0) not in param_map
```

NOTE: reuse whatever library/block fixture the existing
`tests/test_generate_snapshots.py` uses (e.g. a synthetic block with a `Position`
param). If "Teardrop 310"/"Position" is not in the test library, substitute the
duplicate-placed block + param that the existing tests already rely on.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate_snapshots.py -k coordinate -v`
Expected: FAIL — current code iterates `snap.disable` as strings / `snap.params`
as a dict and raises AttributeError or "matches multiple placed blocks".

- [ ] **Step 3: Implement**

Rewrite the loop body of `_build_snapshot_overrides`:

```python
    for snap_idx, snap in enumerate(spec.snapshots):
        for ref in snap.disable:
            path_idx, chain_idx, _ = _resolve_spec_block(
                ref.block, resolved, spec=spec,
                path=ref.path, lane=ref.lane, pos=ref.pos)
            key = (path_idx, chain_idx)
            enabled_map.setdefault(key, [None] * HSP_SNAPSHOT_SLOTS)
            enabled_map[key][snap_idx] = False

        for ov in snap.params:
            r = ov.ref
            path_idx, chain_idx, block = _resolve_spec_block(
                r.block, resolved, spec=spec, path=r.path, lane=r.lane, pos=r.pos)
            validate_params(block, ov.params)
            key = (path_idx, chain_idx)
            block_params = param_map.setdefault(key, {})
            for pname, pval in ov.params.items():
                arr = block_params.setdefault(pname, [None] * HSP_SNAPSHOT_SLOTS)
                arr[snap_idx] = pval
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate_snapshots.py -k coordinate -v`
Expected: PASS.

- [ ] **Step 5: Fix and run the full generate suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate*.py tests/test_spec.py -q`
Update any older snapshot tests still constructing `Snapshot(disable=[str])` /
`params={...}` directly — they must now use `SnapshotBlockRef` /
`SnapshotParamOverride`, or go through `parse_spec` (preferred).
Expected: PASS (0 failures).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(generate): resolve snapshot refs by (path,lane,pos) coordinate"
```

---

### Task 4: Emit coordinate-aware snapshot refs on decompile (Category 1, part 3)

**Files:**
- Modify: `src/helixgen/decompile.py` (`_recover_snapshots` ~line 115-145;
  `decompile_body` orchestration — move `idx = _name_index(...)` above the
  `_recover_snapshots` call and pass `idx` in)
- Test: `tests/test_decompile*.py` (snapshot round-trip)

**Interfaces:**
- Consumes: `_name_index(flow, library) -> dict[name -> [(pi,lane,pos), ...]]`,
  `_ref(name, pi, lane, pos, idx) -> dict` (existing).
- Produces: `_recover_snapshots(body, library, idx)` — NEW third positional arg.
  Emits `disable` entries as bare strings when unambiguous, `{block,lane,pos,path}`
  dicts when ambiguous; emits `params` as the name-keyed dict when every
  param-block in that snapshot is unambiguous, else as the list-of-`{**ref,
  "params": {...}}` form.

- [ ] **Step 1: Write the failing test**

Use the existing decompile round-trip helper. Construct (or load) a body with two
same-named blocks where one is snapshot-disabled and one is snapshot-param-
overridden, decompile it, and assert the emitted spec uses the coordinate list
form and that `parse_spec` accepts it.

```python
def test_snapshot_decompile_emits_coordinates_when_ambiguous(dup_named_snapshot_body, real_lib):
    from helixgen.decompile import decompile_body
    from helixgen.spec import parse_spec
    spec = decompile_body(dup_named_snapshot_body, real_lib)
    snap = spec["snapshots"][0]
    # ambiguous name -> list form with coordinates, not a bare dict
    assert isinstance(snap.get("params"), list)
    assert all("lane" in e and "pos" in e for e in snap["params"])
    parse_spec(spec)  # must round-trip through the parser

def test_snapshot_decompile_stays_dict_when_unambiguous(single_named_snapshot_body, real_lib):
    from helixgen.decompile import decompile_body
    spec = decompile_body(single_named_snapshot_body, real_lib)
    # unambiguous -> current dict form preserved (backward compatible)
    assert isinstance(spec["snapshots"][0].get("params"), dict)
```

Fixtures: if `dup_named_snapshot_body` / `single_named_snapshot_body` do not
exist, build them inline from a minimal two-path body with a split (lane-1
duplicate), following the body-construction pattern in the existing decompile
tests. If a real export with duplicate-named snapshot refs is present in `data/`
(e.g. `Bringin Plexi Back.hsp`), a skip-if-absent integration assertion is
acceptable in addition, but the primary test must be synthetic so it runs on a
clean clone.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -k snapshot_decompile -v`
Expected: FAIL — `_recover_snapshots` takes 2 args / emits only the dict form.

- [ ] **Step 3: Implement**

In `decompile_body`, move `idx = _name_index(flow, library)` to just after `flow`
is computed (before the `_recover_snapshots` call) and pass it:

```python
    idx = _name_index(flow, library)
    snaps = _recover_snapshots(body, library, idx)
    if snaps:
        spec["snapshots"] = snaps
    ...
    fs = _recover_footswitches(body, library, device_id, idx)
```

Rewrite `_recover_snapshots` to accumulate coordinate-keyed overrides and emit
per-snapshot:

```python
def _recover_snapshots(body: dict, library: Library, idx: dict) -> list[dict[str, Any]]:
    names = _snapshot_names(body)
    if not names:
        return []
    # per-snapshot accumulators keyed by (pi, lane, pos, name)
    disables: list[list[tuple]] = [[] for _ in names]
    params:   list[dict[tuple, dict]] = [{} for _ in names]
    flow = (body.get("preset") or {}).get("flow") or []
    for pi, key, bnn, slot in _iter_blocks(flow):
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        name = block.display_name
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        coord = (pi, lane, pos, name)
        en = bnn.get("@enabled")
        if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
            for i, ov in enumerate(en["snapshots"]):
                if i < len(names) and ov is False:
                    disables[i].append(coord)
        for pname, wrapped in (slot.get("params") or {}).items():
            if not (isinstance(wrapped, dict) and isinstance(wrapped.get("snapshots"), list)):
                continue
            for i, ov in enumerate(wrapped["snapshots"]):
                if i < len(names) and ov is not None:
                    params[i].setdefault(coord, {})[pname] = _coerce_param_value(block, pname, ov)

    snaps: list[dict[str, Any]] = []
    for i, nm in enumerate(names):
        s: dict[str, Any] = {"name": nm}
        # disable: bare string if unambiguous, else coordinate dict
        dis = []
        for (pi, lane, pos, name) in disables[i]:
            r = _ref(name, pi, lane, pos, idx)
            dis.append(name if len(r) == 1 else r)
        if dis:
            s["disable"] = dis
        # params: dict form if every param-block unambiguous, else list form
        if params[i]:
            ambiguous = any(len(idx.get(name, [])) > 1 for (_, _, _, name) in params[i])
            if ambiguous:
                s["params"] = [
                    {**_ref(name, pi, lane, pos, idx), "params": pv}
                    for (pi, lane, pos, name), pv in params[i].items()
                ]
            else:
                s["params"] = {name: pv for (_, _, _, name), pv in params[i].items()}
        snaps.append(s)
    return snaps
```

NOTE on dense arrays (Task 1): because generate now densifies, a base-valued
snapshot slot equals the base value, not `null`. `_recover_snapshots` must only
record a *disable* when `ov is False` and a *param override* when the value
differs from the block's base — the existing code already keys off
`ov is False` / `ov is not None`. If Task 1's densify causes a base-valued slot
to be wrongly recovered as an override, filter it: compare `ov` to the slot's
base (`_unwrap_value(slot['@enabled'])` for enabled; the block exemplar / base
param for params) and skip equal values. Add a regression test if this bites.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -k snapshot -v`
Expected: PASS.

- [ ] **Step 5: Full decompile + generate round-trip suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py tests/test_generate*.py tests/test_spec.py -q`
Expected: PASS (0 failures).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(decompile): emit coordinate-aware snapshot refs for duplicate-named blocks"
```

---

### Task 5: Document the snapshot coordinate forms

**Files:**
- Modify: `CLAUDE.md` (the "Optional: snapshots" section)

**Interfaces:** none (docs only).

- [ ] **Step 1: Update the snapshots section**

In `CLAUDE.md`, under "Optional: snapshots (Stadium scenes)", after the existing
example, add:

```markdown
When a snapshot references a block whose display name is ambiguous (multiple
placed blocks humanize to the same name, e.g. two "Stereo" blocks across a
split), carry a `(lane, pos)` coordinate:

- `disable` entries may be objects instead of bare strings:
  `"disable": [{"block": "Stereo", "lane": 1, "pos": 2}]`
- `params` may be a list instead of a name-keyed object:
  `"params": [{"block": "Stereo", "lane": 1, "pos": 2, "params": {"Mix": 0.3}}]`

Coordinates are only needed to disambiguate; the bare string / name-keyed object
forms remain valid for uniquely-named blocks. `path` (0 or 1) is added only when
the same name is ambiguous across both DSP paths.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md && git commit -m "docs: coordinate-aware snapshot references in CLAUDE.md"
```

---

### Task 6: IR block with no assigned IR (Category 3)

Some `HX2_ImpulseResponse*` slots carry no `irhash`. Decompile omits `ir`, so
`generate._resolve_irhash` raises "IR block requires an `ir` field". Add an
explicit "no IR" marker so round-trip works while a genuine user omission still
errors.

**Files:**
- Modify: `src/helixgen/spec.py` (`BlockEntry` ~line 12-19; block parser to read
  `no_ir`)
- Modify: `src/helixgen/generate.py` (`_resolve_irhash` ~line 38-73 and its call
  site ~line 779-780; thread a `no_ir` flag so it returns `None` → no irhash key)
- Modify: `src/helixgen/decompile.py` (`_block_entry` ~line 281-293)
- Test: `tests/test_generate*.py`, `tests/test_decompile*.py`, `tests/test_spec.py`

**Interfaces:**
- Produces: `BlockEntry.no_ir: bool = False`. JSON key `"no_ir": true`. When set,
  generate emits the IR slot with NO `irhash` key. Decompile sets it for IR slots
  whose source had no `irhash`.

- [ ] **Step 1: Write the failing tests**

```python
# spec
def test_block_entry_parses_no_ir():
    from helixgen.spec import parse_spec
    s = parse_spec({"name": "P", "paths": [{"blocks": [
        {"block": "With Pan", "no_ir": True}]}]})
    assert s.paths[0].blocks[0].no_ir is True

# generate: no_ir block emits a slot without irhash and does not raise
def test_generate_no_ir_block_omits_irhash(real_lib_with_ir_block):
    # compose a spec with an IR block flagged no_ir; assert the emitted slot
    # has no "irhash" key and generation does not raise.
    ...

# decompile: an IR slot with no irhash round-trips to no_ir=True
def test_decompile_ir_without_irhash_sets_no_ir(ir_no_hash_body, real_lib):
    from helixgen.decompile import decompile_body
    spec = decompile_body(ir_no_hash_body, real_lib)
    entry = spec["paths"][0]["blocks"][... the IR block ...]
    assert entry.get("no_ir") is True and "ir" not in entry
```

For the generate/decompile fixtures, reuse the IR-block library the existing IR
tests build. If `data/A like supreme.hsp` is present, add a skip-if-absent
integration check that it now round-trips; the primary tests must be synthetic.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src pytest tests/ -k no_ir -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`spec.py` — add field + parse:

```python
@dataclass
class BlockEntry:
    block: str
    params: dict[str, Any] = field(default_factory=dict)
    ir: str | None = None
    no_ir: bool = False
    enabled: bool | None = None
    lane: int = 0
    pos: int | None = None
```

In the block-entry parser (where `ir` is read), add:

```python
    no_ir = data.get("no_ir", False)
    if not isinstance(no_ir, bool):
        raise _err(source, '"no_ir" must be a boolean.')
```

and pass `no_ir=no_ir` into the `BlockEntry(...)` construction. Guard against the
contradiction: if both `ir` and `no_ir` are set, raise
`_err(source, 'set at most one of "ir" / "no_ir".')`.

`generate.py` — thread the flag. At the IR resolution call site (~779), when the
entry's `no_ir` is set, skip `_resolve_irhash` and use `None`:

```python
            resolved_irhash = None
            if block.model_id.startswith(IR_MODEL_PREFIX) and not getattr(entry, "no_ir", False):
                resolved_irhash = _resolve_irhash(block.default_irhash, entry.ir, irs)
```

(Confirm the exact existing variable names at that call site; keep them.) The
existing `if irhash is not None:` guard at ~450 already omits the key when `None`.

`decompile.py` — in `_block_entry`, after the existing `if model.startswith(
IR_MODEL_PREFIX) and slot.get("irhash"):` block, add:

```python
    elif model.startswith(IR_MODEL_PREFIX):
        entry["no_ir"] = True
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src pytest tests/ -k no_ir -v`
Expected: PASS.

- [ ] **Step 5: Full suite for the touched modules**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py tests/test_generate*.py tests/test_decompile*.py tests/test_ir*.py -q`
Expected: PASS (0 failures).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: round-trip IR blocks with no assigned IR (no_ir marker)"
```

---

### Task 7: Minor — footswitch/expression refs never emit an empty block name

Decompile emits `{"switch": "FS9", "block": ""}` when a controlled block's
`display_name` is empty, producing an unparseable spec. Fall back to `model_id`,
mirroring `_block_entry`.

**Files:**
- Modify: `src/helixgen/decompile.py` (`_name_index` ~line 69-89, `_recover_
  footswitches` ~line 148-163, `_recover_expression` ~line 166-183, and/or `_ref`
  ~line 92-100)
- Test: `tests/test_decompile*.py`

**Interfaces:**
- Produces: a shared `_ref_name(block) -> str` that returns `block.display_name`
  when non-empty else `block.model_id`; used by `_name_index`, `_recover_
  footswitches`, `_recover_expression`. The `idx` must key on the SAME name that
  `_ref` emits, so ambiguity detection stays consistent.

- [ ] **Step 1: Write the failing test**

```python
def test_footswitch_ref_falls_back_to_model_id_when_name_empty(empty_name_fs_body, real_lib):
    from helixgen.decompile import decompile_body
    from helixgen.spec import parse_spec
    spec = decompile_body(empty_name_fs_body, real_lib)
    for fs in spec.get("footswitches", []):
        assert fs["block"]  # never empty
    parse_spec(spec)  # must parse
```

Build `empty_name_fs_body` from a block whose library `display_name` is empty
with a `targetbypass` controller on it. If `data/BAS_Drip Pro.hsp` is present,
add a skip-if-absent assertion that it round-trips.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -k empty_name -v`
Expected: FAIL — a footswitch has `block == ""`.

- [ ] **Step 3: Implement**

Add near the top of `decompile.py`:

```python
def _ref_name(block) -> str:
    """Display name when non-empty, else model_id — never empty."""
    return block.display_name or block.model_id
```

Use `_ref_name(block)` everywhere `block.display_name` currently feeds a
reference: in `_name_index` (`idx[name]` key), `_recover_footswitches`,
`_recover_expression`, and `_recover_snapshots` (Task 4). `idx` and `_ref` must
agree on the name, so route both through `_ref_name`.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -k empty_name -v`
Expected: PASS.

- [ ] **Step 5: Full decompile suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "fix(decompile): fall back to model_id for empty block display names"
```

---

### Task 8: Minor — expression recovery skips out-of-scope controllers

Decompile emits expression targets for footswitch-as-parameter controllers
(`"pedal": "FS9"`) and bool-typed params (`"min": false, "max": true`), which the
parser rejects. v1 expression sweeps are EXP1/EXP2 with numeric (non-bool)
min/max only. Skip the rest with a warning.

**Files:**
- Modify: `src/helixgen/decompile.py` (`_recover_expression` ~line 166-183)
- Test: `tests/test_decompile*.py`

**Interfaces:**
- Produces: `_recover_expression` returns only targets whose pedal is `EXP1`/
  `EXP2` and whose `min`/`max` are numeric and not `bool`. Skipped controllers
  print a `warning:` to stderr and do not appear in the spec.

- [ ] **Step 1: Write the failing test**

```python
def test_expression_recovery_skips_bool_and_non_exp(mixed_controller_body, real_lib):
    from helixgen.decompile import decompile_body
    from helixgen.spec import parse_spec
    spec = decompile_body(mixed_controller_body, real_lib)
    for a in spec.get("expression", []):
        assert a["pedal"] in ("EXP1", "EXP2")
        for t in a["targets"]:
            assert not isinstance(t["min"], bool) and isinstance(t["min"], (int, float))
    parse_spec(spec)  # must parse
```

Build `mixed_controller_body` with one EXP2 float sweep plus an FS9 bool
controller (as in `data/BAS_Goliathan.hsp`). Skip-if-absent integration check on
that file if present.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -k skips_bool -v`
Expected: FAIL — an `FS9` pedal / bool min appears, or `parse_spec` raises.

- [ ] **Step 3: Implement**

In `_recover_expression`, inside the target loop, after computing `pedal` and the
`min`/`max` values:

```python
            pedal = controllers.controller_name_for_source(device_id, ctrl.get("source"))
            if pedal not in ("EXP1", "EXP2"):
                continue
            lo, hi = ctrl.get("min", 0.0), ctrl.get("max", 1.0)
            def _numeric(x):
                return isinstance(x, (int, float)) and not isinstance(x, bool)
            if not (_numeric(lo) and _numeric(hi)):
                print(f"warning: skipping expression target on {block.display_name!r}."
                      f"{pname!r}: non-numeric sweep range ({lo!r}..{hi!r}) unsupported in v1.",
                      file=sys.stderr)
                continue
```

Ensure `import sys` is present at module top (add if missing).

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -k skips_bool -v`
Expected: PASS.

- [ ] **Step 5: Full decompile suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile*.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "fix(decompile): skip non-EXP / bool-range expression controllers (v1 scope)"
```

---

### Task 9: Re-measure the scoreboard and refresh the xfail reason

**Files:**
- Modify: `tests/test_decompile_acceptance.py` (xfail `reason` only)
- Run: the full suite + the acceptance scoreboard

**Interfaces:** none.

- [ ] **Step 1: Run the whole suite**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: 0 failures (the acceptance test remains `xfail`/`xpass`).

- [ ] **Step 2: Measure the new round-trip pass rate**

Run this one-off (records the bucket counts):

```bash
PYTHONPATH=$PWD/src python - <<'PY'
from pathlib import Path
from collections import Counter
import tempfile
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp
from helixgen.decompile import decompile_body
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.ir import IrMapping
samples = sorted(Path("data").glob("*.hsp"))
lib = Library(root=Path(tempfile.mkdtemp())/"lib")
for s in samples: ingest_path(s, lib)
irs = IrMapping.load()
def models(b):
    out=[]
    for path in (b.get("preset") or {}).get("flow") or []:
        for k in sorted(path):
            if k.startswith("b") and k not in ("b00","b13") and k[1:].isdigit():
                out.append(path[k].get("slot",[{}])[0].get("model"))
    return out
ok=0; fails=Counter()
for s in samples:
    try:
        b=read_hsp(s); spec=parse_spec(decompile_body(b,lib,irs=irs))
        regen=compose_preset(spec,lib,source=str(s),irs=irs)
        assert models(regen)==models(b); ok+=1
    except Exception as e:
        fails[f"{type(e).__name__}: {str(e)[:60]}"]+=1
print(f"PASS {ok}/{len(samples)}")
for sig,n in fails.most_common(): print(f"{n:3d}  {sig}")
PY
```

Record the `PASS n/211` number in the task's completion note.

- [ ] **Step 3: Update the xfail reason**

Edit the `reason=` string in `test_decompile_acceptance.py` to state the new pass
rate and the remaining categories (P35 branch-lane I/O is the primary one left;
plus any residual buckets the measurement surfaces). Keep `strict=False`. Do NOT
tighten to a full-body `strip_provenance` compare yet — that waits until the P35
category (its own follow-up) lands, else the test cannot XPASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_decompile_acceptance.py && git commit -m "test(decompile): refresh round-trip scoreboard reason after snapshot/IR fidelity"
```

---

## Notes for the executor

- After each task, run `git log main --oneline -1` and confirm it still points at
  `d4f97aa` (or whatever `main` was at plan start) — subagents in worktrees have
  previously leaked a commit onto `main`. All commits belong on
  `hardening/snapshot-coordinate-refs`.
- Deferred to a separate spec+plan (NOT in scope here): Category 2 (P35 branch-lane
  I/O structural passthrough), dense arrays for non-varying blocks, and tightening
  the acceptance test to a full-body compare. These need their own design + a
  hardware check.
- Hardware follow-up (manual, after Task 1): regenerate `thunder-kiss-65.hsp` with
  the flanger `disable` moved to the Lead snapshot and confirm reliable recall on
  the Stadium XL.
