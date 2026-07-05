# Category 5 — bypass fidelity + dual-cab / verbatim state Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a decompiled→regenerated `.hsp` preset a sonic clone of its source by (1) round-tripping each block's base bypass at the correct `bNN.@enabled.value` level and (2) preserving the per-block verbatim state generate drops today — the second cab slot, the `harness` dict, and `favorite`.

**Architecture:** Two independent code paths in the same three modules. Item #1 moves the base bypass read (`decompile._block_entry`) and write (`generate._to_hsp_bnn`) from the inert slot-level `@enabled` to the authoritative `bNN`-level `@enabled`, keeping the per-snapshot `None`→`True` fill decoupled from the base value. Item #3 adds one optional verbatim `raw` field to `spec.BlockEntry` (`{harness, slots}`), captured in decompile and re-emitted in generate. A new corpus scoreboard test asserts per-block sonic state.

**Tech Stack:** Python 3, pure stdlib + `click`. Tests: `pytest` + `stretchr`-style asserts (plain `assert`). Run everything with `PYTHONPATH=$PWD/src`.

## Global Constraints

- Run tests with `PYTHONPATH=$PWD/src pytest` (an editable install may shadow the tree otherwise).
- Pure stdlib + `click` only — no new runtime dependencies.
- TDD throughout: failing test first, minimal implementation, green, commit.
- All work stays on branch `hardening/category5-bypass-and-dualcab` (already created). Never commit to `main`.
- Real-export fixtures live in `data/*.hsp` (gitignored, present on this machine); tests over them must `pytest.skip` when absent so a clean clone stays green.
- Spec of record: `docs/superpowers/specs/2026-07-05-category5-bypass-and-dualcab-design.md`.

---

### Task 1: Generate — write base bypass to the `bNN` level (item #1 write side)

Move the base bypass from the slot-level `@enabled` (inert on Stadium) to the `bNN`-level `@enabled.value`. Keep the slot at the exemplar `True`. Keep the per-snapshot enabled array's `None`→`True` fill, decoupled from the base value (an unset snapshot is *enabled* regardless of the base). This is the audio fix: the device reads bypass at the `bNN` level, so a base-bypassed block currently loads enabled.

