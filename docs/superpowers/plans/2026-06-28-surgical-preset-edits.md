# Surgical Preset Edits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add terse, surgical preset edits (`set-param`, `swap-model`, `enable`/`disable`, `add`/`remove-block`) plus an `.hsp → spec.json` decompiler, so tweaking a tone never means re-authoring the whole spec — whether the tone has a spec or is an orphan `.hsp`.

**Architecture:** The spec is always source of truth. Edit verbs are pure functions that mutate a spec *dict*, validated on regenerate by the existing `generate.py`. Every generated `.hsp` gets a **sidecar spec** (`foo.hsp` ↔ `foo.spec.json`); orphan `.hsp` files are brought into the spec world by a new **decompiler** that reverses `_compose_preset_hsp`. CLI verbs orchestrate load-sidecar-or-decompile → patch → regenerate; MCP exposes `decompile` + a `patch` tool operating on inline spec dicts.

**Tech Stack:** Python 3 stdlib + `click` (CLI) + `mcp`/FastMCP (server). Tests: `pytest` + `stretchr`-style `assert`. No new runtime deps.

## Global Constraints

- Pure stdlib + `click` only for runtime; MCP server may use `mcp` + `click`. No other runtime deps. (CLAUDE.md)
- TDD throughout: failing test first, then minimal implementation. (CLAUDE.md)
- Stadium-only features (routing/snapshots/FS/EXP/IR) apply to `.hsp` chassis; `.hlx` ignores them with a warning. (CLAUDE.md)
- Param values must be type-coerced to the block schema's declared type before emission — reuse `generate._coerce_param_value`; never emit an int where the schema says float. (`generate.py:100`)
- `.hsp` model IDs use the Stadium namespace; library stores `.hlx`-normalized IDs. Translate with `hsp.translate_to_hsp` (write) / `hsp._translate_model_id` (read). (`hsp.py:46`)
- Real-export integration tests are skip-gated on `data/*.hsp` presence so the suite stays green on a clean clone. (`tests/test_generate_input.py:14`)
- Run pytest with `PYTHONPATH=$PWD/src` (an editable global install can shadow the bundled code). (memory: editable-install-shadows-bundled)
- Decompiler fidelity bar is **round-trip stability**, not byte-identity: `compose(spec) → decompile → compose` must reproduce the same preset body (modulo the `meta.helixgen.generated_at` timestamp).

---

## Shared test helpers (referenced by multiple tasks)

Several tasks build an `.hsp`-capable `Library`. Two helpers are introduced in Task 3's test file and reused verbatim by later test files (repeated in each task's code blocks so tasks can be read out of order):

```python
# Synthetic .hsp library: chassis from the conftest hsp fixture + synthetic blocks.
def _hsp_library(tmp_path, sample_serial_preset_hsp):
    import json
    from helixgen.ingest import ingest_path
    from helixgen.library import Block, Library
    hsp_path = tmp_path / "chassis.hsp"
    from helixgen.hsp import HSP_MAGIC
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(hsp_path, lib)  # extracts the Stadium chassis
    # Two synthetic blocks: a drive and an amp. Exemplars are .hlx-normalized
    # (params unwrapped). @type drives the bNN `type` field.
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}, "Tone": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True,
                  "Gain": 0.5, "Tone": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    lib.save_block(Block(
        model_id="HD2_AmpBrit", category="amp", display_name="Brit Amp",
        params={"Drive": {"type": "float"}, "Master": {"type": "float"}},
        exemplar={"@model": "HD2_AmpBrit", "@type": "amp", "@enabled": True,
                  "Drive": 0.5, "Master": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    return lib
```

```python
# Real-export library, skip-gated (strongest acceptance signal).
def _real_hsp_library(tmp_path):
    import pytest
    from pathlib import Path
    from helixgen.ingest import ingest_path
    from helixgen.library import Library
    data_dir = Path(__file__).resolve().parent.parent / "data"
    samples = sorted(data_dir.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    lib = Library(root=tmp_path / "lib")
    for s in samples:
        ingest_path(s, lib)
    return lib, samples
```

---

## Task 1: Reverse controller/input lookups

Decompiling needs the inverse of `resolve_input_model` and `resolve_controller_source`: model→mode and source-id→logical-name. Add pure reverse lookups beside the forward tables so both directions live together.

**Files:**
- Modify: `src/helixgen/controllers.py` (add two functions after `resolve_controller_source`, line ~117)
- Test: `tests/test_controllers.py`

**Interfaces:**
- Produces:
  - `input_mode_for_model(device_id, model: str) -> str | None` — reverse of `resolve_input_model`; `None` if the model isn't a known input model.
  - `controller_name_for_source(device_id, source_id: int) -> str | None` — reverse of `resolve_controller_source`; `None` if the source id is unknown.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controllers.py  (append)
from helixgen import controllers


def test_input_mode_for_model_roundtrips():
    for mode in ("inst1", "inst2", "both", "none"):
        model = controllers.resolve_input_model("stadium_xl", mode)
        assert controllers.input_mode_for_model("stadium_xl", model) == mode


def test_input_mode_for_model_unknown_returns_none():
    assert controllers.input_mode_for_model("stadium_xl", "P35_NotAnInput") is None


def test_controller_name_for_source_roundtrips():
    for name in ("FS1", "FS10", "EXP1", "EXP2"):
        sid = controllers.resolve_controller_source("stadium_xl", name)
        assert controllers.controller_name_for_source("stadium_xl", sid) == name


def test_controller_name_for_source_unknown_returns_none():
    assert controllers.controller_name_for_source("stadium_xl", 0xDEADBEEF) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_controllers.py -q`
Expected: FAIL with `AttributeError: module 'helixgen.controllers' has no attribute 'input_mode_for_model'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/helixgen/controllers.py  (append after resolve_controller_source)
def input_mode_for_model(device_id, model: str) -> str | None:
    """Reverse of resolve_input_model: Stadium input model_id → logical mode."""
    table = INPUT_MODELS[_resolve_device(device_id)]
    for mode, model_id in table.items():
        if model_id == model:
            return mode
    return None


def controller_name_for_source(device_id, source_id: int) -> str | None:
    """Reverse of resolve_controller_source: source id → logical FS/EXP name."""
    table = CONTROLLER_SOURCE_IDS[_resolve_device(device_id)]
    for name, sid in table.items():
        if sid == source_id:
            return name
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_controllers.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/controllers.py tests/test_controllers.py
git commit -m "feat(controllers): reverse lookups for input model + controller source"
```

---

## Task 2: Base-level block enable/disable in the spec

The `enable`/`disable` verbs need a spec field for base (non-snapshot) bypass — today `BlockEntry` has none, so generate always reads `@enabled` from the exemplar. Add an optional `enabled: bool | None` to `BlockEntry`, thread it into `_to_hsp_bnn`'s slot-level `@enabled`, and into the `.hlx` path too.

**Files:**
- Modify: `src/helixgen/spec.py` (`BlockEntry` dataclass line 12; `_parse_block_entry` line 301)
- Modify: `src/helixgen/generate.py` (`_to_hsp_bnn` line 392; `_compose_preset_hsp` block-placement loop line 650; `.hlx` loop line 208)
- Test: `tests/test_spec.py`, `tests/test_generate.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `BlockEntry.enabled: bool | None` (default `None` = use exemplar default). `_to_hsp_bnn(..., enabled_base: bool | None = None)` overrides the slot-level `@enabled` value when not `None`.

- [ ] **Step 1: Write the failing spec test**

```python
# tests/test_spec.py  (append)
from helixgen.spec import parse_spec


def test_block_entry_enabled_parsed():
    spec = parse_spec({"name": "n", "paths": [
        {"blocks": [{"block": "X", "enabled": False}]}]})
    assert spec.paths[0].blocks[0].enabled is False


def test_block_entry_enabled_defaults_none():
    spec = parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}]})
    assert spec.paths[0].blocks[0].enabled is None


def test_block_entry_enabled_must_be_bool():
    import pytest
    from helixgen.spec import SpecError
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [
            {"blocks": [{"block": "X", "enabled": "yes"}]}]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -q -k enabled`
Expected: FAIL (`enabled` attribute missing / not rejected)

- [ ] **Step 3: Implement spec changes**

```python
# src/helixgen/spec.py — BlockEntry (line 12)
@dataclass
class BlockEntry:
    block: str
    params: dict[str, Any] = field(default_factory=dict)
    ir: str | None = None
    enabled: bool | None = None
```

