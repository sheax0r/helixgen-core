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
    """Yield (path_idx, key, bnn, slot) for each user block in the flow.

    Split and join structural blocks are skipped — they are not in the library
    and carry no footswitch / expression / snapshot metadata.
    """
    for path_idx, path_dict in enumerate(flow):
        if not isinstance(path_dict, dict):
            continue
        for key in _bnn_keys(path_dict):
            bnn = path_dict.get(key)
            if isinstance(bnn, dict) and bnn.get("slot"):
                if bnn.get("type") in ("split", "join"):
                    continue
                yield path_idx, key, bnn, bnn["slot"][0]


def _ref_name(block) -> str:
    """Display name when non-empty, else model_id — never empty."""
    return block.display_name or block.model_id


def _name_index(flow: list, library: Library) -> dict:
    """Build a display-name → list-of-(path_idx, lane, pos) index over all placed blocks."""
    from collections import defaultdict
    idx: dict = defaultdict(list)
    for pi, path in enumerate(flow):
        if not isinstance(path, dict):
            continue
        for key in path:
            if not (isinstance(key, str) and key.startswith("b") and key[1:].isdigit()
                    and key not in _ENDPOINT_KEYS):
                continue
            bnn = path[key]
            if not isinstance(bnn, dict) or bnn.get("type") in ("split", "join") or not bnn.get("slot"):
                continue
            num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
            try:
                name = _ref_name(library.load_block(_translate_model_id(bnn["slot"][0].get("model", ""))))
            except Exception:
                continue
            idx[name].append((pi, lane, pos))
    return idx


def _ref(name: str, pi: int, lane: int, pos: int, idx: dict) -> dict:
    """Return a block-reference dict, adding lane/pos only when the name is ambiguous."""
    ref: dict = {"block": name}
    if len(idx.get(name, [])) > 1:
        ref["lane"] = lane
        ref["pos"] = pos
        if pi:
            ref["path"] = pi
    return ref


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


def _recover_snapshots(body: dict, library: Library, idx: dict) -> list[dict[str, Any]]:
    """Recover the spec-level `snapshots` array from a decompiled body.

    `idx` is the display-name -> [(path_idx, lane, pos), ...] index (from
    `_name_index`); it's used to decide whether a snapshot ref needs
    coordinates (ambiguous display_name) or can stay a bare string/dict
    (unambiguous).

    Task 1 densified the per-param `snapshots` arrays on generate: every slot
    now carries an explicit value (base value fills previously-null slots)
    instead of `null`. That means a naive "is this slot non-None" check would
    record a spurious override for every non-diverging snapshot. We filter
    those out by comparing each slot's value to the param's own base value
    (`_unwrap_value(wrapped)`) and only keeping genuine differences.
    """
    names = _snapshot_names(body)
    if not names:
        return []
    # Per-snapshot accumulators keyed by (path_idx, lane, pos, display_name).
    disables: list[list[tuple]] = [[] for _ in names]
    params: list[dict[tuple, dict]] = [{} for _ in names]
    flow = (body.get("preset") or {}).get("flow") or []
    for pi, key, bnn, slot in _iter_blocks(flow):
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        name = _ref_name(block)
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        coord = (pi, lane, pos, name)
        # @enabled snapshot overrides (False => disable in that snapshot).
        # The base bNN-level @enabled is always True (generate never densifies
        # it to anything else), so `ov is False` already isolates genuine
        # disables -- no phantom-filter needed here.
        en = bnn.get("@enabled")
        if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
            for i, ov in enumerate(en["snapshots"]):
                if i < len(names) and ov is False:
                    disables[i].append(coord)
        # param snapshot overrides -- filter dense fills equal to base.
        for pname, wrapped in (slot.get("params") or {}).items():
            if not (isinstance(wrapped, dict) and isinstance(wrapped.get("snapshots"), list)):
                continue
            base = _coerce_param_value(block, pname, _unwrap_value(wrapped))
            for i, ov in enumerate(wrapped["snapshots"]):
                if i >= len(names) or ov is None:
                    continue
                coerced = _coerce_param_value(block, pname, ov)
                if coerced == base:
                    continue  # phantom: densify-filled base value, not a real override
                params[i].setdefault(coord, {})[pname] = coerced

    snaps: list[dict[str, Any]] = []
    for i, nm in enumerate(names):
        s: dict[str, Any] = {"name": nm}
        # disable: bare string if unambiguous, else coordinate dict
        dis = []
        for (dpi, dlane, dpos, dname) in disables[i]:
            r = _ref(dname, dpi, dlane, dpos, idx)
            dis.append(dname if len(r) == 1 else r)
        if dis:
            s["disable"] = dis
        # params: name-keyed dict if every param-block is unambiguous, else
        # the list-of-{**ref, "params": {...}} form.
        if params[i]:
            ambiguous = any(len(idx.get(pname, [])) > 1 for (_, _, _, pname) in params[i])
            if ambiguous:
                s["params"] = [
                    {**_ref(pname, ppi, plane, ppos, idx), "params": pv}
                    for (ppi, plane, ppos, pname), pv in params[i].items()
                ]
            else:
                s["params"] = {pname: pv for (_, _, _, pname), pv in params[i].items()}
        snaps.append(s)
    return snaps