**Files:**
- Modify: `src/helixgen/generate.py` — `_to_hsp_bnn` (lines ~442-488)
- Modify (assertion moves level): `tests/test_patch_cli.py` — `test_cli_disable_block` (~line 45)
- Test: `tests/test_generate.py` (new test function)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_to_hsp_bnn(...)` now emits `bNN["@enabled"] == {"value": <base>, ...}` where `<base>` is `enabled_base` (default `True`), and `slot[0]["@enabled"] == {"value": <exemplar @enabled, ~True>}`. The enabled snapshots array (when present) fills unset slots with `True`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_generate.py` (uses the existing `Library`/`_populate_hsp_library` fixtures already in that file — mirror an existing hsp test's setup):

```python
def test_hsp_base_bypass_lives_at_bnn_level(tmp_library, tmp_path):
    """A block with `enabled: false` must bypass at the bNN level (device reads
    that), while the slot-level @enabled stays the inert exemplar True."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [{"block": "Brit 2204", "enabled": False}]}],
    }, source="t.json")
    preset = compose_preset(spec, lib, source="t.json")
    b01 = preset["preset"]["flow"][0]["b01"]
    assert b01["@enabled"]["value"] is False           # bNN: the real bypass
    assert b01["slot"][0]["@enabled"] == {"value": True}  # slot: inert exemplar


def test_hsp_base_bypass_keeps_true_snapshot_fill(tmp_library, tmp_path):
    """base False + a disable in snapshot 0 must still leave snapshots 1..7
    enabled (True fill), decoupled from the base value."""
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [{"block": "Brit 2204", "enabled": False}]}],
        "snapshots": [
            {"name": "A", "disable": ["Brit 2204"]},
            {"name": "B"},
        ],
    }, source="t.json")
    preset = compose_preset(spec, lib, source="t.json")
    en = preset["preset"]["flow"][0]["b01"]["@enabled"]
    assert en["value"] is False
    assert en["snapshots"] == [False, True, True, True, True, True, True, True]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_hsp_base_bypass_lives_at_bnn_level tests/test_generate.py::test_hsp_base_bypass_keeps_true_snapshot_fill -v`
Expected: FAIL — `b01["@enabled"]["value"]` is currently hardcoded `True` and the slot carries the base.

- [ ] **Step 3: Implement the minimal change in `_to_hsp_bnn`**

In `src/helixgen/generate.py`, change the slot-level `@enabled` (currently `slot_inner["@enabled"] = {"value": base_enabled}`) to always use the exemplar value, keeping `base_enabled` for the bNN level:

```python
    base_enabled = enabled_base if enabled_base is not None else flat.get("@enabled", True)
    # Slot-level @enabled is inert on Stadium; the device reads bypass at the
    # bNN level (see the bNN @enabled built below). Keep the slot at the
    # exemplar value (~always True).
    slot_inner["@enabled"] = {"value": flat.get("@enabled", True)}
```

Then replace the bNN-level enabled wrapper (currently `enabled_wrapped = _wrap_value_with_snapshots(True, enabled_overrides)` followed by the `fs_controller` attach) with an explicit build that puts `base_enabled` at `value` but keeps a `True` fill:

```python
    # bNN-level @enabled carries the real base bypass value. The per-snapshot
    # array fills unset slots with True (an unset snapshot is enabled,
    # independent of the base) — do NOT reuse _wrap_value_with_snapshots here,
    # which would fill with base_enabled and wrongly bypass enabled snapshots.
    enabled_wrapped: dict[str, Any] = {"value": base_enabled}
    if enabled_overrides and any(o is not None for o in enabled_overrides):
        enabled_wrapped["snapshots"] = [
            True if o is None else o for o in enabled_overrides
        ]
    if fs_controller is not None:
        enabled_wrapped["controller"] = fs_controller
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_hsp_base_bypass_lives_at_bnn_level tests/test_generate.py::test_hsp_base_bypass_keeps_true_snapshot_fill -v`
Expected: PASS.

- [ ] **Step 5: Fix the pre-existing patch-CLI test whose assertion moved level**

`tests/test_patch_cli.py::test_cli_disable_block` asserts the base bypass at the old slot level; it must move to the bNN level. Change the assertion (around line 45) from:

```python
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["@enabled"]["value"] is False
```

to:

```python
    assert body["preset"]["flow"][0]["b01"]["@enabled"]["value"] is False
```

- [ ] **Step 6: Run the full suite to confirm no regressions**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: PASS (0 failures). In particular `test_generate.py` snapshot tests (`test_compose_preset_hsp_disable_emits_bnn_snapshots_array`, `..._undisabled_block_has_no_snapshots_array`, `:437`, `:476`), `test_patch_cli.py`, and `test_decompile*` stay green.

- [ ] **Step 7: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py tests/test_patch_cli.py
git commit -m "fix(generate): write base bypass to bNN @enabled, slot stays inert True

The device reads block bypass at the bNN level; generate hardcoded bNN.value
to True and put the base bypass on the inert slot level, so base-bypassed
blocks loaded enabled. Route enabled_base to bNN.@enabled.value; keep the
per-snapshot None->True fill decoupled from the base. Category 5 item #1 (write).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 2: Decompile — read base bypass from the `bNN` level (item #1 read side)

`_block_entry` reads the base bypass from `slot[0]["@enabled"]` (~always `True`), so base-bypassed blocks decompile as enabled. Read it from the `bNN`-level `@enabled` instead. `_block_entry`'s only caller is `_entry_for`, which already holds the `bnn` dict — thread it through.

**Files:**
- Modify: `src/helixgen/decompile.py` — `_entry_for` (~line 291), `_block_entry` (signature + base read, ~lines 349-403), stale comment (~lines 179-181)
- Test: `tests/test_decompile.py` (new test function)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_block_entry(bnn: dict, library, irs)` (was `_block_entry(slot, library, irs)`) — reads `slot = bnn["slot"][0]` internally and the base bypass from `_unwrap_value(bnn.get("@enabled", True))`. Emits `enabled: false` when the base differs from the exemplar baseline (`True`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_decompile.py` (uses the `hsp_library` fixture already used there):

```python
def test_decompile_reads_base_bypass_from_bnn_level(hsp_library):
    """A block bypassed at the bNN level (slot level inert True) decompiles to
    enabled: false."""
    from helixgen.decompile import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    model_id = block.model_id  # ingest-time hsp model id round-trips via translate
    bnn = {
        "@enabled": {"value": False},                 # bNN: real bypass
        "type": "fx", "position": 1, "path": 0,
        "slot": [{"model": model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    assert entry["enabled"] is False
```

Note: if `Tube Drive`'s hsp model id needs translating, mirror how existing
`test_decompile.py` builds slots (the fixture blocks already round-trip). If
`find_block`/model lookup differs in this fixture, use the same `model` string
an existing passing decompile test in this file uses for `Tube Drive`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py::test_decompile_reads_base_bypass_from_bnn_level -v`
Expected: FAIL — `_block_entry` currently takes `slot` (not `bnn`) and reads the slot-level `@enabled` (True), so `entry` has no `enabled` key → `KeyError`/assert fail.

- [ ] **Step 3: Change `_entry_for` to pass `bnn`**

In `src/helixgen/decompile.py`, `_entry_for`, change the block branch from `entry = _block_entry(slot, library, irs)` to:

```python
    else:
        entry = _block_entry(bnn, library, irs)
```

- [ ] **Step 4: Change `_block_entry` signature and base read**

Change the signature and the first lines from:

```python
def _block_entry(slot: dict, library: Library, irs: IrMapping | None) -> dict[str, Any]:
    ...
    model = _translate_model_id(slot.get("model", ""))
```

to take the `bnn` and derive `slot` from it:

```python
def _block_entry(bnn: dict, library: Library, irs: IrMapping | None) -> dict[str, Any]:
    ...
    slot = bnn["slot"][0]
    model = _translate_model_id(slot.get("model", ""))
```

Then change the base-enabled read (currently `base_enabled = _unwrap_value(slot.get("@enabled", True))`) to read the bNN level:

```python
    # Base bypass lives at the bNN level (the device reads it there); the slot
    # level is inert (~always True). See generate._to_hsp_bnn.
    base_enabled = _unwrap_value(bnn.get("@enabled", True))
    exemplar_enabled = block.exemplar.get("@enabled", True)
    if base_enabled != exemplar_enabled:
        entry["enabled"] = base_enabled
```

- [ ] **Step 5: Correct the stale comment in `_recover_snapshots`**

Around lines 179-181 the comment claims "The base bNN-level `@enabled` is always True (generate never densifies it to anything else)." That is no longer true. Replace with:

```python
        # @enabled snapshot overrides (False => disable in that snapshot). The
        # bNN base @enabled.value may now be False (base-bypassed block), but
        # disable-recovery keys only off explicit `snapshots[i] is False`, never
        # the base, so base bypass and per-snapshot bypass stay independent.
```

- [ ] **Step 6: Run the new test + full suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py::test_decompile_reads_base_bypass_from_bnn_level -v && PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures (`test_decompile_roundtrip_stable`, `test_decompile_advanced.py`, acceptance test all green).

- [ ] **Step 7: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile.py
git commit -m "fix(decompile): read base bypass from bNN @enabled, not the inert slot

_block_entry read slot[0].@enabled (~always True), so base-bypassed blocks
decompiled as enabled. Thread the bnn dict through _entry_for and read
bNN.@enabled.value. Category 5 item #1 (read). Corrects a stale invariant
comment in _recover_snapshots.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 3: Spec — add the optional `raw` verbatim field to `BlockEntry`

Add one optional field, `raw`, to `BlockEntry` for verbatim non-modeled bNN state (`{harness, slots}`), and parse/validate it. No behavior yet — just the data model + validation.

**Files:**
- Modify: `src/helixgen/spec.py` — `BlockEntry` dataclass (~line 12), `_parse_path_entry` block branch (~lines 448-467)
- Test: `tests/test_spec.py` (new test functions)

**Interfaces:**
- Consumes: nothing.
- Produces: `BlockEntry.raw: dict[str, Any] | None = None`. `parse_spec` accepts a block-entry `"raw"` object with optional `"harness"` (dict) and `"slots"` (list of dicts); rejects malformed shapes with `SpecError`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_spec.py`:

```python
def test_parse_block_raw_field_roundtrips():
    from helixgen.spec import parse_spec, BlockEntry
    spec = parse_spec({"name": "S", "paths": [{"blocks": [
        {"block": "X", "raw": {
            "harness": {"@enabled": {"value": True}, "params": {}},
            "slots": [{"model": "HD2_CabMicIr_NoCab", "params": {}}],
        }},
    ]}]})
    entry = spec.paths[0].blocks[0]
    assert isinstance(entry, BlockEntry)
    assert entry.raw["harness"]["@enabled"]["value"] is True
    assert entry.raw["slots"][0]["model"] == "HD2_CabMicIr_NoCab"


def test_parse_block_raw_absent_is_none():
    from helixgen.spec import parse_spec
    spec = parse_spec({"name": "S", "paths": [{"blocks": [{"block": "X"}]}]})
    assert spec.paths[0].blocks[0].raw is None


def test_parse_block_raw_rejects_non_object():
    import pytest
    from helixgen.spec import parse_spec, SpecError
    with pytest.raises(SpecError):
        parse_spec({"name": "S", "paths": [{"blocks": [
            {"block": "X", "raw": ["not", "an", "object"]}]}]})


def test_parse_block_raw_rejects_bad_slots():
    import pytest
    from helixgen.spec import parse_spec, SpecError
    with pytest.raises(SpecError):
        parse_spec({"name": "S", "paths": [{"blocks": [
            {"block": "X", "raw": {"slots": [1, 2, 3]}}]}]})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -k raw -v`
Expected: FAIL — `BlockEntry` has no `raw` field; the arg is dropped/ignored and the reject tests don't raise.

- [ ] **Step 3: Add the `raw` field to `BlockEntry`**

In `src/helixgen/spec.py`, add to the `BlockEntry` dataclass:

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
    raw: dict[str, Any] | None = None
```

- [ ] **Step 4: Parse + validate `raw` in `_parse_path_entry`**

In the plain-block branch of `_parse_path_entry` (after `enabled = data.get("enabled")` validation, before `lane, pos = _parse_lane_pos(...)`), add:

```python
    raw = data.get("raw")
    if raw is not None:
        if not isinstance(raw, dict):
            raise _err(source, '"raw" must be an object if provided.')
        if "harness" in raw and not isinstance(raw["harness"], dict):
            raise _err(source, '"raw.harness" must be an object if provided.')
        if "slots" in raw and not (
            isinstance(raw["slots"], list)
            and all(isinstance(s, dict) for s in raw["slots"])
        ):
            raise _err(source, '"raw.slots" must be a list of objects if provided.')
```

and pass it into the returned `BlockEntry`:

```python
    return BlockEntry(block=name, params=dict(params), ir=ir, no_ir=no_ir,
                      enabled=enabled, lane=lane, pos=pos, raw=raw)
```

- [ ] **Step 5: Run the new tests + full suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -k raw -v && PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec.py
git commit -m "feat(spec): optional BlockEntry.raw for verbatim bNN state (harness/slots)

Data model + validation for the verbatim per-block passthrough used by
dual-cab / harness preservation (Category 5 item #3). No behavior yet.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 4: Generate — emit `raw` (harness + extra slots) and `favorite`

Re-emit the verbatim `raw` state onto the generated bNN: attach `harness`, extend the `slot` array with `raw.slots`, and always set `favorite: 0` (constant across the corpus). Depends on Task 3 (`BlockEntry.raw`).

**Files:**
- Modify: `src/helixgen/generate.py` — `_to_hsp_bnn` signature + bNN assembly (~lines 418-488), the `_to_hsp_bnn(...)` call site (~lines 818-832)
- Test: `tests/test_generate.py` (new test function)

**Interfaces:**
- Consumes: `BlockEntry.raw` (Task 3).
- Produces: `_to_hsp_bnn(..., raw: dict | None = None)`. When `raw["harness"]` is a dict → `bNN["harness"]` is a deepcopy of it. When `raw["slots"]` is a list → appended (deepcopy) after `slot[0]`. `bNN["favorite"] == 0` always.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_generate.py`:

```python
def test_hsp_emits_raw_harness_slots_and_favorite(tmp_library, tmp_path):
    lib = Library(tmp_library)
    _populate_hsp_library(lib, tmp_path)
    spec = parse_spec({
        "name": "S",
        "paths": [{"blocks": [
            {"block": "4x12 Greenback 25", "raw": {
                "harness": {"@enabled": {"value": True},
                            "params": {"dual": {"value": True}}},
                "slots": [{"model": "HD2_CabMicIr_NoCab", "params": {}}],
            }},
        ]}],
    }, source="t.json")
    preset = compose_preset(spec, lib, source="t.json")
    bnn = preset["preset"]["flow"][0]["b01"]
    assert bnn["favorite"] == 0
    assert bnn["harness"]["params"]["dual"]["value"] is True
    assert len(bnn["slot"]) == 2
    assert bnn["slot"][1]["model"] == "HD2_CabMicIr_NoCab"
```

(Use a cab block name that exists in `_populate_hsp_library`; if `4x12 Greenback 25` is not populated there, use whatever cab that fixture registers — check the top of `tests/test_generate.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_hsp_emits_raw_harness_slots_and_favorite -v`
Expected: FAIL — `_to_hsp_bnn` has no `raw` param and emits neither `favorite`, `harness`, nor a second slot.

- [ ] **Step 3: Add the `raw` parameter and emission to `_to_hsp_bnn`**

Add `raw: dict[str, Any] | None = None` to the `_to_hsp_bnn` keyword-only signature (after `irhash`). Then change the final bNN assembly (currently building the `bnn` dict and `return bnn`) to include `favorite` and merge `raw`:

```python
    bnn: dict[str, Any] = {
        "@enabled": enabled_wrapped,
        "type": flat.get("@type", _hsp_type_for_block(block)),
        "position": position,
        "path": path_index,
        "favorite": 0,
        "slot": [slot_inner],
    }
    if raw:
        harness = raw.get("harness")
        if isinstance(harness, dict):
            bnn["harness"] = copy.deepcopy(harness)
        extra_slots = raw.get("slots")
        if isinstance(extra_slots, list):
            bnn["slot"].extend(copy.deepcopy(s) for s in extra_slots)
    return bnn
```

- [ ] **Step 4: Pass `raw` from the placement loop**

At the `_to_hsp_bnn(...)` call site (in `_compose_preset_hsp`), add the `raw` argument after `irhash=resolved_irhash,`:

```python
                irhash=resolved_irhash,
                raw=block_entry.raw,
            )
```

- [ ] **Step 5: Run the new test + full suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py::test_hsp_emits_raw_harness_slots_and_favorite -v && PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures. (Existing full-body round-trip tests like `test_decompile_roundtrip_stable` stay green: they generate from specs with no `raw`, so both sides just gain a constant `favorite: 0`.)

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate.py
git commit -m "feat(generate): emit verbatim raw harness + extra slots + favorite

_to_hsp_bnn now re-attaches BlockEntry.raw (harness dict, extra slot[1:]) and
always emits favorite:0, so dual-cab second slots and per-block harness state
round-trip. Category 5 item #3 (write).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 5: Decompile — capture `raw` (harness + extra slots)

Populate `BlockEntry`'s spec-dict with a `raw` object carrying the verbatim `harness` (whenever present — all corpus blocks have one) and any extra slots (`slot[1:]`, for dual-slot blocks). Depends on Task 2 (`_block_entry` now takes `bnn`).

**Files:**
- Modify: `src/helixgen/decompile.py` — `_block_entry` (add raw capture before `return entry`)
- Test: `tests/test_decompile.py` (new test function)

**Interfaces:**
- Consumes: `bnn` (already threaded in Task 2).
- Produces: `_block_entry` output dict includes `"raw": {"harness": <verbatim>, "slots": <verbatim slot[1:]>}` when the source bNN has a `harness` and/or >1 slot. Emitted only with at least one sub-key.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_decompile.py`:

```python
def test_decompile_captures_harness_and_extra_slots(hsp_library):
    from helixgen.decompile import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    bnn = {
        "@enabled": {"value": True},
        "type": "fx", "position": 1, "path": 0,
        "harness": {"@enabled": {"value": True},
                    "params": {"Trails": {"value": True}}},
        "slot": [
            {"model": block.model_id, "@enabled": {"value": True}, "params": {}},
            {"model": "HD2_CabMicIr_NoCab", "@enabled": {"value": True}, "params": {}},
        ],
    }
    entry = _block_entry(bnn, lib, None)
    assert entry["raw"]["harness"]["params"]["Trails"]["value"] is True
    assert entry["raw"]["slots"][0]["model"] == "HD2_CabMicIr_NoCab"


def test_decompile_no_raw_when_no_harness_or_extra_slots(hsp_library):
    from helixgen.decompile import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    bnn = {
        "@enabled": {"value": True}, "type": "fx", "position": 1, "path": 0,
        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    assert "raw" not in entry
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py -k "harness_and_extra or no_raw_when" -v`
Expected: FAIL — `_block_entry` does not emit `raw` yet.

- [ ] **Step 3: Add raw capture to `_block_entry`**

In `src/helixgen/decompile.py`, `_block_entry`, just before `return entry`:

```python
    # Verbatim non-modeled bNN state generate would otherwise drop: the harness
    # dict (present on every real block, non-deterministic — Trails/dual/
    # ControlSource) and any extra slots (slot[1:], i.e. a dual cab).
    raw: dict[str, Any] = {}
    harness = bnn.get("harness")
    if isinstance(harness, dict):
        raw["harness"] = copy.deepcopy(harness)
    slots = bnn.get("slot") or []
    if len(slots) > 1:
        raw["slots"] = copy.deepcopy(slots[1:])
    if raw:
        entry["raw"] = raw
```

(`copy` is already imported at the top of `decompile.py`.)

- [ ] **Step 4: Run the new tests + full suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py -k "harness_and_extra or no_raw_when" -v && PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile.py
git commit -m "feat(decompile): capture verbatim raw harness + extra slots

_block_entry now emits raw.harness (verbatim, all blocks) and raw.slots
(slot[1:], dual cabs) so generate can re-emit them. Category 5 item #3 (read).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 6: Decompile — warn on the un-round-trippable Case-B block

A base-`False` block that is *enabled* in some named snapshot but has **no** `disable` (no explicit `False` in the named range) cannot round-trip under the disable-only model (0/211 in the corpus, but possible for authored/future presets). Emit a stderr warning naming the block. Depends on Task 2.

**Files:**
- Modify: `src/helixgen/decompile.py` — new helper `_warn_unrepresentable_enables`, called from `decompile_body`
- Test: `tests/test_decompile.py` (new test using `capsys`)

**Interfaces:**
- Consumes: `body`, `library` (+ `_snapshot_names`, `_iter_blocks` already in module).
- Produces: `_warn_unrepresentable_enables(body, library) -> None` — prints one `warning: ...` line to stderr per offending block. Called once inside `decompile_body`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_decompile.py`:

```python
def test_decompile_warns_on_unrepresentable_enable(hsp_library, capsys):
    """base=False + enabled in a named snapshot + NO disable => can't round-trip;
    decompile must warn."""
    from helixgen.decompile import decompile_body
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    body = {
        "meta": {"name": "T", "device_id": "stadium_xl"},
        "preset": {
            "snapshots": [{"name": "A"}, {"name": "B"}],  # 2 named
            "flow": [{
                "b00": {"@enabled": {"value": True}, "type": "input", "position": 0,
                        "path": 0, "endpoint": "b13",
                        "slot": [{"model": "P35_InputInst1", "@enabled": {"value": True}, "params": {}}]},
                "b01": {"@enabled": {"value": False,
                                     "snapshots": [True, True, None, None, None, None, None, None]},
                        "type": "fx", "position": 1, "path": 0,
                        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}]},
                "b13": {"@enabled": {"value": True}, "type": "output", "position": 13,
                        "path": 0, "endpoint": "b00",
                        "slot": [{"model": "P35_OutputPath2A", "@enabled": {"value": True}, "params": {}}]},
            }],
        },
    }
    decompile_body(body, lib, irs=None)
    err = capsys.readouterr().err
    assert "cannot round-trip" in err and "b01" in err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py::test_decompile_warns_on_unrepresentable_enable -v`
Expected: FAIL — no warning emitted.

- [ ] **Step 3: Add the helper + call it from `decompile_body`**

Add near the other `_recover_*` helpers in `src/helixgen/decompile.py`:

```python
def _warn_unrepresentable_enables(body: dict, library: Library) -> None:
    """Warn for a base-bypassed block that is enabled in a named snapshot but
    has NO disable (an explicit `False`) anywhere in the named range. The
    disable-only snapshot model cannot express that enable, so it will not
    round-trip until a snapshot enable-override lands. 0/211 in the corpus."""
    names = _snapshot_names(body)
    n = len(names)
    if n == 0:
        return
    flow = (body.get("preset") or {}).get("flow") or []
    for pi, key, bnn, slot in _iter_blocks(flow):
        base = _unwrap_value(bnn.get("@enabled", True))
        if base is not False:
            continue
        en = bnn.get("@enabled")
        arr = en.get("snapshots") if isinstance(en, dict) else None
        if not isinstance(arr, list):
            continue
        named = arr[:n]
        has_enable = any(v is True for v in named)
        has_disable = any(v is False for v in named)
        if has_enable and not has_disable:
            print(
                f"warning: block {slot.get('model')!r} at path {pi} {key} is "
                f"base-bypassed but enabled in a snapshot with no disable; this "
                f"cannot round-trip under the disable-only snapshot model "
                f"(will read bypassed in every snapshot).",
                file=sys.stderr,
            )
```

Then, inside `decompile_body`, after `idx = _name_index(flow, library)` (before the `snaps = _recover_snapshots(...)` line), call:

```python
    _warn_unrepresentable_enables(body, library)
```

(`sys` is already imported at the top of `decompile.py`.)

- [ ] **Step 4: Run the new test + full suite**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py::test_decompile_warns_on_unrepresentable_enable -v && PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures (the corpus never triggers the warning).

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile.py
git commit -m "feat(decompile): warn on un-round-trippable base-bypass+enable block

The disable-only snapshot model can't express 'enabled in snapshot' for a
base-bypassed block (Case B, 0/211 today). Warn if one is ever seen.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 7: Sonic-fidelity scoreboard test (corpus, 211/211)

Add the new acceptance test that round-trips every real export through one shared library and asserts per-user-block: base bypass value, effective per-snapshot bypass over named snapshots (with the source-null skip), all slot models, all slot param values, `harness`, and `favorite`. Depends on Tasks 1–5.

**Files:**
- Create: `tests/test_decompile_sonic_fidelity.py`

**Interfaces:**
- Consumes: `ingest_path`, `Library`, `read_hsp`, `decompile_body`, `compose_preset`, `parse_spec`, `IrMapping`, `decompile._snapshot_names`.
- Produces: one corpus test asserting 211/211 on the sonic-state comparator defined below.

- [ ] **Step 1: Write the test (it is the deliverable; it must pass once Tasks 1–5 are in)**

Create `tests/test_decompile_sonic_fidelity.py`:

```python
"""Sonic-fidelity acceptance test for the decompiler round-trip.

Skips on a clean clone with no data/*.hsp. Complements the model bar
(test_decompile_acceptance.py) by asserting each USER block's audible state:
base bypass, effective per-snapshot bypass over NAMED snapshots (source-null
cells skipped as undefined recall), every slot's model AND param values, the
verbatim harness, and favorite.

Deliberately NOT asserted (see the 2026-07-05 design spec): source-null named
snapshot cells (~30 presets, densified to True — Category-4-consistent),
redundant all-True snapshot arrays, unnamed trailing snapshot slots, top-level
sources/meta/xyctrl/snapshot metadata, and non-FS bypass-assign controllers.
"""
import pytest
from pathlib import Path
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp, _unwrap_value
from helixgen.decompile import decompile_body, _snapshot_names
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.ir import IrMapping


def _real_hsp_library(tmp_path):
    data_dir = Path(__file__).resolve().parent.parent / "data"
    samples = sorted(data_dir.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    lib = Library(root=tmp_path / "lib")
    for s in samples:
        ingest_path(s, lib)
    return lib, samples


def _user_blocks(body):
    """Yield (pi, key, bnn) for each user block (skip endpoints/split/join)."""
    for pi, path in enumerate((body.get("preset") or {}).get("flow") or []):
        if not isinstance(path, dict):
            continue
        for k in path:
            if not (isinstance(k, str) and k.startswith("b") and k[1:].isdigit()):
                continue
            bnn = path[k]
            if not isinstance(bnn, dict) or not bnn.get("slot"):
                continue
            if bnn.get("type") in ("input", "output", "split", "join"):
                continue
            yield pi, k, bnn


def _base(bnn):
    return _unwrap_value(bnn.get("@enabled", True))


def _snap_array(bnn):
    en = bnn.get("@enabled")
    return en.get("snapshots") if isinstance(en, dict) else None


def _effective(bnn, i):
    """Effective bypass in snapshot i: snapshots[i] if present-and-non-null,
    else the base value."""
    arr = _snap_array(bnn)
    if isinstance(arr, list) and i < len(arr) and arr[i] is not None:
        return arr[i]
    return _base(bnn)


def _slot_models(bnn):
    return [s.get("model") for s in bnn.get("slot") or []]


def _slot_param_values(bnn):
    """Per-slot dict of unwrapped param base values (the actual knob values)."""
    out = []
    for s in bnn.get("slot") or []:
        out.append({k: _unwrap_value(v) for k, v in (s.get("params") or {}).items()})
    return out


def test_real_export_sonic_fidelity(tmp_path, strip_provenance):
    lib, samples = _real_hsp_library(tmp_path)
    irs = IrMapping.load()
    failures = []
    ok = 0
    for sample in samples:
        try:
            body = read_hsp(sample)
            n_named = len(_snapshot_names(body))
            spec = parse_spec(decompile_body(body, lib, irs=irs))
            regen = compose_preset(spec, lib, source=str(sample), irs=irs)
            s_blocks = {(pi, k): bnn for pi, k, bnn in _user_blocks(body)}
            r_blocks = {(pi, k): bnn for pi, k, bnn in _user_blocks(regen)}
            assert set(s_blocks) == set(r_blocks), "block key set differs"
            for kk, sb in s_blocks.items():
                rb = r_blocks[kk]
                assert _base(sb) == _base(rb), f"{kk} base bypass"
                for i in range(n_named):
                    sa = _snap_array(sb)
                    # skip source-null/absent named cells (undefined recall)
                    if not (isinstance(sa, list) and i < len(sa) and sa[i] is not None):
                        continue
                    assert _effective(sb, i) == _effective(rb, i), f"{kk} snap {i}"
                assert _slot_models(sb) == _slot_models(rb), f"{kk} slot models"
                assert _slot_param_values(sb) == _slot_param_values(rb), f"{kk} params"
                assert sb.get("harness") == rb.get("harness"), f"{kk} harness"
                assert sb.get("favorite") == rb.get("favorite"), f"{kk} favorite"
            ok += 1
        except Exception as e:  # noqa: BLE001 — collect all before asserting
            failures.append((sample.name, f"{type(e).__name__}: {e}"))
    assert not failures, (
        f"{ok}/{len(samples)} real exports sonically round-trip; "
        f"{len(failures)} do not. First few: {failures[:3]}"
    )
```

- [ ] **Step 2: Run the scoreboard**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_sonic_fidelity.py -v`
Expected: PASS — `211/211 real exports sonically round-trip` (or the current count of `data/*.hsp`).

If it fails on N presets, read the first few failure messages: a `base bypass`/`snap` failure points at Tasks 1-2, a `slot models`/`params`/`harness`/`favorite` failure at Tasks 4-5. Do not weaken the comparator to make it pass — fix the implementation or, if the failure is a genuinely-excluded case (source-null cell), confirm the skip predicate matches the design (`sa[i] is not None`). Report any residual honestly rather than muting it.

- [ ] **Step 3: Run the full suite**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: PASS, 0 failures. Confirm the existing `test_decompile_acceptance.py` model bar is still 211/211.

- [ ] **Step 4: Commit**

```bash
git add tests/test_decompile_sonic_fidelity.py
git commit -m "test(decompile): sonic-fidelity scoreboard — bypass + slots + harness

New corpus acceptance test asserting per-block base bypass, named-snapshot
effective bypass (source-null cells skipped), all slot models + param values,
harness, and favorite round-trip. Target 211/211. Category 5 items #1 + #3.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

### Task 8: Docs + parent-spec status update

Document the new `raw` field in `CLAUDE.md` and mark Category 5 items #1/#3 done in the parent residuals spec, recording what remains deferred.

**Files:**
- Modify: `CLAUDE.md` (spec.json shape section — document `raw`)
- Modify: `docs/superpowers/specs/2026-07-03-decompiler-round-trip-residuals.md` (status update)

**Interfaces:** none (docs only).

- [ ] **Step 1: Document `raw` in `CLAUDE.md`**

In the `spec.json shape` section of `CLAUDE.md`, after the per-block IR reference subsection, add a short subsection:

```markdown
### Optional: per-block verbatim state (`raw`)

Blocks may carry an optional `"raw"` object holding verbatim Stadium bNN state
that helixgen does not model but preserves for round-trip fidelity:

- `"harness"` — the bNN-level `harness` dict (carries `dual`, `Trails`,
  `ControlSource`, its own `@enabled`). Non-deterministic; preserved verbatim.
- `"slots"` — additional slots beyond the first (`slot[1:]`), i.e. the second
  cab of a dual-cab block.

`raw` is emitted by `decompile` and re-attached by `generate`. It is normally
authored only by the decompiler; hand-editing it is unnecessary for typical
tone specs. Stadium-only.
```

- [ ] **Step 2: Update the parent residuals spec status**

In `docs/superpowers/specs/2026-07-03-decompiler-round-trip-residuals.md`, under the Category 5 section, mark items #1 and #3 DONE (branch `hardening/category5-bypass-and-dualcab`, new `test_decompile_sonic_fidelity.py` scoreboard), and record still-deferred: #2 (input-block params), #4 (`preset.params`), the snapshot enable-override (source-null recall / Case B), and top-level unmodeled state.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-07-03-decompiler-round-trip-residuals.md
git commit -m "docs: document BlockEntry.raw; mark Category 5 #1/#3 done

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhczuS6L99WqfgL7yzTwR1"
```

---

## Post-implementation: hardware verify (manual, with the user)

Not a code task — the design's acceptance gate. After Task 7 is green:

1. Pick a source preset that has **base-bypassed blocks AND a real input** (NOT `Black Keys` — its path-0 input is `InputNone`, so it's silent on cold load). Prefer a preset with `InputInst1` and ideally a real dual-cab (e.g. one of the `CT-*` presets, or `A7X` — verify it has `InputInst1` and a base-bypassed block first).
2. Regenerate it: `PYTHONPATH=$PWD/src python -m helixgen generate <sidecar-or-decompiled-spec>.json -o /tmp/<name>.hsp` (or decompile the source to a spec, then generate).
3. `open -R "/tmp/<name>.hsp"` so it's pre-selected in Finder; user imports onto the Stadium XL.
4. Confirm on hardware: base-bypassed blocks load **bypassed**, bypass footswitch assignments still toggle, snapshots recall correctly, and any dual-cab renders both cabs. Specifically confirm the Megadeth-b05-style single-slot IR cab (slot-level bypass dropped) sounds identical.

---

## Self-Review

**Spec coverage:**
- Item #1 base bypass read → Task 2; write → Task 1. ✓
- Decouple value from snapshot fill → Task 1 (Step 3). ✓
- `test_patch_cli` assertion move + stale comment → Task 1 Step 5 / Task 2 Step 5. ✓
- Item #3 `raw` model → Task 3; generate emit + favorite → Task 4; decompile capture → Task 5. ✓
- Case-B warning → Task 6. ✓
- Scoreboard (base value, named effective bypass w/ null-skip, slot models, slot param values, harness, favorite) → Task 7. ✓
- Docs + parent-spec status → Task 8. ✓
- Hardware verify → post-implementation section. ✓

**Placeholder scan:** every code step shows full code; every run step shows exact command + expected result. No TBD/"handle edge cases". ✓

**Type consistency:** `_block_entry(bnn, library, irs)` used consistently (Tasks 2, 5, 6). `_to_hsp_bnn(..., raw=...)` param name matches the call site (Tasks 4). `BlockEntry.raw` shape `{harness, slots}` consistent across spec/generate/decompile/scoreboard. `_effective`/`_snap_array` helpers defined once in Task 7. ✓