```python
# src/helixgen/spec.py — _parse_block_entry, before the final return (line ~323)
    enabled = data.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise _err(source, '"enabled" must be a boolean if provided.')

    return BlockEntry(block=name, params=dict(params), ir=ir, enabled=enabled)
```

- [ ] **Step 4: Run spec test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_spec.py -q -k enabled`
Expected: PASS

- [ ] **Step 5: Write the failing generate test**

```python
# tests/test_generate.py  (append; uses the shared _hsp_library helper repeated here)
import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def _hsp_library(tmp_path, sample_serial_preset_hsp):
    hsp_path = tmp_path / "chassis.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(hsp_path, lib)
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}, "Tone": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True,
                  "Gain": 0.5, "Tone": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    return lib


def test_block_enabled_false_disables_slot(tmp_path, sample_serial_preset_hsp):
    lib = _hsp_library(tmp_path, sample_serial_preset_hsp)
    spec = parse_spec({"name": "n", "paths": [
        {"blocks": [{"block": "Tube Drive", "enabled": False}]}]})
    preset = compose_preset(spec, lib, source="t")
    slot = preset["preset"]["flow"][0]["b01"]["slot"][0]
    assert slot["@enabled"] == {"value": False}
```

- [ ] **Step 6: Run generate test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py -q -k enabled_false`
Expected: FAIL (slot `@enabled` is `{"value": True}`)

- [ ] **Step 7: Thread `enabled` through generate**

```python
# src/helixgen/generate.py — _to_hsp_bnn signature (line 392): add a kwarg
def _to_hsp_bnn(
    block: Block,
    user_params: dict[str, Any],
    *,
    position: int,
    path_index: int,
    enabled_base: bool | None = None,
    enabled_overrides: list[bool | None] | None = None,
    param_overrides: dict[str, list[Any]] | None = None,
    fs_controller: dict[str, Any] | None = None,
    exp_controllers: dict[str, dict[str, Any]] | None = None,
    irhash: str | None = None,
) -> dict[str, Any]:
```

```python
# src/helixgen/generate.py — _to_hsp_bnn, replace the slot @enabled line (line 423)
    base_enabled = enabled_base if enabled_base is not None else flat.get("@enabled", True)
    slot_inner["@enabled"] = {"value": base_enabled}
```

```python
# src/helixgen/generate.py — _compose_preset_hsp block-placement call (line 662)
            path_dict[key] = _to_hsp_bnn(
                block, user_params,
                position=slot_index,
                path_index=path_index,
                enabled_base=block_entry.enabled,
                enabled_overrides=enabled_map.get((path_index, chain_idx)),
                param_overrides=param_map.get((path_index, chain_idx)),
                fs_controller=fs_map.get((path_index, chain_idx)),
                exp_controllers={
                    pname: ctrl
                    for (pi, ci, pname), ctrl in exp_map.items()
                    if pi == path_index and ci == chain_idx
                } or None,
                irhash=resolved_irhash,
            )
```

For the `.hlx` path, set base enabled from the entry. Replace the `.hlx` inner loop header (line 208) so it carries the spec entry alongside the resolved block:

```python
# src/helixgen/generate.py — _compose_preset_hlx, change the chain loop (line 208)
        for (block, user_params), block_entry in zip(chain, spec.paths[path_index].blocks):
            placed = copy.deepcopy(block.exemplar)
            if block_entry.enabled is not None:
                placed["@enabled"] = block_entry.enabled
            for k, v in user_params.items():
                placed[k] = _coerce_param_value(block, k, v)
```

- [ ] **Step 8: Run generate test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_generate.py -q -k enabled_false`
Expected: PASS

- [ ] **Step 9: Run the full suite to confirm no regressions**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: PASS (all prior tests still green)

- [ ] **Step 10: Commit**

```bash
git add src/helixgen/spec.py src/helixgen/generate.py tests/test_spec.py tests/test_generate.py
git commit -m "feat(spec): base-level block enabled flag, threaded into generate"
```

---

## Task 3: Decompiler core — meta, paths, blocks, params, input, enabled

Build `decompile.py` reversing the structural half of `_compose_preset_hsp`: meta (name/author/device_id/color/info), per-path `input` mode, each `bNN` block's display name + param overrides (only params that differ from the library exemplar) + base `enabled`. Snapshots/FS/EXP/IR come in Task 4.

**Files:**
- Create: `src/helixgen/decompile.py`
- Test: `tests/test_decompile.py`

**Interfaces:**
- Consumes: `hsp.read_hsp`, `hsp._translate_model_id`, `hsp._unwrap_value`; `controllers.input_mode_for_model`; `library.Library.load_block`/`find_block`; `generate._coerce_param_value`.
- Produces:
  - `decompile_body(body: dict, library: Library, irs=None) -> dict` — `.hsp` body dict → spec dict.
  - `decompile(hsp_path, library, irs=None) -> dict` — read file then `decompile_body`.

- [ ] **Step 1: Write the failing round-trip-stability test**

```python
# tests/test_decompile.py
import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.decompile import decompile_body


