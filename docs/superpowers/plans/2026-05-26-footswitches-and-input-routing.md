# Footswitches + Input Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two features to helixgen — (a) per-path input routing (Inst 1 / Inst 2 / both / none) with asymmetric defaults, and (b) footswitch and expression-pedal assignments — with logical names resolved per-chassis to Stadium model IDs and controller source integers.

**Architecture:** Both features share a new `src/helixgen/controllers.py` module that holds per-device (`stadium_xl`, future devices) lookup tables. `generate._compose_preset_hsp` gains two new passes: one rewrites the `b00` input endpoint on each path; another wraps `@enabled` (FS) or specific param `value` (EXP) with a `controller` dict and registers source IDs in `preset.sources`. `spec.parse_spec` gains parse helpers for `footswitches` and `expression` and tightens the existing `input` field validation.

**Tech Stack:** Python 3 stdlib only (existing project constraint), `pytest` + `pytest`'s standard `tmp_path` fixture for tests, `click` is already in use for the CLI (not touched in this plan). All file paths absolute from repo root `~/git/helixgen/`.

---

## File Inventory

**Create:**
- `src/helixgen/controllers.py` — `INPUT_MODELS`, `CONTROLLER_SOURCE_IDS`, resolver functions
- `scripts/derive_controller_table.py` — one-time helper for source-ID derivation (not invoked at runtime)
- `tests/test_controllers.py` — table sanity tests
- `tests/test_spec_input.py`
- `tests/test_spec_footswitches.py`
- `tests/test_spec_expression.py`
- `tests/test_generate_input.py`
- `tests/test_generate_footswitches.py`
- `tests/test_generate_expression.py`
- `tests/test_input_reshape.py`

**Modify:**
- `src/helixgen/spec.py` — tighten `input` validation, add FS/EXP dataclasses and parse helpers
- `src/helixgen/generate.py` — input endpoint rewrite pass, FS+EXP controller emission, `preset.sources` population

**Phases are independently shippable.** After Phase 1 completes, input routing is a working feature. After Phase 3 completes, FS is shippable. After Phase 5 completes, EXP is shippable.

---

## Phase 1 — Per-path input routing

### Task 1.1: Add `INPUT_MODELS` table and resolver

**Files:**
- Create: `src/helixgen/controllers.py`
- Test: `tests/test_controllers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_controllers.py`:

```python
"""Sanity tests for the per-chassis controllers table."""
import pytest

from helixgen import controllers


def test_input_models_has_stadium_xl_with_all_four_modes():
    table = controllers.INPUT_MODELS["stadium_xl"]
    assert set(table.keys()) == {"inst1", "inst2", "both", "none"}


def test_input_models_stadium_xl_model_ids_are_p35():
    table = controllers.INPUT_MODELS["stadium_xl"]
    for mode, model_id in table.items():
        assert model_id.startswith("P35_Input"), (
            f"mode {mode!r} maps to {model_id!r}, expected P35_Input* prefix"
        )


def test_resolve_input_model_returns_known_model():
    assert controllers.resolve_input_model("stadium_xl", "both") == "P35_InputInst1_2"


def test_resolve_input_model_unknown_mode_raises_with_valid_list():
    with pytest.raises(controllers.ControllerError) as exc_info:
        controllers.resolve_input_model("stadium_xl", "stereo_only")
    msg = str(exc_info.value)
    assert "stereo_only" in msg
    assert "inst1" in msg and "both" in msg


def test_resolve_input_model_unknown_device_falls_back_to_stadium_xl():
    # Unknown device_id falls back; should resolve "both" via the XL table.
    assert controllers.resolve_input_model("future_device", "both") == "P35_InputInst1_2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controllers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'helixgen.controllers'`

- [ ] **Step 3: Write minimal implementation**

Create `src/helixgen/controllers.py`:

```python
"""Per-device tables for input endpoints and controller (FS/EXP) source IDs.

These tables are empirically derived from real .hsp exports. The outer key
is the chassis's `meta.device_id` (with `stadium_xl` as the canonical alias
used as fallback when the device_id is missing or unrecognized). Inner keys
are the logical names the spec uses.
"""
from __future__ import annotations


class ControllerError(ValueError):
    """Raised when a logical input/FS/EXP name cannot be resolved."""


INPUT_MODELS: dict[str, dict[str, str]] = {
    "stadium_xl": {
        "inst1": "P35_InputInst1",
        "inst2": "P35_InputInst2",
        "both":  "P35_InputInst1_2",
        "none":  "P35_InputNone",
    },
}


def _resolve_device(device_id: str) -> str:
    """Pick the active device table key, falling back to stadium_xl."""
    if device_id in INPUT_MODELS:
        return device_id
    return "stadium_xl"


def resolve_input_model(device_id: str, mode: str) -> str:
    """Look up the Stadium model_id for a logical input mode.

    Raises ControllerError listing valid modes if the mode is unknown.
    """
    table = INPUT_MODELS[_resolve_device(device_id)]
    if mode not in table:
        raise ControllerError(
            f"Unknown input mode {mode!r}. Valid modes: {sorted(table.keys())}."
        )
    return table[mode]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controllers.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/controllers.py tests/test_controllers.py
git commit -m "feat: controllers module with INPUT_MODELS table for stadium_xl"
```

### Task 1.2: Tighten spec `input` validation

**Files:**
- Modify: `src/helixgen/spec.py` (around the existing `_parse_path` function, lines 126-145)
- Test: `tests/test_spec_input.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spec_input.py`:

```python
"""Parse-level tests for spec input-mode validation."""
import pytest

from helixgen.spec import SpecError, parse_spec


def _minimal_spec(**path0_extra):
    return {
        "name": "test",
        "paths": [{"blocks": [], **path0_extra}],
    }


def test_input_omitted_leaves_path_input_none():
    spec = parse_spec(_minimal_spec())
    assert spec.paths[0].input is None


def test_input_valid_modes_accepted():
    for mode in ("inst1", "inst2", "both", "none"):
        spec = parse_spec(_minimal_spec(input=mode))
        assert spec.paths[0].input == mode


def test_input_non_string_rejected():
    with pytest.raises(SpecError, match='"input" must be a string'):
        parse_spec(_minimal_spec(input=42))


def test_input_unknown_string_rejected_with_valid_list():
    with pytest.raises(SpecError) as exc_info:
        parse_spec(_minimal_spec(input="aux"))
    msg = str(exc_info.value)
    assert '"aux"' in msg
    assert "inst1" in msg and "both" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_spec_input.py -v`
Expected: `test_input_unknown_string_rejected_with_valid_list` fails (current `_parse_path` accepts any string).

- [ ] **Step 3: Tighten validation in `spec.py`**

In `src/helixgen/spec.py`, add at module top below existing constants:

```python
VALID_INPUT_MODES = ("inst1", "inst2", "both", "none")
```

Replace the existing `input` validation in `_parse_path` (lines 130-132):

```python
    inp = data.get("input")
    if inp is not None and not isinstance(inp, str):
        raise _err(source, '"input" must be a string if provided.')
```

with:

```python
    inp = data.get("input")
    if inp is not None:
        if not isinstance(inp, str):
            raise _err(source, '"input" must be a string if provided.')
        if inp not in VALID_INPUT_MODES:
            raise _err(
                source,
                f'"input" must be one of {list(VALID_INPUT_MODES)} '
                f'(got {inp!r}).',
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_spec_input.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec_input.py
git commit -m "feat(spec): validate input mode against VALID_INPUT_MODES"
```

### Task 1.3: Mono↔stereo param reshape helper

