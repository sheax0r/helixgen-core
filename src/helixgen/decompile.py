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