def _hsp_library(tmp_path, sample_serial_preset_hsp):
    hsp_path = tmp_path / "chassis.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(hsp_path, lib)
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}, "Tone": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True,
                  "Gain": 0.5, "Tone": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    lib.save_block(Block(
        model_id="HD2_AmpBrit", category="amp", display_name="Brit Amp",
        params={"Drive": {"type": "float"}, "Master": {"type": "float"}},
        exemplar={"@model": "HD2_AmpBrit", "@type": "amp", "@enabled": True,
                  "Drive": 0.5, "Master": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    return lib


def _strip_provenance(preset: dict) -> dict:
    p = json.loads(json.dumps(preset))  # deep copy
    p.get("meta", {}).get("helixgen", {}).pop("generated_at", None)
    return p


def test_decompile_roundtrip_stable(tmp_path, sample_serial_preset_hsp):
    lib = _hsp_library(tmp_path, sample_serial_preset_hsp)
    spec1 = parse_spec({
        "name": "RT", "author": "me",
        "paths": [{"input": "inst1", "blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.7}, "enabled": False},
            {"block": "Brit Amp",   "params": {"Drive": 0.8, "Master": 0.6}},
        ]}],
    })
    p1 = compose_preset(spec1, lib, source="t")
    spec2_dict = decompile_body(p1, lib)
    spec2 = parse_spec(spec2_dict)
    p2 = compose_preset(spec2, lib, source="t")
    assert _strip_provenance(p2) == _strip_provenance(p1)


def test_decompile_recovers_meta_and_blocks(tmp_path, sample_serial_preset_hsp):
    lib = _hsp_library(tmp_path, sample_serial_preset_hsp)
    spec1 = parse_spec({"name": "Tone X", "author": "me", "paths": [
        {"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.7}}]}]})
    p1 = compose_preset(spec1, lib, source="t")
    d = decompile_body(p1, lib)
    assert d["name"] == "Tone X"
    assert d["author"] == "me"
    assert d["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert d["paths"][0]["blocks"][0]["params"] == {"Gain": 0.7}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'helixgen.decompile'`)

- [ ] **Step 3: Implement the decompiler core**

```python
# src/helixgen/decompile.py
"""Decompile: reverse a Stadium .hsp body back into a generate-ready spec dict.

The fidelity bar is *round-trip stability*: composing the returned spec must
reproduce the source preset body (modulo the generated_at provenance stamp).
Only values that differ from the library exemplar are emitted, so specs stay
minimal and readable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from helixgen import controllers
from helixgen.generate import _coerce_param_value
from helixgen.hsp import _translate_model_id, _unwrap_value, read_hsp
from helixgen.library import Library


_ENDPOINT_KEYS = frozenset({"b00", "b13"})


def _device_id(body: dict) -> Any:
    return (body.get("meta") or {}).get("device_id") or "stadium_xl"


def _bnn_keys(path_dict: dict) -> list[str]:
    return sorted(
        k for k in path_dict
        if isinstance(k, str) and k.startswith("b")
        and k not in _ENDPOINT_KEYS and k[1:].isdigit()
    )


def _input_mode(path_dict: dict, device_id: Any) -> str | None:
    b00 = path_dict.get("b00")
    if not isinstance(b00, dict) or not b00.get("slot"):
        return None
    model = b00["slot"][0].get("model", "")
    return controllers.input_mode_for_model(device_id, model)


def _block_entry(slot: dict, library: Library) -> dict[str, Any]:
    """One slot dict → a spec block entry (block name + non-default params)."""
    model = _translate_model_id(slot.get("model", ""))
    block = library.load_block(model)
    entry: dict[str, Any] = {"block": block.display_name}

    params: dict[str, Any] = {}
    for name, wrapped in (slot.get("params") or {}).items():
        value = _unwrap_value(wrapped)
        default = block.exemplar.get(name)
        # Coerce the exemplar default to the same type before comparing, so a
        # float-vs-int mismatch doesn't spuriously register as an override.
        if default is not None:
            default = _coerce_param_value(block, name, default)
        coerced = _coerce_param_value(block, name, value)
        if default is None or coerced != default:
            params[name] = coerced
    if params:
        entry["params"] = params

    base_enabled = _unwrap_value(slot.get("@enabled", True))
    exemplar_enabled = block.exemplar.get("@enabled", True)
    if base_enabled != exemplar_enabled:
        entry["enabled"] = base_enabled

    return entry


def decompile_body(body: dict, library: Library, irs=None) -> dict[str, Any]:
    device_id = _device_id(body)
    flow = (body.get("preset") or {}).get("flow") or []

    paths: list[dict[str, Any]] = []
    for path_dict in flow:
        if not isinstance(path_dict, dict):
            continue
        blocks: list[dict[str, Any]] = []
        for key in _bnn_keys(path_dict):
            bnn = path_dict[key]
            if not isinstance(bnn, dict) or not bnn.get("slot"):
                continue
            blocks.append(_block_entry(bnn["slot"][0], library))
        path_entry: dict[str, Any] = {"blocks": blocks}
        mode = _input_mode(path_dict, device_id)
        if mode is not None:
            path_entry["input"] = mode
        paths.append(path_entry)

    meta = body.get("meta") or {}
    spec: dict[str, Any] = {"name": meta.get("name") or "Untitled", "paths": paths}
    if meta.get("author"):
        spec["author"] = meta["author"]
    return spec


def decompile(hsp_path: Path | str, library: Library, irs=None) -> dict[str, Any]:
    return decompile_body(read_hsp(hsp_path), library, irs=irs)
```

Note: the generated default `input` modes are `("both", "none")` by path index; emitting `input` only when `input_mode_for_model` resolves a mode keeps single-path round-trips stable because path 0's `both` is re-emitted explicitly and re-resolves to the same model. The round-trip-stability test is the guard.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile.py
git commit -m "feat(decompile): core .hsp->spec (meta, paths, blocks, params, input, enabled)"
```

---

## Task 4: Decompiler advanced — snapshots, footswitches, expression, IR

Recover the controller-driven features by reversing `_build_snapshot_overrides`, `_build_fs_assignments`, `_build_exp_assignments`, and the IR slot `irhash`.

**Files:**
- Modify: `src/helixgen/decompile.py`
- Test: `tests/test_decompile_advanced.py`

**Interfaces:**
- Consumes (added): `controllers.controller_name_for_source`; `ir.IR_MODEL_PREFIX`, `ir.IrMapping`.
- Produces: `decompile_body` now also emits `snapshots`, `footswitches`, `expression`, and per-block `ir`.

- [ ] **Step 1: Write the failing test (snapshots + FS + EXP round-trip stable)**

```python
# tests/test_decompile_advanced.py
import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.decompile import decompile_body


def _hsp_library(tmp_path, sample_serial_preset_hsp):
    hsp_path = tmp_path / "chassis.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(hsp_path, lib)
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}, "Tone": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True,
                  "Gain": 0.5, "Tone": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    lib.save_block(Block(
        model_id="HD2_AmpBrit", category="amp", display_name="Brit Amp",
        params={"Drive": {"type": "float"}, "Master": {"type": "float"}},
        exemplar={"@model": "HD2_AmpBrit", "@type": "amp", "@enabled": True,
                  "Drive": 0.5, "Master": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    return lib


def _strip_provenance(preset: dict) -> dict:
    p = json.loads(json.dumps(preset))
    p.get("meta", {}).get("helixgen", {}).pop("generated_at", None)
    return p


def _roundtrip(spec_dict, lib):
    p1 = compose_preset(parse_spec(spec_dict), lib, source="t")
    spec2 = parse_spec(decompile_body(p1, lib))
    p2 = compose_preset(spec2, lib, source="t")
    return _strip_provenance(p1), _strip_provenance(p2)


def test_snapshots_roundtrip_stable(tmp_path, sample_serial_preset_hsp):
    lib = _hsp_library(tmp_path, sample_serial_preset_hsp)
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Brit Amp"}]}],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"],
             "params": {"Brit Amp": {"Drive": 0.9}}}]}
    p1, p2 = _roundtrip(spec, lib)
    assert p1 == p2
    d = decompile_body(compose_preset(parse_spec(spec), lib, source="t"), lib)
    names = [s["name"] for s in d["snapshots"]]
    assert names[:2] == ["Rhythm", "Lead"]


def test_footswitch_roundtrip_stable(tmp_path, sample_serial_preset_hsp):
    lib = _hsp_library(tmp_path, sample_serial_preset_hsp)
    spec = {"name": "F", "paths": [{"blocks": [{"block": "Tube Drive"}]}],
            "footswitches": [{"switch": "FS3", "block": "Tube Drive",
                              "behavior": "momentary"}]}
    p1, p2 = _roundtrip(spec, lib)
    assert p1 == p2


def test_expression_roundtrip_stable(tmp_path, sample_serial_preset_hsp):
    lib = _hsp_library(tmp_path, sample_serial_preset_hsp)
    spec = {"name": "E", "paths": [{"blocks": [{"block": "Brit Amp"}]}],
            "expression": [{"pedal": "EXP1", "targets": [
                {"block": "Brit Amp", "param": "Master", "min": 0.1, "max": 0.8}]}]}
    p1, p2 = _roundtrip(spec, lib)
    assert p1 == p2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_advanced.py -q`
Expected: FAIL (decompiled spec lacks snapshots/footswitches/expression → composed `p2` differs from `p1`)

- [ ] **Step 3: Implement advanced recovery**

Add helpers and extend `decompile_body`. Insert before `decompile_body`:

```python
# src/helixgen/decompile.py  (add imports at top)
from helixgen.ir import IR_MODEL_PREFIX, IrMapping
```

```python
# src/helixgen/decompile.py  (new helpers, above decompile_body)
def _iter_blocks(flow):
    """Yield (path_idx, slot, block_display_resolver-ready slot) for user blocks."""
    for path_idx, path_dict in enumerate(flow):
        if not isinstance(path_dict, dict):
            continue
        for key in _bnn_keys(path_dict):
            bnn = path_dict.get(key)
            if isinstance(bnn, dict) and bnn.get("slot"):
                yield path_idx, bnn, bnn["slot"][0]


def _snapshot_names(body: dict) -> list[str]:
    """Names from preset.snapshots, trimmed of trailing `Snap N` placeholders."""
    raw = (body.get("preset") or {}).get("snapshots") or []
    names = [s.get("name", "") for s in raw]
    # A slot is a placeholder iff named exactly "Snap <i+1>".
    keep = 0
    for i, n in enumerate(names):
        if n != f"Snap {i + 1}":
            keep = i + 1
    return names[:keep]


def _recover_snapshots(body: dict, library: Library) -> list[dict[str, Any]]:
    names = _snapshot_names(body)
    if not names:
        return []
    snaps: list[dict[str, Any]] = [
        {"name": n, "disable": [], "params": {}} for n in names
    ]
    flow = (body.get("preset") or {}).get("flow") or []
    for _, bnn, slot in _iter_blocks(flow):
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        # @enabled snapshot overrides (False => disable in that snapshot).
        en = bnn.get("@enabled")
        if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
            for i, ov in enumerate(en["snapshots"]):
                if i < len(snaps) and ov is False:
                    snaps[i]["disable"].append(block.display_name)
        # param snapshot overrides.
        for pname, wrapped in (slot.get("params") or {}).items():
            if not (isinstance(wrapped, dict) and isinstance(wrapped.get("snapshots"), list)):
                continue
            for i, ov in enumerate(wrapped["snapshots"]):
                if i < len(snaps) and ov is not None:
                    snaps[i]["params"].setdefault(block.display_name, {})[pname] = (
                        _coerce_param_value(block, pname, ov))
    # Drop empty disable/params keys for cleanliness.
    for s in snaps:
        if not s["disable"]:
            s.pop("disable")
        if not s["params"]:
            s.pop("params")
    return snaps


def _recover_footswitches(body: dict, library: Library, device_id) -> list[dict[str, Any]]:
    flow = (body.get("preset") or {}).get("flow") or []
    out: list[dict[str, Any]] = []
    for _, bnn, slot in _iter_blocks(flow):
        en = bnn.get("@enabled")
        ctrl = en.get("controller") if isinstance(en, dict) else None
        if not (isinstance(ctrl, dict) and ctrl.get("type") == "targetbypass"):
            continue
        name = controllers.controller_name_for_source(device_id, ctrl.get("source"))
        if name is None:
            continue
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        out.append({"switch": name, "block": block.display_name,
                    "behavior": ctrl.get("behavior", "latching")})
    return out


def _recover_expression(body: dict, library: Library, device_id) -> list[dict[str, Any]]:
    flow = (body.get("preset") or {}).get("flow") or []
    by_pedal: dict[str, list[dict[str, Any]]] = {}
    for _, _bnn, slot in _iter_blocks(flow):
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        for pname, wrapped in (slot.get("params") or {}).items():
            ctrl = wrapped.get("controller") if isinstance(wrapped, dict) else None
            if not (isinstance(ctrl, dict) and ctrl.get("type") == "param"):
                continue
            pedal = controllers.controller_name_for_source(device_id, ctrl.get("source"))
            if pedal is None:
                continue
            by_pedal.setdefault(pedal, []).append({
                "block": block.display_name, "param": pname,
                "min": ctrl.get("min", 0.0), "max": ctrl.get("max", 1.0)})
    return [{"pedal": p, "targets": t} for p, t in by_pedal.items()]
```

Extend `_block_entry` to recover the IR ref, and `decompile_body` to attach the advanced sections:

```python
# src/helixgen/decompile.py — _block_entry: add irs param + ir recovery
def _block_entry(slot: dict, library: Library, irs: IrMapping | None) -> dict[str, Any]:
    model = _translate_model_id(slot.get("model", ""))
    block = library.load_block(model)
    entry: dict[str, Any] = {"block": block.display_name}
    # ... params + enabled logic unchanged ...
    # (append, after enabled handling, before `return entry`)
    if model.startswith(IR_MODEL_PREFIX) and slot.get("irhash"):
        irhash = slot["irhash"]
        basename = None
        if irs is not None:
            for h, p in irs.entries.items():
                if h == irhash:
                    import os
                    basename = os.path.basename(p)
                    break
        if basename is not None:
            entry["ir"] = basename
        elif irhash != getattr(block, "default_irhash", None):
            entry["ir"] = irhash
    return entry
```

```python
# src/helixgen/decompile.py — decompile_body: pass irs, attach sections
def decompile_body(body: dict, library: Library, irs=None) -> dict[str, Any]:
    if irs is None:
        irs = IrMapping.load()
    device_id = _device_id(body)
    flow = (body.get("preset") or {}).get("flow") or []
    paths = []
    for path_dict in flow:
        if not isinstance(path_dict, dict):
            continue
        blocks = []
        for key in _bnn_keys(path_dict):
            bnn = path_dict[key]
            if not isinstance(bnn, dict) or not bnn.get("slot"):
                continue
            blocks.append(_block_entry(bnn["slot"][0], library, irs))
        path_entry = {"blocks": blocks}
        mode = _input_mode(path_dict, device_id)
        if mode is not None:
            path_entry["input"] = mode
        paths.append(path_entry)

    meta = body.get("meta") or {}
    spec: dict[str, Any] = {"name": meta.get("name") or "Untitled", "paths": paths}
    if meta.get("author"):
        spec["author"] = meta["author"]
    snaps = _recover_snapshots(body, library)
    if snaps:
        spec["snapshots"] = snaps
    fs = _recover_footswitches(body, library, device_id)
    if fs:
        spec["footswitches"] = fs
    exp = _recover_expression(body, library, device_id)
    if exp:
        spec["expression"] = exp
    return spec
```

Update the Task 3 call site `_block_entry(bnn["slot"][0], library)` is replaced by the new 3-arg form above.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_advanced.py tests/test_decompile.py -q`
Expected: PASS (both files)

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/decompile.py tests/test_decompile_advanced.py
git commit -m "feat(decompile): recover snapshots, footswitches, expression, IR refs"
```

---

## Task 5: `decompile` CLI command + real-export acceptance round-trip

Expose the decompiler and prove round-trip stability against the user's real exports (skip-gated).

**Files:**
- Modify: `src/helixgen/cli.py` (new command after `generate_cmd`, line ~112)
- Test: `tests/test_decompile_cli.py`, `tests/test_decompile_acceptance.py`

**Interfaces:**
- Consumes: `decompile.decompile`.
- Produces: CLI `helixgen decompile <preset.hsp> -o <spec.json>`.

- [ ] **Step 1: Write the failing CLI test**

```python
# tests/test_decompile_cli.py
import json
from click.testing import CliRunner
from helixgen.cli import cli
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def test_decompile_cmd_writes_spec(tmp_path, sample_serial_preset_hsp, monkeypatch):
    # Build a library + a generated .hsp on disk.
    lib_root = tmp_path / "lib"
    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=lib_root)
    ingest_path(chassis, lib)
    lib.save_block(Block(
        model_id="HD2_DistTube", category="drive", display_name="Tube Drive",
        params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))
    preset = compose_preset(parse_spec(
        {"name": "CLI", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.9}}]}]}),
        lib, source="t")
    hsp_path = tmp_path / "in.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(preset).encode())

    out = tmp_path / "out.spec.json"
    res = CliRunner().invoke(cli, [
        "decompile", str(hsp_path), "-o", str(out), "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    spec = json.loads(out.read_text())
    assert spec["name"] == "CLI"
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_cli.py -q`
Expected: FAIL (`No such command 'decompile'`)

- [ ] **Step 3: Implement the CLI command**

```python
# src/helixgen/cli.py — after generate_cmd (line ~112)
@cli.command(name="decompile")
@click.argument("hsp_path", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", "output_path", type=click.Path(path_type=Path), required=True)
@_library_option
@_irs_option
def decompile_cmd(
    hsp_path: Path, output_path: Path, library_path: Path | None, irs_dir: Path | None
) -> None:
    """Reconstruct a spec.json from a Stadium .hsp preset."""
    import json as _json
    from helixgen.decompile import decompile
    library = _resolved_library(library_path)
    irs = _resolved_irs(irs_dir)
    try:
        spec = decompile(hsp_path, library, irs=irs)
    except (KeyError, LookupError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_json.dumps(spec, indent=2))
    click.echo(f"Wrote {output_path}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_cli.py -q`
Expected: PASS

- [ ] **Step 5: Write the real-export acceptance test**

```python
# tests/test_decompile_acceptance.py
import json
import pytest
from pathlib import Path
from helixgen.ingest import ingest_path
from helixgen.library import Library
from helixgen.hsp import read_hsp
from helixgen.decompile import decompile_body
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def _real_hsp_library(tmp_path):
    data_dir = Path(__file__).resolve().parent.parent / "data"
    samples = sorted(data_dir.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    lib = Library(root=tmp_path / "lib")
    for s in samples:
        ingest_path(s, lib)
    return lib, samples


def _strip(p):
    p = json.loads(json.dumps(p))
    p.get("meta", {}).get("helixgen", {}).pop("generated_at", None)
    return p


def test_real_export_decompile_roundtrip_stable(tmp_path):
    lib, samples = _real_hsp_library(tmp_path)
    for sample in samples:
        body = read_hsp(sample)
        spec = parse_spec(decompile_body(body, lib))
        regen = compose_preset(spec, lib, source=str(sample))
        # Compare flow block models — the load-bearing, decompiler-owned content.
        def models(b):
            out = []
            for path in (b.get("preset") or {}).get("flow") or []:
                for k in sorted(path):
                    if k.startswith("b") and k not in ("b00", "b13") and k[1:].isdigit():
                        slot = path[k].get("slot", [{}])[0]
                        out.append(slot.get("model"))
            return out
        assert models(_strip(regen)) == models(_strip(body)), sample.name
```

- [ ] **Step 6: Run acceptance test (passes or skips cleanly)**

Run: `PYTHONPATH=$PWD/src pytest tests/test_decompile_acceptance.py -q`
Expected: PASS (or SKIP on a clean clone with empty `data/`)

- [ ] **Step 7: Commit**

```bash
git add src/helixgen/cli.py tests/test_decompile_cli.py tests/test_decompile_acceptance.py
git commit -m "feat(cli): decompile command + real-export round-trip acceptance test"
```

---

## Task 6: Pure patch verbs — addressing, set-param, enable/disable, add/remove

`patch.py` holds pure spec-dict transforms with block addressing. No file IO, no regeneration — fully unit-testable.

**Files:**
- Create: `src/helixgen/patch.py`
- Test: `tests/test_patch.py`

**Interfaces:**
- Produces:
  - `PatchError(ValueError)`.
  - `resolve_block(spec: dict, name: str, path: int | None, index: int | None) -> tuple[int, int]` — returns `(path_idx, block_idx)`; raises `PatchError` on no-match or ambiguity.
  - `set_param(spec, block, param, value, *, path=None, index=None) -> dict`
  - `set_enabled(spec, block, enabled: bool, *, path=None, index=None, snapshot=None) -> dict`
  - `add_block(spec, block, *, path=0, after=None, params=None) -> dict`
  - `remove_block(spec, block, *, path=None, index=None) -> dict`
  - All mutate a deep copy and return the new spec dict.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_patch.py
import pytest
from helixgen import patch


def _spec():
    return {"name": "P", "paths": [{"blocks": [
        {"block": "Tube Drive", "params": {"Gain": 0.5}},
        {"block": "Brit Amp", "params": {"Drive": 0.6}}]}]}


def test_resolve_block_unique():
    assert patch.resolve_block(_spec(), "Brit Amp", None, None) == (0, 1)


def test_resolve_block_missing_raises():
    with pytest.raises(patch.PatchError):
        patch.resolve_block(_spec(), "Nope", None, None)


def test_resolve_block_ambiguous_requires_index():
    s = {"name": "P", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Tube Drive"}]}]}
    with pytest.raises(patch.PatchError):
        patch.resolve_block(s, "Tube Drive", None, None)
    assert patch.resolve_block(s, "Tube Drive", 0, 1) == (0, 1)


def test_set_param():
    out = patch.set_param(_spec(), "Tube Drive", "Gain", 0.9)
    assert out["paths"][0]["blocks"][0]["params"]["Gain"] == 0.9


def test_set_enabled_base():
    out = patch.set_enabled(_spec(), "Tube Drive", False)
    assert out["paths"][0]["blocks"][0]["enabled"] is False


def test_set_enabled_in_snapshot():
    s = _spec()
    s["snapshots"] = [{"name": "Lead"}]
    out = patch.set_enabled(s, "Tube Drive", False, snapshot="Lead")
    assert "Tube Drive" in out["snapshots"][0]["disable"]


def test_add_block_after():
    out = patch.add_block(_spec(), "Plate Stereo", after="Brit Amp",
                          params={"Mix": 0.2})
    names = [b["block"] for b in out["paths"][0]["blocks"]]
    assert names == ["Tube Drive", "Brit Amp", "Plate Stereo"]


def test_remove_block():
    out = patch.remove_block(_spec(), "Tube Drive")
    names = [b["block"] for b in out["paths"][0]["blocks"]]
    assert names == ["Brit Amp"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_patch.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'helixgen.patch'`)

- [ ] **Step 3: Implement the pure verbs**

```python
# src/helixgen/patch.py
"""Pure spec-dict transforms for surgical preset edits.

Each verb deep-copies the spec, mutates it, and returns the new dict. Block
addressing is by display name, disambiguated by (path, index). Validation of
param names/ranges is deferred to generate.py at regeneration time.
"""
from __future__ import annotations

import copy
from typing import Any


class PatchError(ValueError):
    """A surgical edit could not be applied (bad address, etc.)."""


def resolve_block(spec: dict, name: str, path: int | None, index: int | None) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    for pi, p in enumerate(spec.get("paths", [])):
        for bi, b in enumerate(p.get("blocks", [])):
            if b.get("block") == name:
                matches.append((pi, bi))
    if path is not None and index is not None:
        if (path, index) in matches:
            return (path, index)
        raise PatchError(f"No block {name!r} at path {path} index {index}.")
    if not matches:
        raise PatchError(f"Block {name!r} is not in the spec. Placed blocks: "
                         f"{[b.get('block') for p in spec.get('paths', []) for b in p.get('blocks', [])]}.")
    if len(matches) > 1:
        raise PatchError(f"Block {name!r} matches {len(matches)} placements; "
                         f"disambiguate with --path/--index.")
    return matches[0]


def set_param(spec, block, param, value, *, path=None, index=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index)
    out["paths"][pi]["blocks"][bi].setdefault("params", {})[param] = value
    return out


def set_enabled(spec, block, enabled, *, path=None, index=None, snapshot=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index)
    if snapshot is None:
        out["paths"][pi]["blocks"][bi]["enabled"] = enabled
        return out
    snaps = out.get("snapshots", [])
    target = next((s for s in snaps if s.get("name") == snapshot), None)
    if target is None:
        raise PatchError(f"No snapshot named {snapshot!r}.")
    disable = target.setdefault("disable", [])
    if enabled and block in disable:
        disable.remove(block)
    elif not enabled and block not in disable:
        disable.append(block)
    return out


def add_block(spec, block, *, path=0, after=None, params=None) -> dict:
    out = copy.deepcopy(spec)
    if path >= len(out.get("paths", [])):
        raise PatchError(f"No path {path} in spec.")
    blocks = out["paths"][path]["blocks"]
    entry: dict[str, Any] = {"block": block}
    if params:
        entry["params"] = dict(params)
    if after is None:
        blocks.append(entry)
    else:
        _, bi = resolve_block(out, after, path, None)
        blocks.insert(bi + 1, entry)
    return out


def remove_block(spec, block, *, path=None, index=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index)
    del out["paths"][pi]["blocks"][bi]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_patch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/patch.py tests/test_patch.py
git commit -m "feat(patch): pure spec verbs — set-param, enable/disable, add/remove + addressing"
```

---

## Task 7: `swap_model` verb (category check, param carryover, warnings, IR)

The one verb with smarts: replace a block with another of the **same category**, carry over same-named params, drop the rest with a warning, preserve the `ir` ref when the target is an IR block.

**Files:**
- Modify: `src/helixgen/patch.py`
- Test: `tests/test_patch_swap.py`

**Interfaces:**
- Consumes: `library.Library.find_block`; `ir.IR_MODEL_PREFIX`.
- Produces: `swap_model(spec, old, new, library, *, path=None, index=None) -> tuple[dict, list[str]]` — returns `(new_spec, warnings)`. Raises `PatchError` on cross-category swap or unknown target.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_patch_swap.py
import pytest
from helixgen import patch
from helixgen.library import Block, Library


def _lib(tmp_path):
    lib = Library(root=tmp_path / "lib")
    lib.save_block(Block(model_id="HD2_AmpA", category="amp", display_name="Amp A",
        params={"Drive": {"type": "float"}, "Master": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    lib.save_block(Block(model_id="HD2_AmpB", category="amp", display_name="Amp B",
        params={"Drive": {"type": "float"}, "Presence": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    lib.save_block(Block(model_id="HD2_CabC", category="cab", display_name="Cab C",
        params={"HighCut": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    return lib


def _spec():
    return {"name": "S", "paths": [{"blocks": [
        {"block": "Amp A", "params": {"Drive": 0.8, "Master": 0.6}}]}]}


def test_swap_same_category_carries_shared_params(tmp_path):
    out, warns = patch.swap_model(_spec(), "Amp A", "Amp B", _lib(tmp_path))
    b = out["paths"][0]["blocks"][0]
    assert b["block"] == "Amp B"
    assert b["params"]["Drive"] == 0.8        # shared param carried
    assert "Master" not in b["params"]        # dropped (not on Amp B)
    assert any("Master" in w for w in warns)  # warned about the drop


def test_swap_cross_category_refused(tmp_path):
    with pytest.raises(patch.PatchError):
        patch.swap_model(_spec(), "Amp A", "Cab C", _lib(tmp_path))


def test_swap_unknown_target_refused(tmp_path):
    with pytest.raises(patch.PatchError):
        patch.swap_model(_spec(), "Amp A", "Ghost", _lib(tmp_path))


def test_swap_preserves_ir_ref(tmp_path):
    lib = Library(root=tmp_path / "lib")
    lib.save_block(Block(model_id="HX2_ImpulseResponse1", category="cab",
        display_name="IR One", params={"Mix": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    lib.save_block(Block(model_id="HX2_ImpulseResponse2", category="cab",
        display_name="IR Two", params={"Mix": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "IR One", "ir": "my.wav", "params": {"Mix": 1.0}}]}]}
    out, _ = patch.swap_model(spec, "IR One", "IR Two", lib)
    assert out["paths"][0]["blocks"][0]["ir"] == "my.wav"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_patch_swap.py -q`
Expected: FAIL (`module 'helixgen.patch' has no attribute 'swap_model'`)

- [ ] **Step 3: Implement `swap_model`**

```python
# src/helixgen/patch.py  (append; add imports at top of file)
from helixgen.ir import IR_MODEL_PREFIX
from helixgen.library import Library


def swap_model(spec, old, new, library: Library, *, path=None, index=None):
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, old, path, index)
    entry = out["paths"][pi]["blocks"][bi]

    try:
        old_block = library.find_block(old)
        new_block = library.find_block(new)
    except (KeyError, LookupError) as e:
        raise PatchError(str(e)) from e

    if old_block.category != new_block.category:
        raise PatchError(
            f"Cannot swap {old!r} ({old_block.category}) for {new!r} "
            f"({new_block.category}): categories differ.")

    warnings: list[str] = []
    old_params = entry.get("params", {})
    new_keys = set(new_block.params.keys())
    carried = {k: v for k, v in old_params.items() if k in new_keys}
    dropped = sorted(set(old_params) - new_keys)
    if dropped:
        warnings.append(
            f"swap {old!r}→{new!r}: dropped param(s) {dropped} not on target.")

    entry["block"] = new_block.display_name
    if carried:
        entry["params"] = carried
    else:
        entry.pop("params", None)

    # Preserve IR ref only when the target is also an IR block.
    if entry.get("ir") is not None and not new_block.model_id.startswith(IR_MODEL_PREFIX):
        entry.pop("ir", None)
        warnings.append(f"swap {old!r}→{new!r}: dropped 'ir' (target is not an IR block).")

    return out, warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_patch_swap.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/helixgen/patch.py tests/test_patch_swap.py
git commit -m "feat(patch): swap-model with category check, param carryover, IR preservation"
```

---

## Task 8: Sidecar emission + load-or-decompile orchestration

Tie the file workflow together: `generate` writes `foo.spec.json` next to `foo.hsp`; a loader returns the spec for a preset (sidecar if present, else decompile and write the sidecar).

**Files:**
- Modify: `src/helixgen/generate.py` (`generate_preset`, line 697)
- Create: `src/helixgen/preset_io.py`
- Test: `tests/test_preset_io.py`

**Interfaces:**
- Consumes: `decompile.decompile`, `library.Library`, `ir.IrMapping`.
- Produces:
  - `sidecar_path(hsp_path: Path) -> Path` — `foo.hsp` → `foo.spec.json`.
  - `load_spec_for_preset(preset_path: Path, library, irs=None) -> tuple[dict, Path]` — returns `(spec_dict, spec_path)`. For `.json`: load directly. For `.hsp`: sidecar if it exists; else decompile, write the sidecar, return it.
  - `generate_preset` writes the sidecar spec for `.hsp` output (copies the input spec text).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_preset_io.py
import json
from pathlib import Path
from helixgen import preset_io
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import generate_preset


def _lib_and_chassis(tmp_path, sample_serial_preset_hsp):
    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(model_id="HD2_DistTube", category="drive",
        display_name="Tube Drive", params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    return lib


def test_sidecar_path():
    assert preset_io.sidecar_path(Path("/a/foo.hsp")) == Path("/a/foo.spec.json")


def test_generate_writes_sidecar(tmp_path, sample_serial_preset_hsp):
    lib = _lib_and_chassis(tmp_path, sample_serial_preset_hsp)
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "Side", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, lib)
    sidecar = preset_io.sidecar_path(out)
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())["name"] == "Side"


def test_load_spec_uses_sidecar(tmp_path, sample_serial_preset_hsp):
    lib = _lib_and_chassis(tmp_path, sample_serial_preset_hsp)
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "Side", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, lib)
    spec, path = preset_io.load_spec_for_preset(out, lib)
    assert spec["name"] == "Side"
    assert path == preset_io.sidecar_path(out)


def test_load_spec_decompiles_orphan(tmp_path, sample_serial_preset_hsp):
    lib = _lib_and_chassis(tmp_path, sample_serial_preset_hsp)
    # Generate, then delete the sidecar to simulate an orphan.
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "Orphan", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, lib)
    preset_io.sidecar_path(out).unlink()
    spec, path = preset_io.load_spec_for_preset(out, lib)
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert path.exists()  # sidecar written on decompile
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_preset_io.py -q`
Expected: FAIL (`No module named 'helixgen.preset_io'`)

- [ ] **Step 3: Implement sidecar in `generate_preset`**

```python
# src/helixgen/generate.py — generate_preset, replace the .hsp write branch (line 722)
    if shape == "hsp":
        body = json.dumps(preset, separators=(",", ":")).encode("utf-8")
        output_path.write_bytes(HSP_MAGIC + body)
        # Sidecar spec beside the .hsp (source of truth for surgical edits).
        sidecar = output_path.with_name(output_path.stem + ".spec.json")
        sidecar.write_text(json.dumps(raw, indent=2))
    else:
        output_path.write_text(json.dumps(preset, indent=2))
    return output_path
```

- [ ] **Step 4: Implement `preset_io.py`**

```python
# src/helixgen/preset_io.py
"""Sidecar-spec convention + load-or-decompile orchestration for surgical edits."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helixgen.decompile import decompile
from helixgen.library import Library


def sidecar_path(hsp_path: Path) -> Path:
    hsp_path = Path(hsp_path)
    return hsp_path.with_name(hsp_path.stem + ".spec.json")


def load_spec_for_preset(preset_path: Path, library: Library, irs=None) -> tuple[dict, Path]:
    """Return (spec_dict, spec_path) for a preset.

    - .json input → loaded directly.
    - .hsp input  → sidecar if present; else decompile, write the sidecar,
      and return it.
    """
    preset_path = Path(preset_path)
    if preset_path.suffix == ".json":
        return json.loads(preset_path.read_text()), preset_path
    side = sidecar_path(preset_path)
    if side.exists():
        return json.loads(side.read_text()), side
    spec = decompile(preset_path, library, irs=irs)
    side.write_text(json.dumps(spec, indent=2))
    return spec, side
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_preset_io.py -q`
Expected: PASS

- [ ] **Step 6: Run full suite (sidecar must not break existing generate tests)**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/helixgen/generate.py src/helixgen/preset_io.py tests/test_preset_io.py
git commit -m "feat: sidecar spec emission + load-or-decompile orchestration"
```

---

## Task 9: CLI verbs wiring patch + regenerate

Expose `set-param`, `swap-model`, `enable`, `disable`, `add-block`, `remove-block` as CLI commands. Each loads the spec (sidecar/decompile), applies the pure verb, writes the spec back, and regenerates the `.hsp` when the target was an `.hsp`.

**Files:**
- Modify: `src/helixgen/cli.py` (new commands after `decompile_cmd`)
- Test: `tests/test_patch_cli.py`

**Interfaces:**
- Consumes: `preset_io.load_spec_for_preset`, `preset_io.sidecar_path`, `patch.*`, `generate.generate_preset`, `patch.PatchError`.
- Produces: a shared `_apply_and_save(preset_path, library, irs, mutate) -> None` helper plus six commands.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_patch_cli.py
import json
from click.testing import CliRunner
from helixgen.cli import cli
from helixgen.hsp import HSP_MAGIC, read_hsp
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import generate_preset


def _setup(tmp_path, sample_serial_preset_hsp):
    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib_root = tmp_path / "lib"
    lib = Library(root=lib_root)
    ingest_path(chassis, lib)
    lib.save_block(Block(model_id="HD2_DistTube", category="drive",
        display_name="Tube Drive", params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "C", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, lib)
    return lib_root, out


def test_cli_set_param_regenerates(tmp_path, sample_serial_preset_hsp):
    lib_root, out = _setup(tmp_path, sample_serial_preset_hsp)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Tube Drive", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    slot = body["preset"]["flow"][0]["b01"]["slot"][0]
    assert slot["params"]["Gain"]["value"] == 0.9
    # Sidecar updated too.
    side = out.with_name(out.stem + ".spec.json")
    assert json.loads(side.read_text())["paths"][0]["blocks"][0]["params"]["Gain"] == 0.9


def test_cli_disable_block(tmp_path, sample_serial_preset_hsp):
    lib_root, out = _setup(tmp_path, sample_serial_preset_hsp)
    res = CliRunner().invoke(cli, [
        "disable", str(out), "Tube Drive", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["@enabled"]["value"] is False


def test_cli_unknown_block_errors(tmp_path, sample_serial_preset_hsp):
    lib_root, out = _setup(tmp_path, sample_serial_preset_hsp)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Ghost", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code != 0
    assert "Ghost" in res.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_patch_cli.py -q`
Expected: FAIL (`No such command 'set-param'`)

- [ ] **Step 3: Implement the shared helper + commands**

```python
# src/helixgen/cli.py — append after decompile_cmd
def _coerce_cli_value(raw: str):
    """Parse a CLI param value: bool, int, float, else string."""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _apply_and_save(preset_path: Path, library, irs, mutate) -> list[str]:
    """Load spec (sidecar/decompile), apply `mutate`, persist spec + regen .hsp."""
    import json as _json
    from helixgen.preset_io import load_spec_for_preset, sidecar_path
    from helixgen.generate import generate_preset
    spec, spec_path = load_spec_for_preset(preset_path, library, irs=irs)
    new_spec, warnings = mutate(spec)
    spec_path.write_text(_json.dumps(new_spec, indent=2))
    if Path(preset_path).suffix == ".hsp":
        generate_preset(spec_path, Path(preset_path), library, irs=irs)
    return warnings


def _patch_command(mutate_factory):
    """Decorator-free helper: build the library/irs, run, translate errors."""
    from helixgen.patch import PatchError
    from helixgen.spec import SpecError
    from helixgen.generate import ParamValidationError, GenerateError

    def run(preset_path, library_path, irs_dir, *args, **kwargs):
        library = _resolved_library(library_path)
        irs = _resolved_irs(irs_dir)
        try:
            warnings = _apply_and_save(
                preset_path, library, irs,
                lambda spec: mutate_factory(spec, library, *args, **kwargs))
        except (PatchError, KeyError, LookupError, SpecError,
                ParamValidationError, GenerateError) as e:
            raise click.ClickException(str(e)) from e
        for w in warnings:
            click.echo(f"warning: {w}", err=True)
        click.echo(f"Patched {preset_path}")
    return run


@cli.command(name="set-param")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.argument("param")
@click.argument("value")
@click.option("--path", type=int, default=None)
@click.option("--index", type=int, default=None)
@_library_option
@_irs_option
def set_param_cmd(preset_path, block, param, value, path, index, library_path, irs_dir):
    """Set a block param: helixgen set-param preset.hsp "Brit Amp" Drive 0.85"""
    from helixgen import patch
    _patch_command(lambda spec, lib: (
        patch.set_param(spec, block, param, _coerce_cli_value(value),
                        path=path, index=index), [])
    )(preset_path, library_path, irs_dir)


@cli.command(name="enable")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--snapshot", default=None)
@click.option("--path", type=int, default=None)
@click.option("--index", type=int, default=None)
@_library_option
@_irs_option
def enable_cmd(preset_path, block, snapshot, path, index, library_path, irs_dir):
    """Enable (un-bypass) a block."""
    from helixgen import patch
    _patch_command(lambda spec, lib: (
        patch.set_enabled(spec, block, True, path=path, index=index, snapshot=snapshot), [])
    )(preset_path, library_path, irs_dir)


@cli.command(name="disable")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--snapshot", default=None)
@click.option("--path", type=int, default=None)
@click.option("--index", type=int, default=None)
@_library_option
@_irs_option
def disable_cmd(preset_path, block, snapshot, path, index, library_path, irs_dir):
    """Disable (bypass) a block."""
    from helixgen import patch
    _patch_command(lambda spec, lib: (
        patch.set_enabled(spec, block, False, path=path, index=index, snapshot=snapshot), [])
    )(preset_path, library_path, irs_dir)


@cli.command(name="add-block")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--path", type=int, default=0)
@click.option("--after", default=None)
@_library_option
@_irs_option
def add_block_cmd(preset_path, block, path, after, library_path, irs_dir):
    """Add a block to a path (optionally after another block)."""
    from helixgen import patch
    _patch_command(lambda spec, lib: (
        patch.add_block(spec, block, path=path, after=after), [])
    )(preset_path, library_path, irs_dir)


@cli.command(name="remove-block")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("block")
@click.option("--path", type=int, default=None)
@click.option("--index", type=int, default=None)
@_library_option
@_irs_option
def remove_block_cmd(preset_path, block, path, index, library_path, irs_dir):
    """Remove a block from a path."""
    from helixgen import patch
    _patch_command(lambda spec, lib: (
        patch.remove_block(spec, block, path=path, index=index), [])
    )(preset_path, library_path, irs_dir)


@cli.command(name="swap-model")
@click.argument("preset_path", type=click.Path(exists=True, path_type=Path))
@click.argument("old")
@click.argument("new")
@click.option("--path", type=int, default=None)
@click.option("--index", type=int, default=None)
@_library_option
@_irs_option
def swap_model_cmd(preset_path, old, new, path, index, library_path, irs_dir):
    """Swap a block for another of the same category."""
    from helixgen import patch
    _patch_command(lambda spec, lib:
        patch.swap_model(spec, old, new, lib, path=path, index=index)
    )(preset_path, library_path, irs_dir)
```

Note: the `_patch_command(...)` factory receives `mutate_factory(spec, library, ...)`; the lambdas above ignore extra args and close over the parsed click params directly, returning `(new_spec, warnings)`. `swap_model` already returns that tuple; the simpler verbs wrap their result as `(new_spec, [])`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_patch_cli.py -q`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/helixgen/cli.py tests/test_patch_cli.py
git commit -m "feat(cli): surgical edit verbs — set-param/enable/disable/add/remove/swap-model"
```

---

## Task 10: MCP tools — `decompile_preset` + `patch_preset`

Expose the decompiler and a batch patch over inline spec dicts, so the `tone` skill can drive edits via the MCP server. MCP stays file-stateless: `decompile_preset` takes a base64 `.hsp` blob → spec dict; `patch_preset` takes a spec dict + an operation list → new spec dict + warnings.

**Files:**
- Modify: `mcp_server/tools.py` (new handlers)
- Modify: `mcp_server/server.py` (register two FastMCP tools)
- Test: `tests/mcp_server/test_patch_tools.py`

**Interfaces:**
- Consumes: `decompile.decompile_body`, `hsp.read_hsp`/`is_hsp_bytes`, `patch.*`.
- Produces:
  - `decompile_preset_handler(library, model, hsp_b64: str) -> dict` — returns the spec dict.
  - `patch_preset_handler(library, model, spec: dict, operations: list[dict]) -> dict` — returns `{"spec": <dict>, "warnings": [<str>]}`. Each op is `{"op": "set_param"|"set_enabled"|"add_block"|"remove_block"|"swap_model", ...}`.

- [ ] **Step 1: Write the failing handler tests**

```python
# tests/mcp_server/test_patch_tools.py
import base64, json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from mcp_server import tools

MODEL = "Helix Stadium XL"


def _lib(tmp_path, sample_serial_preset_hsp):
    chassis = tmp_path / "c.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(model_id="HD2_DistTube", category="drive",
        display_name="Tube Drive", params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistTube", "@type": "fx", "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    return lib


def test_decompile_preset_handler(tmp_path, sample_serial_preset_hsp):
    lib = _lib(tmp_path, sample_serial_preset_hsp)
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), lib, source="t")
    blob = base64.b64encode(HSP_MAGIC + json.dumps(preset).encode()).decode()
    spec = tools.decompile_preset_handler(lib, MODEL, blob)
    assert spec["name"] == "M"
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"


def test_patch_preset_handler_set_param(tmp_path, sample_serial_preset_hsp):
    lib = _lib(tmp_path, sample_serial_preset_hsp)
    spec = {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}
    res = tools.patch_preset_handler(lib, MODEL, spec,
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9}])
    assert res["spec"]["paths"][0]["blocks"][0]["params"]["Gain"] == 0.9
    assert res["warnings"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/mcp_server/test_patch_tools.py -q`
Expected: FAIL (`module 'mcp_server.tools' has no attribute 'decompile_preset_handler'`)

- [ ] **Step 3: Implement the handlers**

```python
# mcp_server/tools.py  (append; imports near the top of the file)
import base64 as _base64
from helixgen.decompile import decompile_body
from helixgen.hsp import HSP_MAGIC
from helixgen import patch as _patch


def decompile_preset_handler(library, model: str, hsp_b64: str) -> dict:
    """Decompile a base64-encoded .hsp blob into a spec dict."""
    _validate_model(model)
    raw = _base64.b64decode(hsp_b64)
    if raw[:len(HSP_MAGIC)] != HSP_MAGIC:
        raise ValueError("payload is not a .hsp blob (missing magic header)")
    body = json.loads(raw[len(HSP_MAGIC):].decode("utf-8"))
    return decompile_body(body, library)


_PATCH_OPS = {
    "set_param": lambda lib, spec, o: (
        _patch.set_param(spec, o["block"], o["param"], o["value"],
                         path=o.get("path"), index=o.get("index")), []),
    "set_enabled": lambda lib, spec, o: (
        _patch.set_enabled(spec, o["block"], o["enabled"],
                           path=o.get("path"), index=o.get("index"),
                           snapshot=o.get("snapshot")), []),
    "add_block": lambda lib, spec, o: (
        _patch.add_block(spec, o["block"], path=o.get("path", 0),
                         after=o.get("after"), params=o.get("params")), []),
    "remove_block": lambda lib, spec, o: (
        _patch.remove_block(spec, o["block"],
                            path=o.get("path"), index=o.get("index")), []),
    "swap_model": lambda lib, spec, o: _patch.swap_model(
        spec, o["old"], o["new"], lib, path=o.get("path"), index=o.get("index")),
}


def patch_preset_handler(library, model: str, spec: dict, operations: list) -> dict:
    """Apply a sequence of patch ops to a spec dict. Returns {spec, warnings}."""
    _validate_model(model)
    warnings: list[str] = []
    current = spec
    for o in operations:
        op = o.get("op")
        if op not in _PATCH_OPS:
            raise ValueError(f"unknown patch op {op!r}; valid: {sorted(_PATCH_OPS)}")
        current, warns = _PATCH_OPS[op](library, current, o)
        warnings.extend(warns)
    return {"spec": current, "warnings": warnings}
```

- [ ] **Step 4: Run handler tests to verify they pass**

Run: `PYTHONPATH=$PWD/src pytest tests/mcp_server/test_patch_tools.py -q`
Expected: PASS

- [ ] **Step 5: Register the FastMCP tools**

```python
# mcp_server/server.py  (append two tools, mirroring the existing pattern at line 60)
@app.tool()
def decompile_preset(model: str, hsp_b64: str) -> dict[str, Any]:
    """Decompile a base64-encoded Stadium .hsp into an editable spec dict.

    Use this to bring an orphan/ingested preset into the spec world before
    applying surgical edits with patch_preset.
    """
    return _tools.decompile_preset_handler(_resolve_library(), model, hsp_b64)


@app.tool()
def patch_preset(model: str, spec: dict[str, Any], operations: list) -> dict[str, Any]:
    """Apply surgical edits to a spec dict and return {spec, warnings}.

    operations is a list of {"op": ...} dicts. Ops: set_param (block, param,
    value), set_enabled (block, enabled, [snapshot]), add_block (block,
    [path], [after], [params]), remove_block (block), swap_model (old, new).
    Regenerate the .hsp afterwards with generate_preset(spec=<returned spec>).
    """
    return _tools.patch_preset_handler(_resolve_library(), model, spec, operations)
```

- [ ] **Step 6: Verify the server imports cleanly**

Run: `PYTHONPATH=$PWD/src python -c "import mcp_server.server"`
Expected: no output, exit 0

- [ ] **Step 7: Commit**

```bash
git add mcp_server/tools.py mcp_server/server.py tests/mcp_server/test_patch_tools.py
git commit -m "feat(mcp): decompile_preset + patch_preset tools"
```

---

## Task 11: Teach the `tone` skill the patch loop

Document the new workflow in the `tone` skill so adjustments to an existing tone become surgical patches, not full regenerations.

**Files:**
- Modify: `.claude/skills/tone/SKILL.md` (the source-of-truth copy in the repo)
- Test: `tests/test_skills.py` (assert the skill mentions the patch loop)

**Interfaces:**
- Consumes: nothing (documentation).
- Produces: a "Adjusting an existing tone" section the skill follows.

- [ ] **Step 1: Locate the repo's tone SKILL.md**

Run: `ls .claude/skills/tone/SKILL.md`
Expected: the file path prints (this is the checked-in copy that ships in the plugin).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_skills.py  (append)
from pathlib import Path


def test_tone_skill_documents_patch_loop():
    skill = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "tone" / "SKILL.md"
    text = skill.read_text()
    assert "patch_preset" in text
    assert "decompile" in text
    # The skill must prefer surgical edits for adjustments.
    assert "Adjusting an existing tone" in text
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src pytest tests/test_skills.py -q -k patch_loop`
Expected: FAIL (strings absent)

- [ ] **Step 4: Add the workflow section to the skill**

Append to `.claude/skills/tone/SKILL.md`:

```markdown
## Adjusting an existing tone (surgical edits)

When the user asks to *tweak* a tone you already generated (e.g. "brighter
cab", "swap to a Plexi", "more delay", "kill the reverb"), do NOT regenerate
from a fresh description. Apply the narrowest surgical edit instead:

1. If you still hold the spec dict, call `patch_preset(model, spec, operations)`
   with the smallest op that expresses the change:
   - "brighter" → `set_param` on the cab `HighCut` (raise it).
   - "swap to a Plexi" → `swap_model` (old → new amp; same category required).
   - "kill the reverb" → `set_enabled` with `enabled: false` on the reverb block.
   - "add a delay" → `add_block` with the delay block, `after` the amp/cab.
2. If you only have the `.hsp` (an orphan the user imported), first call
   `decompile_preset(model, hsp_b64)` to recover the spec, then patch it.
3. Regenerate by calling `generate_preset(model, spec=<patched spec>)`.
4. Surface any `warnings` from `patch_preset` (e.g. dropped params on a swap)
   to the user.

Prefer one `patch_preset` call with multiple `operations` over several
regenerations. The spec stays the source of truth; the `.hsp` is rebuilt from it.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src pytest tests/test_skills.py -q -k patch_loop`
Expected: PASS

- [ ] **Step 6: Run the full suite one final time**

Run: `PYTHONPATH=$PWD/src pytest -q`
Expected: PASS (entire suite green)

- [ ] **Step 7: Commit**

```bash
git add .claude/skills/tone/SKILL.md tests/test_skills.py
git commit -m "docs(tone): teach the surgical patch loop (patch_preset/decompile_preset)"
```

---

## Self-Review

**Spec coverage:**
- Decompiler (`.hsp → spec`, all features) → Tasks 3, 4; CLI in Task 5; round-trip-stability bar enforced in Tasks 3, 4, 5. ✓
- Sidecar spec convention → Task 8. ✓
- `set-param`, `enable`/`disable`, `add`/`remove-block` → Tasks 2 (enabled field), 6; CLI in Task 9; MCP in Task 10. ✓
- `swap-model` with category check + param carryover + warnings + IR preservation → Task 7; CLI in Task 9; MCP in Task 10. ✓
- Block addressing (display name + `--path/--index`) → Task 6 (`resolve_block`), wired in Task 9. ✓
- Orphan auto-decompile → Task 8 (`load_spec_for_preset`). ✓
- Skill wiring → Task 11. ✓
- Both surfaces (CLI + MCP) → Tasks 9, 10. ✓
- Out of scope (parallel paths, reorder, byte-identity) → respected; no task attempts them. ✓
- Error handling (unknown/ambiguous block, cross-category, dropped-param warning) → Tasks 6, 7, 9 tests. ✓

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N" — each step carries full code. The one `[model_id]` bracket-disambiguation nicety mentioned in the spec is covered functionally by `--path/--index`; bracket syntax is not required for v1 and is not referenced by any task (no dangling promise).

**Type consistency:** `decompile_body(body, library, irs=None)` signature is consistent across Tasks 3, 4, 5, 10. `_block_entry` is introduced 2-arg in Task 3 and explicitly widened to 3-arg (`irs`) in Task 4 with a call-site note. `swap_model` returns `(spec, warnings)` in Task 7 and is consumed as a tuple in Tasks 9, 10. `_to_hsp_bnn`'s new `enabled_base` kwarg (Task 2) matches its call site. `load_spec_for_preset` returns `(dict, Path)` in Task 8 and is unpacked that way in Task 9.