**Files:**
- Modify: `src/helixgen/generate.py` (add helper near other private generate helpers, before `_compose_preset_hsp`)
- Test: `tests/test_input_reshape.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_input_reshape.py`:

```python
"""Unit tests for _reshape_input_params (mono <-> stereo)."""
from helixgen.generate import _reshape_input_params


def test_mono_to_stereo_wraps_each_value_and_adds_stereolink():
    mono = {
        "Pad":       {"value": 1},
        "Trim":      {"value": 0.5},
        "threshold": {"value": -48.0},
    }
    out = _reshape_input_params(mono, to_stereo=True)
    assert out == {
        "Pad":         {"1": {"value": 1},    "2": {"value": 1}},
        "Trim":        {"1": {"value": 0.5},  "2": {"value": 0.5}},
        "threshold":   {"1": {"value": -48.0},"2": {"value": -48.0}},
        "StereoLink":  {"value": False},
    }


def test_stereo_to_mono_takes_channel_one_and_drops_stereolink():
    stereo = {
        "Pad":        {"1": {"value": 1},   "2": {"value": 0}},
        "Trim":       {"1": {"value": 0.3}, "2": {"value": 0.7}},  # distinct values
        "StereoLink": {"value": False},
    }
    out = _reshape_input_params(stereo, to_stereo=False)
    assert out == {
        "Pad":  {"value": 1},
        "Trim": {"value": 0.3},  # channel 1, NOT channel 2
    }


def test_mono_to_mono_identity():
    mono = {"Pad": {"value": 1}}
    out = _reshape_input_params(mono, to_stereo=False)
    assert out == mono


def test_stereo_to_stereo_identity():
    stereo = {
        "Pad":        {"1": {"value": 1}, "2": {"value": 1}},
        "StereoLink": {"value": False},
    }
    out = _reshape_input_params(stereo, to_stereo=True)
    assert out == stereo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_input_reshape.py -v`
Expected: FAIL with `ImportError: cannot import name '_reshape_input_params'`

- [ ] **Step 3: Implement the helper**

Add to `src/helixgen/generate.py`, immediately after the `_wrap_value_with_snapshots` function (around line 195):

```python
def _is_stereo_param(value: Any) -> bool:
    """True if a slot-param value uses the stereo `{"1": ..., "2": ...}` shape."""
    return (
        isinstance(value, dict)
        and "1" in value
        and isinstance(value["1"], dict)
        and "value" in value["1"]
    )


def _reshape_input_params(
    params: dict[str, Any], *, to_stereo: bool
) -> dict[str, Any]:
    """Convert input-endpoint params between mono and stereo shapes.

    Mono shape:   {"<name>": {"value": x}, ...}
    Stereo shape: {"<name>": {"1": {"value": x}, "2": {"value": y}}, ...,
                   "StereoLink": {"value": False}}

    Going mono → stereo wraps each scalar value into per-channel entries
    (both channels start equal) and adds `StereoLink: false`. Going stereo
    → mono takes channel 1 (channel 2 is discarded) and drops StereoLink.
    Identity transforms (already in target shape) return the input
    unchanged.
    """
    currently_stereo = any(_is_stereo_param(v) for v in params.values())
    if currently_stereo == to_stereo:
        return dict(params)

    if to_stereo:
        out: dict[str, Any] = {}
        for k, v in params.items():
            if k == "StereoLink":
                continue
            out[k] = {"1": dict(v), "2": dict(v)}
        out["StereoLink"] = {"value": False}
        return out

    # stereo → mono
    out = {}
    for k, v in params.items():
        if k == "StereoLink":
            continue
        if _is_stereo_param(v):
            out[k] = dict(v["1"])
        else:
            out[k] = dict(v)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_input_reshape.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/generate.py tests/test_input_reshape.py
git commit -m "feat(generate): _reshape_input_params helper for mono<->stereo input shape"
```

### Task 1.4: Wire input rewrite into `_compose_preset_hsp`

**Files:**
- Modify: `src/helixgen/generate.py` (add helpers + integration in `_compose_preset_hsp`)
- Test: `tests/test_generate_input.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_generate_input.py`:

```python
"""Round-trip tests: spec input mode → generated .hsp b00 model + param shape."""
import json
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC
from helixgen.library import Library


# These tests need a real Stadium chassis. They're skipped when the user's
# data/ directory is empty (clean clone), matching the project's existing
# fixture-gated test pattern.

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library_with_stadium_chassis(tmp_path) -> Library:
    """Build a Library by ingesting the first .hsp in data/ as the chassis."""
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _b00_model(preset: dict, path_index: int) -> str:
    return preset["preset"]["flow"][path_index]["b00"]["slot"][0]["model"]


def _b00_params(preset: dict, path_index: int) -> dict:
    return preset["preset"]["flow"][path_index]["b00"]["slot"][0]["params"]


def _is_stereo(params: dict) -> bool:
    sample = next(iter(v for k, v in params.items() if k != "StereoLink"), None)
    return isinstance(sample, dict) and "1" in sample


@pytest.mark.parametrize("mode,expected_model", [
    ("inst1", "P35_InputInst1"),
    ("inst2", "P35_InputInst2"),
    ("both",  "P35_InputInst1_2"),
    ("none",  "P35_InputNone"),
])
def test_path0_input_mode_sets_model(tmp_path, mode, expected_model):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"input": mode, "blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    assert _b00_model(preset, 0) == expected_model


def test_path0_input_both_yields_stereo_params(tmp_path):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"input": "both", "blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    params = _b00_params(preset, 0)
    assert _is_stereo(params)
    assert params["StereoLink"] == {"value": False}


def test_path0_input_inst1_yields_mono_params(tmp_path):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"input": "inst1", "blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    params = _b00_params(preset, 0)
    assert not _is_stereo(params)
    assert "StereoLink" not in params


def test_default_path0_is_both_path1_is_none(tmp_path):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"blocks": []}, {"blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    assert _b00_model(preset, 0) == "P35_InputInst1_2"
    assert _b00_model(preset, 1) == "P35_InputNone"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_generate_input.py -v`
Expected: most tests FAIL — the b00 model in the generated preset still matches whatever the chassis carried, not what the spec says.

- [ ] **Step 3: Add the input-rewrite pass to generate.py**

In `src/helixgen/generate.py`:

(a) Add a top-level import near the others:

```python
from helixgen import controllers
```

(b) Add the default-mode tuple near `HSP_SNAPSHOT_SLOTS` (around line 176):

```python
DEFAULT_INPUT_MODES = ("both", "none")  # by path index
```

(c) Add the rewrite helper directly after `_reshape_input_params`:

```python
def _rewrite_input_endpoint(path_dict: dict[str, Any], target_model: str) -> None:
    """Rewrite path_dict['b00'] to use `target_model`, reshaping params as needed.

    Mutates path_dict in place. Param values from the chassis are preserved
    across the swap; only the model and (where mono/stereo differs) the param
    wrapping shape change. Raises GenerateError if the path has no b00 slot.
    """
    b00 = path_dict.get("b00")
    if not isinstance(b00, dict) or not b00.get("slot"):
        raise GenerateError(
            "Chassis path has no b00 input slot; cannot apply spec input mode."
        )
    slot = b00["slot"][0]
    if slot.get("model") == target_model:
        return
    target_is_stereo = target_model.endswith("_2")
    slot["params"] = _reshape_input_params(
        slot.get("params") or {}, to_stereo=target_is_stereo
    )
    slot["model"] = target_model
```

(d) Add device-id extraction helper near `_is_chassis_meta_key` (line 179):

