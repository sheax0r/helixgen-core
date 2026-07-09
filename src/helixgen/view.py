"""View: project a parsed Stadium .hsp body dict into a readable recipe-shape
dict (name, paths[*].blocks, snapshots, footswitches, expression, etc.) for
agents/humans to comprehend a preset.

This is a direct port of ``decompile.decompile_body`` (and every private
helper it depends on) under the hsp-canonical redesign: ``.hsp`` is the
single source of truth, and ``view()`` is the read-only projection off of it.
Unlike the old ``decompile()`` entry point, ``view()`` never reads a path off
disk and never writes a sidecar file -- it takes an already-parsed body dict
(from ``hsp.read_hsp``) and returns a plain dict. It is lossy by design; see
the fidelity notes below (carried over from decompile.py).

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
  raw 32-hex hash is emitted as the ``ir`` field. Regenerating that spec will
  fail with ``GenerateError`` (wrapping the underlying ``IrMappingError``)
  until the IR is registered via ``helixgen register-irs``.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any

from helixgen import controllers
from helixgen.generate import _coerce_param_value
from helixgen.hsp import ENDPOINT_KEYS as _ENDPOINT_KEYS, _translate_model_id, _unwrap_value
from helixgen.ir import IR_MODEL_PREFIX, IrMapping
from helixgen.library import Library
from helixgen.spec import StructuralEntry


def _device_id(body: dict) -> Any:
    return (body.get("meta") or {}).get("device_id") or "stadium_xl"


def _bnn_keys(path_dict: dict) -> list[str]:
    return sorted(
        k for k in path_dict
        if isinstance(k, str) and k.startswith("b")
        and k not in _ENDPOINT_KEYS and k[1:].isdigit()
    )


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
                if bnn.get("type") in ("split", "join", "input", "output"):
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
            if not isinstance(bnn, dict) or bnn.get("type") in ("split", "join", "input", "output") or not bnn.get("slot"):
                continue
            num = int(key[1:]); lane = 1 if num >= 14 else 0; pos = num - 14 * lane
            try:
                name = _ref_name(library.load_block(_translate_model_id(bnn["slot"][0].get("model", ""))))
            except Exception:
                continue
            idx[name].append((pi, lane, pos))
    return idx


def _ref(name: str, pi: int, lane: int, pos: int, idx: dict) -> dict:
    """Return a block-reference dict, adding coordinates only when the name is ambiguous."""
    ref: dict = {"block": name}
    placements = idx.get(name, [])
    if len(placements) > 1:
        ref["lane"] = lane
        ref["pos"] = pos
        # Include path (even 0) when the name is ambiguous ACROSS paths — lane/pos
        # alone can't disambiguate a same-(lane,pos) collision between path 0 and 1.
        if len({p for (p, _, _) in placements}) > 1:
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


def _warn_unrepresentable_enables(body: dict) -> None:
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
        # @enabled snapshot overrides (False => disable in that snapshot). The
        # bNN base @enabled.value may now be False (base-bypassed block), but
        # disable-recovery keys only off explicit `snapshots[i] is False`, never
        # the base, so base bypass and per-snapshot bypass stay independent.
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
            if pedal not in ("EXP1", "EXP2"):
                print(f"warning: skipping expression target on {block.display_name!r}."
                      f"{pname!r}: controller {pedal or ctrl.get('source')!r} is not an "
                      f"EXP1/EXP2 pedal (footswitch-as-parameter controllers are out of "
                      f"v1 scope).", file=sys.stderr)
                continue
            lo, hi = ctrl.get("min", 0.0), ctrl.get("max", 1.0)

            def _numeric(x: Any) -> bool:
                return isinstance(x, (int, float)) and not isinstance(x, bool)

            if not (_numeric(lo) and _numeric(hi)):
                print(f"warning: skipping expression target on {block.display_name!r}."
                      f"{pname!r}: non-numeric sweep range ({lo!r}..{hi!r}) unsupported in v1.",
                      file=sys.stderr)
                continue
            by_pedal.setdefault(pedal, []).append({
                **_ref(_ref_name(block), pi, lane, pos, idx),
                "param": pname,
                "min": lo, "max": hi})
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
        entry = _block_entry(bnn, library, irs)
    entry["lane"] = lane
    entry["pos"] = pos
    return entry


def _reconstruct_path_blocks(path_dict, library, irs):
    """Ordered spec ``blocks`` list for one .hsp path.

    - Main input b00 → dropped here (drives the `input` field).
    - Endpoints (other than b00) and orphaned split/join → StructuralEntry
      (verbatim); library is never consulted for them.
    - Balanced split/join → semantic Split/Join with branch reconstruction.
    - User blocks → BlockEntry.
    """
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


def _block_entry(bnn: dict, library: Library, irs: IrMapping | None) -> dict[str, Any]:
    """One bNN dict → a spec block entry (block name + non-default params).

    The block reference is the display_name when it uniquely resolves back to
    this block; otherwise the model_id is used so the spec regenerates without
    ambiguity.
    """
    slot = bnn["slot"][0]
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

    # Base bypass lives at the bNN level (the device reads it there); the slot
    # level is inert (~always True). See generate._to_hsp_bnn.
    base_enabled = _unwrap_value(bnn.get("@enabled", True))
    exemplar_enabled = block.exemplar.get("@enabled", True)
    if base_enabled != exemplar_enabled:
        entry["enabled"] = base_enabled

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
        # IR slot with no irhash at all (device slot with no IR loaded).
        # Mark it explicitly so generate doesn't raise "IR block requires
        # an `ir` field" for a preset that never had one.
        entry["no_ir"] = True

    # Verbatim non-modeled bNN state generate would otherwise drop: the harness
    # dict (present on every real block, non-deterministic — Trails/dual/
    # ControlSource) and any extra slots (slot[1:], i.e. a dual cab).
    raw: dict[str, Any] = {}
    harness = bnn.get("harness")
    if isinstance(harness, dict):
        harness_copy = copy.deepcopy(harness)
        # Lift the author-facing Trails (delay/reverb spillover) out of the
        # verbatim harness into a clean top-level `trails` field, so it is a
        # single source of truth that generate re-injects. Gate on category to
        # stay symmetric with generate's delay/reverb-only guard: a block that
        # could not be regenerated with a `trails` field never gets one, and its
        # Trails (if any) stays verbatim inside raw.harness.
        if block.category in ("delay", "reverb"):
            hparams = harness_copy.get("params")
            trails_wrapped = hparams.get("Trails") if isinstance(hparams, dict) else None
            if isinstance(trails_wrapped, dict) and "value" in trails_wrapped:
                entry["trails"] = bool(trails_wrapped["value"])
                del hparams["Trails"]
        raw["harness"] = harness_copy
    slots = bnn.get("slot") or []
    if len(slots) > 1:
        raw["slots"] = copy.deepcopy(slots[1:])
    if raw:
        entry["raw"] = raw

    return entry


def view(body: dict, library: Library, *, irs: IrMapping | None = None) -> dict[str, Any]:
    """Project a parsed Stadium ``.hsp`` body into a readable recipe-shape
    dict: ``name``, ``paths[*].blocks``, and (when present) ``snapshots``,
    ``footswitches``, ``expression``.

    Read-only: ``body`` is an already-parsed dict (from ``hsp.read_hsp``);
    this function never touches the filesystem and never writes a sidecar.
    """
    if irs is None:
        irs = IrMapping.load()
    device_id = _device_id(body)
    flow = (body.get("preset") or {}).get("flow") or []

    paths: list[dict[str, Any]] = []
    for path_dict in flow:
        if not isinstance(path_dict, dict):
            continue
        blocks = [
            {"structural": b.raw, "lane": b.lane, "pos": b.pos} if isinstance(b, StructuralEntry) else b
            for b in _reconstruct_path_blocks(path_dict, library, irs)
        ]
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

    _warn_unrepresentable_enables(body)

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
