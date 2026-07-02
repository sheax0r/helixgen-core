"""Decompile: reverse a Stadium .hsp body back into a generate-ready spec dict.

The fidelity bar is *round-trip stability*: composing the returned spec must
reproduce the source preset body (modulo the generated_at provenance stamp).
Only values that differ from the library exemplar are emitted, so specs stay
minimal and readable.

Limitations
-----------
* **Ambiguous display names**: when a block's display_name matches multiple
  library entries the emitted reference is the ``model_id`` instead, so the
  spec regenerates unambiguously.
* **Orphan IR hash**: if an IR slot's ``irhash`` is neither registered in the
  IR mapping *nor* equal to the block's ingest-time ``default_irhash``, the
  raw 32-hex hash is emitted as the ``ir`` field.  Regenerating that spec will
  fail with ``GenerateError`` (wrapping the underlying ``IrMappingError``)
  until the IR is registered via ``helixgen register-irs``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from helixgen import controllers
from helixgen.generate import _coerce_param_value
from helixgen.hsp import ENDPOINT_KEYS as _ENDPOINT_KEYS, _translate_model_id, _unwrap_value, read_hsp
from helixgen.ir import IR_MODEL_PREFIX, IrMapping
from helixgen.library import Library


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


def _iter_blocks(flow: list) -> Any:
    """Yield (path_idx, bnn, slot) for each user block in the flow."""
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


def _recover_footswitches(body: dict, library: Library, device_id: Any) -> list[dict[str, Any]]:
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


def _recover_expression(body: dict, library: Library, device_id: Any) -> list[dict[str, Any]]:
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


def _block_entry(slot: dict, library: Library, irs: IrMapping | None) -> dict[str, Any]:
    """One slot dict → a spec block entry (block name + non-default params).

    The block reference is the display_name when it uniquely resolves back to
    this block; otherwise the model_id is used so the spec regenerates without
    ambiguity.
    """
    model = _translate_model_id(slot.get("model", ""))
    block = library.load_block(model)
    name = block.display_name
    try:
        if library.find_block(name).model_id != block.model_id:
            name = block.model_id
    except (KeyError, LookupError):
        name = block.model_id
    entry: dict[str, Any] = {"block": name}

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

    if model.startswith(IR_MODEL_PREFIX) and slot.get("irhash"):
        irhash = slot["irhash"]
        basename = None
        if irs is not None:
            for h, p in irs.entries.items():
                if h == irhash:
                    basename = os.path.basename(p)
                    break
        if basename is not None:
            entry["ir"] = basename
        elif irhash != getattr(block, "default_irhash", None):
            entry["ir"] = irhash

    return entry


def decompile_body(body: dict, library: Library, irs=None) -> dict[str, Any]:
    if irs is None:
        irs = IrMapping.load()
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
            blocks.append(_block_entry(bnn["slot"][0], library, irs))
        path_entry: dict[str, Any] = {"blocks": blocks}
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


def decompile(hsp_path: Path | str, library: Library, irs=None) -> dict[str, Any]:
    return decompile_body(read_hsp(hsp_path), library, irs=irs)