def _recover_footswitches(body: dict, library: Library, device_id: Any, idx: dict) -> list[dict[str, Any]]:
    flow = (body.get("preset") or {}).get("flow") or []
    out: list[dict[str, Any]] = []
    for pi, key, bnn, slot in _iter_blocks(flow):
        en = bnn.get("@enabled")
        ctrl = en.get("controller") if isinstance(en, dict) else None
        if not (isinstance(ctrl, dict) and ctrl.get("type") == "targetbypass"):
            continue
        name = controllers.controller_name_for_source(device_id, ctrl.get("source"))
        if name is None:
            continue
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        out.append({"switch": name, **_ref(_ref_name(block), pi, lane, pos, idx),
                    "behavior": ctrl.get("behavior", "latching")})
    return out


def _recover_expression(body: dict, library: Library, device_id: Any, idx: dict) -> list[dict[str, Any]]:
    flow = (body.get("preset") or {}).get("flow") or []
    by_pedal: dict[str, list[dict[str, Any]]] = {}
    for pi, key, _bnn, slot in _iter_blocks(flow):
        block = library.load_block(_translate_model_id(slot.get("model", "")))
        num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
        for pname, wrapped in (slot.get("params") or {}).items():
            ctrl = wrapped.get("controller") if isinstance(wrapped, dict) else None
            if not (isinstance(ctrl, dict) and ctrl.get("type") == "param"):
                continue
            pedal = controllers.controller_name_for_source(device_id, ctrl.get("source"))
            if pedal is None:
                continue
            by_pedal.setdefault(pedal, []).append({
                **_ref(_ref_name(block), pi, lane, pos, idx),
                "param": pname,
                "min": ctrl.get("min", 0.0), "max": ctrl.get("max", 1.0)})
    return [{"pedal": p, "targets": t} for p, t in by_pedal.items()]


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
    """Ordered spec ``blocks`` list for one .hsp path: lane-0 blocks in position
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
        # Always emit ir — registered basename if available, else raw 32-hex hash.
        # Omitting ir when irhash == block.default_irhash caused regeneration to
        # silently use the library default instead of the preset's actual hash,
        # breaking the round-trip for presets whose default_irhash is None.
        entry["ir"] = basename if basename is not None else irhash
    elif model.startswith(IR_MODEL_PREFIX):
        # IR slot with no irhash at all (device slot with no IR loaded).
        # Mark it explicitly so generate doesn't raise "IR block requires
        # an `ir` field" for a preset that never had one.
        entry["no_ir"] = True

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
        blocks = _reconstruct_path_blocks(path_dict, library, irs)
        path_entry: dict[str, Any] = {"blocks": blocks}
        mode = _input_mode(path_dict, device_id)
        if mode is not None:
            path_entry["input"] = mode
        paths.append(path_entry)

    meta = body.get("meta") or {}
    spec: dict[str, Any] = {"name": meta.get("name") or "Untitled", "paths": paths}
    if meta.get("author"):
        spec["author"] = meta["author"]

    idx = _name_index(flow, library)

    snaps = _recover_snapshots(body, library, idx)
    if snaps:
        spec["snapshots"] = snaps

    fs = _recover_footswitches(body, library, device_id, idx)
    if fs:
        spec["footswitches"] = fs

    exp = _recover_expression(body, library, device_id, idx)
    if exp:
        spec["expression"] = exp

    return spec


def decompile(hsp_path: Path | str, library: Library, irs=None) -> dict[str, Any]:
    return decompile_body(read_hsp(hsp_path), library, irs=irs)