```python
def _chassis_device_id(chassis: dict[str, Any]) -> str:
    """Return the chassis's device_id, or 'stadium_xl' if absent/unrecognized."""
    return (chassis.get("meta") or {}).get("device_id") or "stadium_xl"
```

(e) In `_compose_preset_hsp`, immediately after the `flow = ...` line (around line 363, before the `for path_index, chain` loop), insert:

```python
    device_id = _chassis_device_id(chassis)
    for path_index, path_entry in enumerate(spec.paths):
        if path_index >= len(flow):
            break  # block-placement loop below will raise the proper error
        path_dict = flow[path_index]
        if not isinstance(path_dict, dict):
            continue  # ditto
        mode = path_entry.input or DEFAULT_INPUT_MODES[path_index]
        target_model = controllers.resolve_input_model(device_id, mode)
        _rewrite_input_endpoint(path_dict, target_model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_generate_input.py -v`
Expected: PASS (all parametrize cases + 3 explicit tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: All existing tests still pass — but note: any existing snapshot or generate tests that asserted the b00 model implicitly (via full-preset comparison) may now fail because the default for path 0 changed to "both". If they fail, audit the test: it should be updated to either (a) set `input: "inst1"` explicitly to match the chassis it ingests, or (b) accept the new default. Existing tests should *not* mass-update without auditing.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate_input.py
git commit -m "feat(generate): per-path input endpoint rewrite (defaults: both / none)"
```

### Task 1.5: Document `input` in CLAUDE.md

**Files:**
- Modify: `~/git/helixgen/CLAUDE.md` (the spec.json shape section)

- [ ] **Step 1: Add the documentation**

In `CLAUDE.md`, in the "spec.json shape" section (after the `paths` bullet point), add a new paragraph:

```markdown
### Optional: per-path input routing

Each path entry may carry an optional `"input"` field with one of:
- `"inst1"` — Instrument 1 jack only
- `"inst2"` — Instrument 2 jack only
- `"both"` — both jacks (stereo) — **default on paths[0]**
- `"none"` — input disabled — **default on paths[1]**

Stadium-only; ignored with a warning for `.hlx` (legacy Helix) chassis.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document per-path input field in CLAUDE.md"
```

**Phase 1 complete.** Input routing now works end to end. The rest of the plan adds footswitches and expression pedal.

---

## Phase 2 — Footswitch source-ID derivation

This phase has one task: produce the FS source-ID → logical-name mapping for Stadium XL, empirically, from the user's real exports. The output of this phase is two lines added to `controllers.py` (the `CONTROLLER_SOURCE_IDS["stadium_xl"]` table) and a committed derivation script.

### Task 2.1: Write the derivation script and populate the FS table

**Files:**
- Create: `scripts/derive_controller_table.py`
- Modify: `src/helixgen/controllers.py` (add `CONTROLLER_SOURCE_IDS`)
- Test: `tests/test_controllers.py` (extend with FS table tests)

- [ ] **Step 1: Write the script**

Create `scripts/derive_controller_table.py`:

```python
#!/usr/bin/env python3
"""One-time helper: scan data/*.hsp and report observed FS/EXP controller sources.

Not invoked at runtime. Used to derive the values pasted into
src/helixgen/controllers.py:CONTROLLER_SOURCE_IDS.

Usage:
    python scripts/derive_controller_table.py [data_dir]

Prints a frequency table of (source_id, controller_type) tuples seen across
all .hsp files in the given directory (default: ./data).
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from helixgen.hsp import read_hsp


def main(argv: list[str]) -> int:
    data_dir = Path(argv[1] if len(argv) > 1 else "data")
    if not data_dir.is_dir():
        print(f"No such directory: {data_dir}", file=sys.stderr)
        return 1

    counts: Counter = Counter()
    for fp in sorted(data_dir.glob("*.hsp")):
        try:
            d = read_hsp(fp)
        except Exception as e:
            print(f"skip {fp.name}: {e}", file=sys.stderr)
            continue
        flow = d.get("preset", {}).get("flow") or []
        for path in flow:
            if not isinstance(path, dict):
                continue
            for bkey, block in path.items():
                if not isinstance(block, dict) or not bkey.startswith("b"):
                    continue
                slots = block.get("slot") or []
                for slot in slots:
                    if not isinstance(slot, dict):
                        continue
                    enabled = block.get("@enabled")
                    if isinstance(enabled, dict) and isinstance(enabled.get("controller"), dict):
                        c = enabled["controller"]
                        counts[(c.get("source"), c.get("type", ""))] += 1
                    for pname, pval in (slot.get("params") or {}).items():
                        if isinstance(pval, dict) and isinstance(pval.get("controller"), dict):
                            c = pval["controller"]
                            counts[(c.get("source"), c.get("type", ""))] += 1

    print(f"{'source':>12}  {'hex':>12}  {'type':<15}  count")
    for (src, ctype), n in sorted(counts.items()):
        hex_str = f"0x{src:08x}" if isinstance(src, int) else str(src)
        print(f"{src!s:>12}  {hex_str:>12}  {ctype:<15}  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 2: Run the script and capture the output**

Run: `python scripts/derive_controller_table.py data`

Read the output. The relevant rows are those with `type=targetbypass` (FS-as-bypass assignments). Their `source` integers — sorted ascending — are the user's Stadium XL FS source IDs. We expect a contiguous block of 8 (Stadium XL stomp mode); larger blocks may indicate FSes in other modes (snapshot mode, etc.) which we ignore for v1.

Cross-check with the brainstorming-phase finding that FS source IDs follow `0x010101NN`. The first eight values in that range (`0x01010100..0x01010107`) are expected to be `FS1..FS8`. Stadium XL also has FS9 and FS10 (`0x01010108`, `0x01010109`). If the data agrees, populate the table accordingly. If it diverges, follow what the data says — that is the authoritative source for the actual hardware in use.

- [ ] **Step 3: Write the failing test for the new table**

Append to `tests/test_controllers.py`:

```python
def test_controller_source_ids_has_stadium_xl_fs_1_through_10():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    for n in range(1, 11):
        assert f"FS{n}" in table, f"FS{n} missing from stadium_xl table"
        assert isinstance(table[f"FS{n}"], int)


def test_controller_source_ids_stadium_xl_fs_values_unique():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    fs_values = [table[f"FS{n}"] for n in range(1, 11)]
    assert len(set(fs_values)) == len(fs_values), "FS source IDs are not unique"


def test_resolve_controller_source_known_name():
    sid = controllers.resolve_controller_source("stadium_xl", "FS1")
    assert isinstance(sid, int)


def test_resolve_controller_source_unknown_raises_with_valid_list():
    with pytest.raises(controllers.ControllerError) as exc_info:
        controllers.resolve_controller_source("stadium_xl", "FS99")
    msg = str(exc_info.value)
    assert "FS99" in msg
    assert "FS1" in msg
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_controllers.py -v`
Expected: FAIL with `AttributeError: module 'helixgen.controllers' has no attribute 'CONTROLLER_SOURCE_IDS'`.

- [ ] **Step 5: Populate the table and add the resolver**

In `src/helixgen/controllers.py`, after `INPUT_MODELS`:

```python
# Empirically derived from the user's real .hsp exports (see
# scripts/derive_controller_table.py). FS1..FS10 are Stadium XL's 10
# physical stomp-mode footswitches; their source IDs follow 0x010101NN.
CONTROLLER_SOURCE_IDS: dict[str, dict[str, int]] = {
    "stadium_xl": {
        "FS1":  0x01010100,
        "FS2":  0x01010101,
        "FS3":  0x01010102,
        "FS4":  0x01010103,
        "FS5":  0x01010104,
        "FS6":  0x01010105,
        "FS7":  0x01010106,
        "FS8":  0x01010107,
        "FS9":  0x01010108,
        "FS10": 0x01010109,
    },
}


def resolve_controller_source(device_id: str, logical_name: str) -> int:
    """Look up a controller source ID for a logical FS/EXP name.

    Raises ControllerError listing valid names if the logical name is unknown.
    """
    table = CONTROLLER_SOURCE_IDS[_resolve_device(device_id)]
    if logical_name not in table:
        raise ControllerError(
            f"Unknown controller name {logical_name!r}. "
            f"Valid names: {sorted(table.keys())}."
        )
    return table[logical_name]
```

**Important:** If the derivation script in Step 2 produced source IDs that differ from the `0x010101NN` pattern above, replace the values in the dict with what the script printed. The pattern is an inferred default; the script is the ground truth.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_controllers.py -v`
Expected: PASS (9 tests total)

- [ ] **Step 7: Commit**

```bash
git add scripts/derive_controller_table.py src/helixgen/controllers.py tests/test_controllers.py
git commit -m "feat(controllers): empirically derived FS1-FS10 source IDs for stadium_xl"
```

---

## Phase 3 — Footswitch spec + generation

### Task 3.1: Footswitch dataclasses and spec parsing

**Files:**
- Modify: `src/helixgen/spec.py`
- Test: `tests/test_spec_footswitches.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spec_footswitches.py`:

```python
"""Parse-level tests for the spec footswitches section."""
import pytest

from helixgen.spec import SpecError, parse_spec


def _spec_with_footswitches(*entries):
    return {
        "name": "fs-test",
        "paths": [{"blocks": [{"block": "Compulsive Drive"}]}],
        "footswitches": list(entries),
    }


def test_no_footswitches_field_yields_empty_list():
    spec = parse_spec({"name": "t", "paths": [{"blocks": []}]})
    assert spec.footswitches == []


def test_single_footswitch_minimal_fields():
    spec = parse_spec(_spec_with_footswitches(
        {"switch": "FS3", "block": "Compulsive Drive"},
    ))
    assert len(spec.footswitches) == 1
    fs = spec.footswitches[0]
    assert fs.switch == "FS3"
    assert fs.block == "Compulsive Drive"
    assert fs.behavior == "latching"  # default


def test_footswitch_with_explicit_behavior_momentary():
    spec = parse_spec(_spec_with_footswitches(
        {"switch": "FS4", "block": "Compulsive Drive", "behavior": "momentary"},
    ))
    assert spec.footswitches[0].behavior == "momentary"


def test_footswitch_invalid_behavior_rejected():
    with pytest.raises(SpecError, match='"behavior" must be'):
        parse_spec(_spec_with_footswitches(
            {"switch": "FS1", "block": "X", "behavior": "weird"},
        ))


def test_footswitch_missing_switch_rejected():
    with pytest.raises(SpecError, match='"switch" is required'):
        parse_spec(_spec_with_footswitches({"block": "X"}))


def test_footswitch_missing_block_rejected():
    with pytest.raises(SpecError, match='"block" is required'):
        parse_spec(_spec_with_footswitches({"switch": "FS1"}))


def test_footswitch_duplicate_switch_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec(_spec_with_footswitches(
            {"switch": "FS1", "block": "A"},
            {"switch": "FS1", "block": "B"},
        ))


def test_footswitch_duplicate_block_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec(_spec_with_footswitches(
            {"switch": "FS1", "block": "A"},
            {"switch": "FS2", "block": "A"},
        ))


def test_footswitches_must_be_list():
    with pytest.raises(SpecError, match='"footswitches" must be a list'):
        parse_spec({"name": "t", "paths": [{"blocks": []}], "footswitches": {}})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_spec_footswitches.py -v`
Expected: FAIL (the `FootswitchAssignment` class and parsing don't exist yet).

- [ ] **Step 3: Add dataclass and parser in `spec.py`**

In `src/helixgen/spec.py`, add after the `Snapshot` dataclass:

```python
@dataclass
class FootswitchAssignment:
    """A single FS-to-block bypass assignment.

    `switch` is a logical name (e.g. "FS3"); the chassis-specific source
    ID is resolved at generate time.
    """
    switch: str
    block: str
    behavior: str = "latching"


VALID_FS_BEHAVIORS = ("latching", "momentary")
```

Extend the `Spec` dataclass — change:

```python
@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
```

to:

```python
@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
    footswitches: list[FootswitchAssignment] = field(default_factory=list)
```

Add parser helpers below `_parse_snapshot`:

```python
def _parse_footswitches(raw: Any, *, source: str) -> list[FootswitchAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"footswitches" must be a list.')
    out: list[FootswitchAssignment] = []
    seen_switches: set[str] = set()
    seen_blocks: set[str] = set()
    for i, entry in enumerate(raw):
        fs = _parse_footswitch(entry, source=f"{source} footswitches[{i}]")
        if fs.switch in seen_switches:
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate switch {fs.switch!r}; each switch may appear once.",
            )
        if fs.block in seen_blocks:
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate block {fs.block!r}; one block per footswitch.",
            )
        seen_switches.add(fs.switch)
        seen_blocks.add(fs.block)
        out.append(fs)
    return out


def _parse_footswitch(data: Any, *, source: str) -> FootswitchAssignment:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    switch = data.get("switch")
    if not isinstance(switch, str) or not switch:
        raise _err(source, '"switch" is required and must be a non-empty string.')
    block = data.get("block")
    if not isinstance(block, str) or not block:
        raise _err(source, '"block" is required and must be a non-empty string.')
    behavior = data.get("behavior", "latching")
    if behavior not in VALID_FS_BEHAVIORS:
        raise _err(
            source,
            f'"behavior" must be one of {list(VALID_FS_BEHAVIORS)} (got {behavior!r}).',
        )
    return FootswitchAssignment(switch=switch, block=block, behavior=behavior)
```

In `parse_spec`, replace:

```python
    return Spec(name=name, paths=paths, author=author, snapshots=snapshots)
```

with:

```python
    footswitches = _parse_footswitches(data.get("footswitches"), source=source)
    return Spec(
        name=name, paths=paths, author=author,
        snapshots=snapshots, footswitches=footswitches,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_spec_footswitches.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec_footswitches.py
git commit -m "feat(spec): parse footswitches section with switch/block/behavior + dup detection"
```

### Task 3.2: Emit FS controller blocks during generation

**Files:**
- Modify: `src/helixgen/generate.py`
- Test: `tests/test_generate_footswitches.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_generate_footswitches.py`:

```python
"""Round-trip tests: spec footswitches → controller block on @enabled + preset.sources."""
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.library import Library

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _build_spec(library, **extra):
    """Build a parsed Spec with one drive block on path 0, plus extras."""
    from helixgen.spec import parse_spec
    # Pick any drive block from the library to use as the FS-controlled block.
    drive_blocks = [b for b in library.iter_blocks() if b.category == "drive"]
    if not drive_blocks:
        pytest.skip("No drive blocks in library; cannot build FS test spec.")
    drive_name = drive_blocks[0].display_name
    return parse_spec({
        "name": "fs-test",
        "paths": [{"input": "inst1", "blocks": [{"block": drive_name}]}],
        **extra,
    }), drive_name


def _b01_enabled(preset):
    return preset["preset"]["flow"][0]["b01"]["@enabled"]


def test_fs_assigned_block_gets_controller_on_enabled(tmp_path):
    library = _library(tmp_path)
    spec, drive_name = _build_spec(library, footswitches=[
        {"switch": "FS3", "block": drive_name},
    ])
    preset = compose_preset(spec, library, source="test")
    enabled = _b01_enabled(preset)
    assert "controller" in enabled
    ctrl = enabled["controller"]
    assert ctrl["type"] == "targetbypass"
    assert ctrl["behavior"] == "latching"
    assert ctrl["source"] == 0x01010102  # FS3
    assert ctrl["min"] is None and ctrl["max"] is None


def test_fs_momentary_behavior_propagates_to_controller(tmp_path):
    library = _library(tmp_path)
    spec, drive_name = _build_spec(library, footswitches=[
        {"switch": "FS4", "block": drive_name, "behavior": "momentary"},
    ])
    preset = compose_preset(spec, library, source="test")
    assert _b01_enabled(preset)["controller"]["behavior"] == "momentary"


def test_fs_source_id_added_to_preset_sources(tmp_path):
    library = _library(tmp_path)
    spec, drive_name = _build_spec(library, footswitches=[
        {"switch": "FS5", "block": drive_name},
    ])
    preset = compose_preset(spec, library, source="test")
    sources = preset["preset"]["sources"]
    assert "16843012" in sources or 0x01010104 in sources or "0x01010104" in sources \
        or str(0x01010104) in sources  # implementation chooses string-int key
    # Whichever form the impl uses, the entry value should be {"bypass": false}
    key_used = next(k for k in sources if int(k) == 0x01010104)
    assert sources[key_used] == {"bypass": False}


def test_no_fs_means_no_controller_wrap(tmp_path):
    library = _library(tmp_path)
    spec, _ = _build_spec(library, footswitches=[])
    preset = compose_preset(spec, library, source="test")
    enabled = _b01_enabled(preset)
    assert "controller" not in enabled


def test_fs_with_snapshot_disable_composes(tmp_path):
    """A block that has both an FS assignment and a snapshot-disable should
    get both: @enabled wrapper carries 'snapshots' AND 'controller'."""
    library = _library(tmp_path)
    spec, drive_name = _build_spec(library,
        snapshots=[
            {"name": "A"},
            {"name": "B", "disable": [drive_name]},
        ],
        footswitches=[{"switch": "FS3", "block": drive_name}],
    )
    preset = compose_preset(spec, library, source="test")
    enabled = _b01_enabled(preset)
    assert "controller" in enabled
    assert "snapshots" in enabled
    assert enabled["snapshots"][1] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_generate_footswitches.py -v`
Expected: FAIL — current generator doesn't emit controllers and `preset.sources` isn't populated for new IDs.

- [ ] **Step 3: Add controller-block emission**

In `src/helixgen/generate.py`, add a helper after `_chassis_device_id`:

```python
def _build_fs_controller(source_id: int, behavior: str) -> dict[str, Any]:
    """Build the controller dict that wraps @enabled for an FS assignment.

    Shape derived from real Stadium XL exports.
    """
    return {
        "behavior":   behavior,
        "bypassed":   False,
        "curve":      "linear",
        "delay":      None,
        "goid":       None,
        "max":        None,
        "midisource": 0,
        "min":        None,
        "source":     source_id,
        "threshold":  None,
        "type":       "targetbypass",
    }
```

Extend `_to_hsp_bnn` to accept an FS controller. Change its signature:

```python
def _to_hsp_bnn(
    block: Block,
    user_params: dict[str, Any],
    *,
    position: int,
    path_index: int,
    enabled_overrides: list[bool | None] | None = None,
    param_overrides: dict[str, list[Any]] | None = None,
    fs_controller: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

And replace the `bnn` construction at the end:

```python
    enabled_wrapped = _wrap_value_with_snapshots(True, enabled_overrides)
    if fs_controller is not None:
        enabled_wrapped["controller"] = fs_controller

    bnn: dict[str, Any] = {
        "@enabled": enabled_wrapped,
        "type": flat.get("@type", _hsp_type_for_block(block)),
        "position": position,
        "path": path_index,
        "slot": [slot_inner],
    }
    return bnn
```

Add a build helper near `_build_snapshot_overrides`:

```python
def _build_fs_assignments(
    spec: Spec, resolved: list[ResolvedPath], device_id: str
) -> tuple[dict[tuple[int, int], dict[str, Any]], set[int]]:
    """Resolve FS assignments to (path,chain) → controller-dict + source-id set."""
    fs_map: dict[tuple[int, int], dict[str, Any]] = {}
    source_ids: set[int] = set()
    for fs in spec.footswitches:
        path_idx, chain_idx, _ = _resolve_snapshot_block(fs.block, resolved)
        source_id = controllers.resolve_controller_source(device_id, fs.switch)
        fs_map[(path_idx, chain_idx)] = _build_fs_controller(source_id, fs.behavior)
        source_ids.add(source_id)
    return fs_map, source_ids
```

Note: `_resolve_snapshot_block`'s error message mentions "Snapshot"; we reuse it here even though the call site is FS. For clarity, rename it now — the function is generic. Update the function header:

```python
def _resolve_spec_block(
    name_or_id: str, resolved: list[ResolvedPath]
) -> tuple[int, int, Block]:
    """Locate a block in the resolved spec chains by display_name or model_id."""
    matches: list[tuple[int, int, Block]] = []
    for path_idx, chain in enumerate(resolved):
        for chain_idx, (block, _) in enumerate(chain):
            if block.model_id == name_or_id or block.display_name == name_or_id:
                matches.append((path_idx, chain_idx, block))
    if not matches:
        raise GenerateError(
            f"Spec references block {name_or_id!r} but no such block is "
            f"in the spec's paths. Add it to a path first."
        )
    if len(matches) > 1:
        raise GenerateError(
            f"Block {name_or_id!r} matches multiple placed blocks. "
            f"Use the model_id (in brackets in `list-blocks`) to disambiguate."
        )
    return matches[0]
```

Replace both existing call sites of `_resolve_snapshot_block` (in `_build_snapshot_overrides`, the `for name in snap.disable` loop and the `for block_name, overrides in snap.params.items()` loop) with `_resolve_spec_block`.

In `_compose_preset_hsp`, after the `enabled_map, param_map = _build_snapshot_overrides(...)` line, add:

```python
    fs_map, fs_source_ids = _build_fs_assignments(spec, resolved, _chassis_device_id(chassis))
```

In the block-placement loop, pass `fs_controller`:

```python
        for chain_idx, (block, user_params) in enumerate(chain):
            slot_index = chain_idx + 1
            key = f"b{slot_index:02d}"
            path_dict[key] = _to_hsp_bnn(
                block, user_params,
                position=slot_index,
                path_index=path_index,
                enabled_overrides=enabled_map.get((path_index, chain_idx)),
                param_overrides=param_map.get((path_index, chain_idx)),
                fs_controller=fs_map.get((path_index, chain_idx)),
            )
```

After the block-placement loop in `_compose_preset_hsp`, before `preset["preset"]["snapshots"] = ...`, add `preset.sources` population:

```python
    if fs_source_ids:
        sources = preset["preset"].setdefault("sources", {})
        for sid in fs_source_ids:
            sources.setdefault(str(sid), {"bypass": False})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_generate_footswitches.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate_footswitches.py
git commit -m "feat(generate): emit FS controller blocks + register source IDs"
```

### Task 3.3: Document footswitches in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the section**

In `CLAUDE.md`, after the "Optional: snapshots" section, add:

```markdown
### Optional: footswitches

Assign blocks to physical footswitches on the device. Stadium XL exposes
`FS1`..`FS10`.

```json
"footswitches": [
  {"switch": "FS3", "block": "Compulsive Drive"},
  {"switch": "FS4", "block": "Tape Echo Stereo", "behavior": "momentary"}
]
```

- `switch` — `"FS1"`..`"FS10"`.
- `block` — must reference a block placed in `paths`.
- `behavior` — `"latching"` (default; toggle) or `"momentary"` (on while held).
- One switch may be assigned at most one block; one block may be on at most one switch.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document footswitches section in CLAUDE.md"
```

**Phase 3 complete.** Footswitches are shippable independently.

---

## Phase 4 — Expression-pedal source-ID derivation

### Task 4.1: Find an EXP example, confirm wrapper shape, populate EXP table

**Files:**
- Modify: `src/helixgen/controllers.py` (add EXP entries)
- Test: `tests/test_controllers.py` (extend with EXP table tests)

- [ ] **Step 1: Run the derivation script and identify EXP sources**

Run: `python scripts/derive_controller_table.py data`

Look for rows where `type=param`. The brainstorming exploration already found one EXP example with `source=16908545` (`0x01020101`) and `type=param`. There may be other `param`-type sources in the data, including `0x010101NN` values that represent FSes assigned to a knob rather than to bypass. **The EXP-pedal sources are distinguishable by being outside the `0x010101NN` FS range.**

Based on the data already inspected:
- `0x01020100` and `0x01020101` are seen in some files — these are likely `EXP1` and `EXP2` respectively. Confirm by inspecting the controlled params (a real expression assignment is on something like `Pedal Position` or `Mix`, with `behavior: continuous`).

If you observe any source IDs that look like EXP but you cannot positively identify, stop and ask the user to confirm against their actual Stadium XL — do not guess.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_controllers.py`:

```python
def test_controller_source_ids_has_exp1_exp2():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    assert "EXP1" in table
    assert "EXP2" in table


def test_exp_source_ids_distinct_from_fs():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    fs_values = {table[f"FS{n}"] for n in range(1, 11)}
    exp_values = {table["EXP1"], table["EXP2"]}
    assert fs_values.isdisjoint(exp_values), (
        "EXP source IDs collide with FS IDs; check the table."
    )
```

Note: `EXPONBOARD` is intentionally not in the assertion. If the derivation step confirms it exists on Stadium XL, add a third assertion line; if it doesn't (the design's risk #4), leave it out.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_controllers.py::test_controller_source_ids_has_exp1_exp2 -v`
Expected: FAIL — table doesn't have EXP yet.

- [ ] **Step 4: Add EXP entries to the table**

In `src/helixgen/controllers.py`, extend `CONTROLLER_SOURCE_IDS["stadium_xl"]`:

```python
CONTROLLER_SOURCE_IDS: dict[str, dict[str, int]] = {
    "stadium_xl": {
        # FS1..FS10 (stomp mode footswitches), pattern 0x010101NN
        "FS1":  0x01010100,
        "FS2":  0x01010101,
        "FS3":  0x01010102,
        "FS4":  0x01010103,
        "FS5":  0x01010104,
        "FS6":  0x01010105,
        "FS7":  0x01010106,
        "FS8":  0x01010107,
        "FS9":  0x01010108,
        "FS10": 0x01010109,
        # Expression pedals (derived empirically from data/*.hsp)
        "EXP1": 0x01020100,
        "EXP2": 0x01020101,
    },
}
```

**Replace the EXP values with whatever the derivation step reported** if it disagrees with these defaults.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_controllers.py -v`
Expected: PASS (all controller tests, 11 total)

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/controllers.py tests/test_controllers.py
git commit -m "feat(controllers): add EXP1/EXP2 source IDs for stadium_xl"
```

---

## Phase 5 — Expression pedal spec + generation

### Task 5.1: Expression dataclasses and spec parsing

**Files:**
- Modify: `src/helixgen/spec.py`
- Test: `tests/test_spec_expression.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_spec_expression.py`:

```python
"""Parse-level tests for the spec expression section."""
import pytest

from helixgen.spec import SpecError, parse_spec


def _spec(*expression_entries):
    return parse_spec({
        "name": "exp-test",
        "paths": [{"blocks": [{"block": "Brit Plexi Brt"}]}],
        "expression": list(expression_entries),
    })


def test_no_expression_field_yields_empty_list():
    spec = parse_spec({"name": "t", "paths": [{"blocks": []}]})
    assert spec.expression == []


def test_minimal_expression_entry():
    spec = _spec({
        "pedal": "EXP1",
        "targets": [{"block": "Brit Plexi Brt", "param": "Master"}],
    })
    assert len(spec.expression) == 1
    e = spec.expression[0]
    assert e.pedal == "EXP1"
    assert len(e.targets) == 1
    t = e.targets[0]
    assert t.block == "Brit Plexi Brt"
    assert t.param == "Master"
    assert t.min == 0.0
    assert t.max == 1.0


def test_expression_target_with_custom_min_max():
    spec = _spec({
        "pedal": "EXP1",
        "targets": [{"block": "Brit Plexi Brt", "param": "Master", "min": 0.2, "max": 0.8}],
    })
    t = spec.expression[0].targets[0]
    assert t.min == 0.2
    assert t.max == 0.8


def test_expression_target_min_greater_than_max_rejected():
    with pytest.raises(SpecError, match='"min" must be <='):
        _spec({
            "pedal": "EXP1",
            "targets": [{"block": "X", "param": "Y", "min": 0.9, "max": 0.1}],
        })


def test_expression_target_min_out_of_range_rejected():
    with pytest.raises(SpecError, match="must be in"):
        _spec({
            "pedal": "EXP1",
            "targets": [{"block": "X", "param": "Y", "min": -0.1}],
        })


def test_expression_multi_target():
    spec = _spec({
        "pedal": "EXP1",
        "targets": [
            {"block": "Brit Plexi Brt", "param": "Master"},
            {"block": "Brit Plexi Brt", "param": "Drive"},
        ],
    })
    assert len(spec.expression[0].targets) == 2


def test_expression_duplicate_pedal_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        _spec(
            {"pedal": "EXP1", "targets": [{"block": "A", "param": "P"}]},
            {"pedal": "EXP1", "targets": [{"block": "B", "param": "Q"}]},
        )


def test_expression_duplicate_block_param_across_pedals_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        _spec(
            {"pedal": "EXP1", "targets": [{"block": "A", "param": "P"}]},
            {"pedal": "EXP2", "targets": [{"block": "A", "param": "P"}]},
        )


def test_expression_empty_targets_rejected():
    with pytest.raises(SpecError, match="targets.*non-empty"):
        _spec({"pedal": "EXP1", "targets": []})


def test_expression_missing_pedal_rejected():
    with pytest.raises(SpecError, match='"pedal" is required'):
        _spec({"targets": [{"block": "A", "param": "B"}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_spec_expression.py -v`
Expected: FAIL — `ExpressionAssignment` doesn't exist.

- [ ] **Step 3: Add dataclasses and parser**

In `src/helixgen/spec.py`, add after `FootswitchAssignment`:

```python
@dataclass
class ExpressionTarget:
    block: str
    param: str
    min: float = 0.0
    max: float = 1.0


@dataclass
class ExpressionAssignment:
    pedal: str
    targets: list[ExpressionTarget] = field(default_factory=list)
```

Extend `Spec`:

```python
@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
    footswitches: list[FootswitchAssignment] = field(default_factory=list)
    expression: list[ExpressionAssignment] = field(default_factory=list)
```

Add parsers below `_parse_footswitch`:

```python
def _parse_expression(raw: Any, *, source: str) -> list[ExpressionAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"expression" must be a list.')
    out: list[ExpressionAssignment] = []
    seen_pedals: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for i, entry in enumerate(raw):
        assignment = _parse_expression_assignment(
            entry, source=f"{source} expression[{i}]"
        )
        if assignment.pedal in seen_pedals:
            raise _err(
                f"{source} expression[{i}]",
                f"duplicate pedal {assignment.pedal!r}; each pedal may appear once.",
            )
        seen_pedals.add(assignment.pedal)
        for j, t in enumerate(assignment.targets):
            key = (t.block, t.param)
            if key in seen_targets:
                raise _err(
                    f"{source} expression[{i}] targets[{j}]",
                    f"duplicate (block, param) {key!r}; one param per pedal across the spec.",
                )
            seen_targets.add(key)
        out.append(assignment)
    return out


def _parse_expression_assignment(data: Any, *, source: str) -> ExpressionAssignment:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    pedal = data.get("pedal")
    if not isinstance(pedal, str) or not pedal:
        raise _err(source, '"pedal" is required and must be a non-empty string.')
    targets_raw = data.get("targets")
    if not isinstance(targets_raw, list) or len(targets_raw) == 0:
        raise _err(source, '"targets" must be a non-empty list.')
    targets = [
        _parse_expression_target(t, source=f"{source} targets[{j}]")
        for j, t in enumerate(targets_raw)
    ]
    return ExpressionAssignment(pedal=pedal, targets=targets)


def _parse_expression_target(data: Any, *, source: str) -> ExpressionTarget:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    block = data.get("block")
    if not isinstance(block, str) or not block:
        raise _err(source, '"block" is required and must be a non-empty string.')
    param = data.get("param")
    if not isinstance(param, str) or not param:
        raise _err(source, '"param" is required and must be a non-empty string.')
    mn = data.get("min", 0.0)
    mx = data.get("max", 1.0)
    for label, val in (("min", mn), ("max", mx)):
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise _err(source, f'"{label}" must be a number.')
        if val < 0.0 or val > 1.0:
            raise _err(source, f'"{label}" must be in [0.0, 1.0] (got {val}).')
    if mn > mx:
        raise _err(source, f'"min" must be <= "max" (got min={mn}, max={mx}).')
    return ExpressionTarget(block=block, param=param, min=float(mn), max=float(mx))
```

In `parse_spec`, add to the final `Spec(...)` construction:

```python
    expression = _parse_expression(data.get("expression"), source=source)
    return Spec(
        name=name, paths=paths, author=author,
        snapshots=snapshots, footswitches=footswitches, expression=expression,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_spec_expression.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/spec.py tests/test_spec_expression.py
git commit -m "feat(spec): parse expression section with pedals + multi-target + min/max"
```

### Task 5.2: Emit EXP controller wrappers at the param level

**Files:**
- Modify: `src/helixgen/generate.py`
- Test: `tests/test_generate_expression.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_generate_expression.py`:

```python
"""Round-trip tests: spec expression → controller block on slot.params[X]."""
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.library import Library

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _amp_block(library):
    amps = [b for b in library.iter_blocks() if b.category == "amp"]
    if not amps:
        pytest.skip("No amp blocks in library.")
    return amps[0]


def _b01_param(preset, name):
    return preset["preset"]["flow"][0]["b01"]["slot"][0]["params"][name]


def test_exp_target_wraps_param_value_with_controller(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    sample_param = next(iter(amp.params.keys()))
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [{"block": amp.display_name, "param": sample_param}],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    wrapped = _b01_param(preset, sample_param)
    assert "controller" in wrapped
    ctrl = wrapped["controller"]
    assert ctrl["type"] == "param"
    assert ctrl["behavior"] == "continuous"
    assert ctrl["source"] == 0x01020100  # EXP1
    assert ctrl["min"] == 0.0
    assert ctrl["max"] == 1.0


def test_exp_custom_min_max_propagates(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    sample_param = next(iter(amp.params.keys()))
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [{
                "block": amp.display_name, "param": sample_param,
                "min": 0.25, "max": 0.75,
            }],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    ctrl = _b01_param(preset, sample_param)["controller"]
    assert ctrl["min"] == 0.25
    assert ctrl["max"] == 0.75


def test_exp_source_id_registered_in_preset_sources(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    sample_param = next(iter(amp.params.keys()))
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [{"block": amp.display_name, "param": sample_param}],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    sources = preset["preset"]["sources"]
    key = next(k for k in sources if int(k) == 0x01020100)
    assert sources[key] == {"bypass": False}


def test_exp_multi_target_wraps_each_param(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    p1, p2 = list(amp.params.keys())[:2]
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [
                {"block": amp.display_name, "param": p1},
                {"block": amp.display_name, "param": p2},
            ],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    assert "controller" in _b01_param(preset, p1)
    assert "controller" in _b01_param(preset, p2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_generate_expression.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement EXP controller emission**

In `src/helixgen/generate.py`, add a helper near `_build_fs_controller`:

```python
def _build_exp_controller(source_id: int, min_val: float, max_val: float) -> dict[str, Any]:
    """Build the controller dict that wraps a param value for an EXP assignment.

    Shape derived from real Stadium XL exports (type=param, behavior=continuous,
    numeric delay/goid/threshold).
    """
    return {
        "behavior":   "continuous",
        "bypassed":   False,
        "curve":      "linear",
        "delay":      0,
        "goid":       0,
        "max":        max_val,
        "midisource": 0,
        "min":        min_val,
        "source":     source_id,
        "threshold":  0.0,
        "type":       "param",
    }
```

Add a build helper near `_build_fs_assignments`:

```python
def _build_exp_assignments(
    spec: Spec, resolved: list[ResolvedPath], device_id: str
) -> tuple[dict[tuple[int, int, str], dict[str, Any]], set[int]]:
    """Resolve EXP assignments to (path, chain, param) → controller dict + source IDs."""
    exp_map: dict[tuple[int, int, str], dict[str, Any]] = {}
    source_ids: set[int] = set()
    for assignment in spec.expression:
        source_id = controllers.resolve_controller_source(device_id, assignment.pedal)
        for target in assignment.targets:
            path_idx, chain_idx, block = _resolve_spec_block(target.block, resolved)
            if target.param not in block.params:
                raise GenerateError(
                    f"EXP target {assignment.pedal} → "
                    f"{target.block!r}.{target.param!r}: unknown param. "
                    f"Known params: {sorted(block.params.keys())}."
                )
            exp_map[(path_idx, chain_idx, target.param)] = _build_exp_controller(
                source_id, target.min, target.max,
            )
            source_ids.add(source_id)
    return exp_map, source_ids
```

Extend `_to_hsp_bnn` to accept exp wrappers — add a kwarg:

```python
def _to_hsp_bnn(
    block: Block,
    user_params: dict[str, Any],
    *,
    position: int,
    path_index: int,
    enabled_overrides: list[bool | None] | None = None,
    param_overrides: dict[str, list[Any]] | None = None,
    fs_controller: dict[str, Any] | None = None,
    exp_controllers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
```

In the params-building loop inside `_to_hsp_bnn`, replace:

```python
    params: dict[str, Any] = {}
    for k, v in flat.items():
        if not isinstance(k, str) or k.startswith(RAW_BLOCK_SYSTEM_KEY_PREFIX):
            continue
        params[k] = _wrap_value_with_snapshots(v, (param_overrides or {}).get(k))
    slot_inner["params"] = params
```

with:

```python
    params: dict[str, Any] = {}
    for k, v in flat.items():
        if not isinstance(k, str) or k.startswith(RAW_BLOCK_SYSTEM_KEY_PREFIX):
            continue
        wrapped = _wrap_value_with_snapshots(v, (param_overrides or {}).get(k))
        if exp_controllers and k in exp_controllers:
            wrapped["controller"] = exp_controllers[k]
        params[k] = wrapped
    slot_inner["params"] = params
```

In `_compose_preset_hsp`, after `fs_map, fs_source_ids = ...`, add:

```python
    exp_map, exp_source_ids = _build_exp_assignments(spec, resolved, _chassis_device_id(chassis))
```

In the block placement loop, extend the `_to_hsp_bnn` call:

```python
            path_dict[key] = _to_hsp_bnn(
                block, user_params,
                position=slot_index,
                path_index=path_index,
                enabled_overrides=enabled_map.get((path_index, chain_idx)),
                param_overrides=param_map.get((path_index, chain_idx)),
                fs_controller=fs_map.get((path_index, chain_idx)),
                exp_controllers={
                    pname: ctrl
                    for (pi, ci, pname), ctrl in exp_map.items()
                    if pi == path_index and ci == chain_idx
                } or None,
            )
```

Update the `preset.sources` registration to include EXP IDs:

```python
    all_source_ids = fs_source_ids | exp_source_ids
    if all_source_ids:
        sources = preset["preset"].setdefault("sources", {})
        for sid in all_source_ids:
            sources.setdefault(str(sid), {"bypass": False})
```

(Replace the old `if fs_source_ids:` block with this one.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_generate_expression.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `pytest -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/generate.py tests/test_generate_expression.py
git commit -m "feat(generate): emit EXP controller wrappers on param values + multi-target"
```

### Task 5.3: Document expression in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the section**

In `CLAUDE.md`, after the footswitches section, add:

```markdown
### Optional: expression pedal

Sweep one or more parameters with the expression pedal(s). Stadium XL
exposes `EXP1` and `EXP2`.

```json
"expression": [
  {
    "pedal": "EXP1",
    "targets": [{"block": "Teardrop 310", "param": "Position"}]
  },
  {
    "pedal": "EXP2",
    "targets": [
      {"block": "Brit Plexi Brt",   "param": "Master", "min": 0.0, "max": 0.7},
      {"block": "Tape Echo Stereo", "param": "Mix",    "min": 0.0, "max": 0.4}
    ]
  }
]
```

- `pedal` — `"EXP1"` or `"EXP2"`.
- `targets` — non-empty list. Each target sweeps one param on one block.
- `min`/`max` — normalized 0..1 floats; default `0.0`/`1.0`; must satisfy `min ≤ max`.
- One pedal may have many targets. One `(block, param)` pair may be driven by at most one pedal.
- v1 only sweeps 0..1-style float params (knob values). Hz/int/bool params are out of scope.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document expression section in CLAUDE.md"
```

**Phase 5 complete.** All three features are shippable.

---

## Phase 6 — End-to-end integration test

### Task 6.1: All-features-together round-trip

**Files:**
- Create: `tests/test_generate_combined.py`

- [ ] **Step 1: Write the test**

Create `tests/test_generate_combined.py`:

```python
"""Integration test: input + snapshots + footswitches + expression in one spec."""
import json
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC
from helixgen.library import Library

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def test_combined_spec_roundtrips_with_all_features(tmp_path):
    library = _library(tmp_path)
    amps = [b for b in library.iter_blocks() if b.category == "amp"]
    drives = [b for b in library.iter_blocks() if b.category == "drive"]
    if not amps or not drives:
        pytest.skip("Need at least one amp and one drive in the library.")
    amp = amps[0]
    drive = drives[0]
    amp_param = next(iter(amp.params.keys()))

    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "combined",
        "paths": [{
            "input": "both",
            "blocks": [
                {"block": drive.display_name},
                {"block": amp.display_name},
            ],
        }],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": [drive.display_name]},
        ],
        "footswitches": [
            {"switch": "FS3", "block": drive.display_name},
        ],
        "expression": [
            {"pedal": "EXP1", "targets": [
                {"block": amp.display_name, "param": amp_param, "min": 0.1, "max": 0.9},
            ]},
        ],
    })
    preset = compose_preset(spec, library, source="test")

    # Input: path 0 is stereo (both)
    assert preset["preset"]["flow"][0]["b00"]["slot"][0]["model"] == "P35_InputInst1_2"

    # Snapshot: drive block has snapshots array showing disable in snap 1
    drive_enabled = preset["preset"]["flow"][0]["b01"]["@enabled"]
    assert drive_enabled["snapshots"][1] is False

    # Footswitch: drive block's @enabled has a controller
    assert "controller" in drive_enabled
    assert drive_enabled["controller"]["source"] == 0x01010102  # FS3

    # Expression: amp block's chosen param has a controller
    amp_param_wrapped = preset["preset"]["flow"][0]["b02"]["slot"][0]["params"][amp_param]
    assert "controller" in amp_param_wrapped
    assert amp_param_wrapped["controller"]["source"] == 0x01020100  # EXP1
    assert amp_param_wrapped["controller"]["min"] == 0.1

    # Sources: both source IDs are registered
    sources = preset["preset"]["sources"]
    source_ids = {int(k) for k in sources}
    assert 0x01010102 in source_ids
    assert 0x01020100 in source_ids
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_generate_combined.py -v`
Expected: PASS.

- [ ] **Step 3: Run the whole suite once more**

Run: `pytest -q`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_generate_combined.py
git commit -m "test: end-to-end integration with input, snapshots, FS, and EXP combined"
```

---

## Manual hardware validation (do once before declaring done)

These are not automated tasks; they are a final check before the work is
considered shipped, matching the project's v1 device-validation pattern.

1. Generate a preset with `input: "both"`, two FS assignments (one
   latching, one momentary), and one EXP assignment with custom min/max.
2. Load the generated `.hsp` on the Helix Stadium XL.
3. Plug the guitar into Inst 1 only: confirm signal flows.
4. Plug the guitar into Inst 2 only: confirm signal flows.
5. Tap the FS assigned in latching mode: confirm block toggles.
6. Hold the FS assigned in momentary mode: confirm block is on while
   held and off when released.
7. Sweep the expression pedal: confirm the assigned param moves between
   the spec's `min` and `max`.
8. If all pass, update the `project_helixgen_v1_device_validated` memory
   note (or add a new memory) to record that footswitch / expression /
   dual-input features are device-validated as of the test date.

---

## Self-review notes

The plan covers all sections of both specs:

- **Footswitches spec**: covered by Phase 2 (table) + Phase 3 (parse + generate + docs). Snapshot+controller composition tested in Task 3.2 Step 1's fifth test. Validation rules in Task 3.1.
- **Input-routing spec**: covered by Phase 1 (table + parse + generate + reshape helper + docs). Asymmetric defaults tested in Task 1.4. Mono↔stereo edge cases in Task 1.3.
- **Expression-pedal spec**: covered by Phase 4 (table) + Phase 5 (parse + generate + docs). Multi-target across blocks tested in Task 5.2. The "v1 only 0..1 float params" constraint is enforced by the `block.params` lookup in `_build_exp_assignments` raising on unknown params — non-float params would still be found, so we add no further check at this layer; the user's mistake (sweeping an Hz param) would still produce a syntactically valid but musically odd preset, which the manual validation step catches.
- **Risks** in both specs (EXP wrapper shape, snapshot+controller composition, EXPONBOARD existence): the derivation tasks (Phase 2, Phase 4) and the integration test in Phase 6 are the gates.
