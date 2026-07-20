"""helixgen ``.hsp`` → device chain + IR resolution helpers.

Extracts an ordered device-native chain — a list of ``(device_model_id,
{param_name: value}[, irhash])`` in signal order — from a helixgen ``.hsp``
body, resolving helixgen model names/params to the device's numeric vocabulary
(via ``modelmap``/``defs``). The :mod:`helixgen.device.transcode` module consumes
this chain to synthesize a full ``_sbepgsm`` document; there is no template
preset involved (the old template-authoring path was retired once the
transcoder shipped).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import defs


def device_category(model_id: int) -> Optional[str]:
    return defs.category_for(model_id)


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def param_name_map(model_id: int, src_names: List[str]) -> Dict[str, str]:
    """``{helixgen_param_name: device_param_name}`` for a model.

    Device and helixgen mostly share param names (Tone/Level/Bass/Mix/…) but a
    few differ (helixgen "Drive" vs device "Gain"). Strategy: exact/normalized
    name match first, then assign any leftover helixgen names to the remaining
    device params by position (both are in canonical param order). A helixgen
    name that maps nowhere is omitted. Shared by base-param and snapshot-array
    mapping so the two never disagree on a block's device names.
    """
    dev = defs.model_params_for(model_id)
    dev_names = list(dev.keys())
    by_norm = {_norm(n): n for n in dev_names}
    mapping: Dict[str, str] = {}
    leftover: List[str] = []
    used = set()
    for hn in src_names:
        dn = by_norm.get(_norm(hn))
        if dn is not None and dn not in used:
            mapping[hn] = dn
            used.add(dn)
        else:
            leftover.append(hn)
    remaining = [n for n in dev_names if n not in used]
    for hn, dn in zip(leftover, remaining):
        mapping[hn] = dn
    return mapping


def map_params(model_id: int, src_params: Dict[str, Any]) -> Dict[str, Any]:
    """Map helixgen param names -> device param names for a model. Returns
    ``{device_param_name: value}`` (see :func:`param_name_map`)."""
    mapping = param_name_map(model_id, list(src_params.keys()))
    return {mapping[hn]: v for hn, v in src_params.items() if hn in mapping}


# --- helixgen .hsp -> device chain ------------------------------------------

def _default_resolve_model(helixgen_model_id: str) -> Optional[int]:
    """Resolve a helixgen model-id string to a device numeric id.

    Prefers the reconciled modelmap (helixgen<->device vocabulary); falls back to
    a direct defs lookup for the models whose names already match.
    """
    try:
        from . import modelmap
        dev = modelmap.device_model_id(helixgen_model_id)
        if dev is not None:
            return dev
    except Exception:
        pass
    return defs.model_id_for(helixgen_model_id)


class UnresolvedModel(Exception):
    def __init__(self, model_id: str):
        super().__init__(f"could not resolve helixgen model {model_id!r} to a device model")
        self.model_id = model_id


def hsp_to_chain(hsp_body: dict, *, dsp: int = 0,
                 resolve_model=_default_resolve_model,
                 strict: bool = True) -> List[Tuple[int, Dict[str, Any]]]:
    """Extract an ordered device chain from a helixgen ``.hsp`` body.

    Input/output endpoints are skipped; each user block's helixgen model is
    resolved to a device id and its params flattened to ``{name: value}``.
    ``strict`` raises :class:`UnresolvedModel` on a model that can't be resolved;
    otherwise the block is skipped.
    """
    flow = hsp_body["preset"]["flow"][dsp]
    chain: List[Tuple[int, Dict[str, Any]]] = []
    for key in sorted(k for k in flow if isinstance(k, str) and k.startswith("b")):
        b = flow[key]
        if not isinstance(b, dict):
            continue
        slot = b.get("slot")
        if not (isinstance(slot, list) and slot and isinstance(slot[0], dict)):
            continue
        model = slot[0].get("model")
        if not model:
            continue
        dev_id = resolve_model(model)
        if dev_id is None:
            if strict:
                raise UnresolvedModel(model)
            continue
        cat = device_category(dev_id)
        if cat in (None, "input", "output"):
            continue
        raw = {}
        for name, wrapped in (slot[0].get("params") or {}).items():
            val = wrapped.get("value") if isinstance(wrapped, dict) else wrapped
            if isinstance(val, (int, float)):
                raw[name] = val
        params = map_params(dev_id, raw)   # helixgen param names -> device names
        chain.append((dev_id, params))
    return chain


def hsp_to_chain_with_irs(
    hsp_body: dict, *, dsp: int = 0,
    resolve_model=_default_resolve_model,
    strict: bool = True,
) -> List[Tuple[int, Dict[str, Any], Optional[str]]]:
    """Like :func:`hsp_to_chain`, but also carry each user block's IR hash.

    Returns ``(device_model_id, {param_name: value}, irhash_or_None)`` in signal
    order. ``irhash`` is the block's ``.hsp`` slot ``irhash`` string (32-hex) when
    present (IR cab blocks), else ``None``. Additive sibling of
    :func:`hsp_to_chain` — the latter's signature/behaviour is unchanged.
    """
    flow = hsp_body["preset"]["flow"][dsp]
    chain: List[Tuple[int, Dict[str, Any], Optional[str]]] = []
    for key in sorted(k for k in flow if isinstance(k, str) and k.startswith("b")):
        b = flow[key]
        if not isinstance(b, dict):
            continue
        slot = b.get("slot")
        if not (isinstance(slot, list) and slot and isinstance(slot[0], dict)):
            continue
        model = slot[0].get("model")
        if not model:
            continue
        dev_id = resolve_model(model)
        if dev_id is None:
            if strict:
                raise UnresolvedModel(model)
            continue
        cat = device_category(dev_id)
        if cat in (None, "input", "output"):
            continue
        raw = {}
        for name, wrapped in (slot[0].get("params") or {}).items():
            val = wrapped.get("value") if isinstance(wrapped, dict) else wrapped
            if isinstance(val, (int, float)):
                raw[name] = val
        params = map_params(dev_id, raw)
        irhash = slot[0].get("irhash") or None
        chain.append((dev_id, params, irhash))
    return chain


def _lane_pos(key: str) -> Tuple[int, int]:
    """``.hsp`` ``bNN`` key -> ``(lane, pos)`` (lane-1 blocks live at 14+pos)."""
    num = int(key[1:])
    lane = 1 if num >= 14 else 0
    return lane, num - 14 * lane


def _snapshot_arrays(slot: dict, bnn: dict,
                     dev_id: int) -> Tuple[Optional[List[Any]], Dict[str, List[Any]]]:
    """Extract a block's per-snapshot bypass + param arrays, mapped to device
    param names. Returns ``(bypass_list_or_None, {device_param: [values]})``.

    The FULL dense arrays are read (all 8 snapshots — the trailing "Snap N"
    slots carry real state, not placeholders). ``None`` entries (legacy sparse
    exports) fall back to the base value. The bypass list is DEVICE polarity
    (``True`` = bypassed), i.e. the inverse of the ``.hsp`` ``@enabled``
    arrays — a device bypass target's snapshot value is "is it bypassed"
    (hardware-verified against the Stadium app's own import, 2026-07-13).
    A param whose per-snapshot array is entirely ``None`` (no override) is
    skipped.
    """
    bypass: Optional[List[Any]] = None
    en = bnn.get("@enabled")
    if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
        arr = en["snapshots"]
        if any(v is not None for v in arr):
            bv = en.get("value")
            base = True if bv is None else bool(bv)  # missing/None = enabled
            bypass = [not bool(base if v is None else v) for v in arr]

    src_params = slot.get("params") or {}
    name_map = param_name_map(dev_id, list(src_params.keys()))
    params: Dict[str, List[Any]] = {}
    for pname, wrapped in src_params.items():
        if not (isinstance(wrapped, dict) and isinstance(wrapped.get("snapshots"), list)):
            continue
        arr = wrapped["snapshots"]
        if not any(v is not None for v in arr):
            continue
        dev_name = name_map.get(pname)
        if dev_name is None:
            continue
        base = wrapped.get("value")
        if base is None and any(v is None for v in arr):
            # No base to fill the sparse slots with — a None must never reach
            # the device's tamv (msgpack nil where it expects a value).
            continue
        params[dev_name] = [base if v is None else v for v in arr]
    return bypass, params


def _ctl_meta(c: dict, *, behavior_default: str) -> Dict[str, Any]:
    """Common controller metadata lifted off a ``.hsp`` controller dict:
    source id, behavior, and (when non-default) curve / threshold."""
    meta: Dict[str, Any] = {"source": c["source"],
                            "behavior": c.get("behavior", behavior_default)}
    curve = c.get("curve")
    if isinstance(curve, str) and curve != "linear":
        meta["curve"] = curve
    thr = c.get("threshold")
    if isinstance(thr, (int, float)) and not isinstance(thr, bool) and thr != 0.0:
        meta["threshold"] = thr
    return meta


def hsp_to_paths(hsp_body: dict, *, resolve_model=_default_resolve_model,
                 strict: bool = True) -> List[Dict[str, Any]]:
    """Read EVERY DSP flow of a ``.hsp`` body into per-path recipe entries
    (dual-amp spec §3.1) — the multi-path successor to :func:`hsp_to_chain`.

    Each returned path dict is ``{"blocks": [...], "input": <mode|None>,
    "structural": [...]}`` where every block carries its device model-name
    string, mapped params, ``irhash``, ``(lane, pos)`` coordinate, and (when it
    varies) ``snap_bypass`` / ``snap_params`` arrays. ``structural`` holds each
    split/join as ``{"kind", "model", "params"}`` for the transcoder to
    materialize. A b13 output endpoint whose ``gain``/``pan`` wrappers carry
    per-snapshot arrays (#62 phase 2 trims) contributes
    ``output_snap_params`` (``{device_param: [8 values]}``) alongside
    ``output_params``. Signal order within a lane is preserved; endpoints (b00
    input / outputs / looper / None) are skipped as user blocks (the b00 input
    mode drives ``input``).
    """
    preset = hsp_body.get("preset") or {}
    flows = preset.get("flow") or []
    device_id = (hsp_body.get("meta") or {}).get("device_id") or "stadium_xl"
    has_snaps = bool(preset.get("snapshots"))
    midi_by_coord = _hsp_midi_by_coord(hsp_body)

    from .. import controllers as _controllers

    out: List[Dict[str, Any]] = []
    for pi, flow in enumerate(flows):
        if not isinstance(flow, dict):
            continue
        blocks: List[Dict[str, Any]] = []
        structural: List[Dict[str, Any]] = []
        input_mode: Optional[str] = None
        input_params: Dict[str, Any] = {}
        input_enabled: Optional[bool] = None
        input_snap_bypass: Optional[List[Any]] = None
        output_params: Dict[str, Any] = {}
        output_snap_params: Dict[str, List[Any]] = {}
        for key in sorted(k for k in flow if isinstance(k, str)
                          and k.startswith("b") and k[1:].isdigit()):
            b = flow[key]
            if not isinstance(b, dict):
                continue
            slot_list = b.get("slot")
            if not (isinstance(slot_list, list) and slot_list
                    and isinstance(slot_list[0], dict)):
                continue
            slot = slot_list[0]
            model = slot.get("model")
            if not model:
                continue
            typ = b.get("type")
            if typ in ("split", "join"):
                sp = {n: (w.get("value") if isinstance(w, dict) else w)
                      for n, w in (slot.get("params") or {}).items()}
                slane, spos = _lane_pos(key)
                structural.append({"kind": typ, "model": model, "params": sp,
                                   "lane": slane, "pos": spos})
                continue
            if key == "b00":
                input_mode = _controllers.input_mode_for_model(device_id, model)
                input_params = _lift_endpoint_params(slot)
                # #23: the input endpoint carries its own bypass state — a base
                # ``@enabled.value`` of False (loads bypassed) and a per-snapshot
                # bypass array (the Stadium app snapshot-tracks the DSP input as
                # a bypass target). Capture both so the transcoder can reproduce
                # them; without this an input muted per-snapshot / bypassed at
                # load silently reverts on ``device install``/``sync``.
                en0 = b.get("@enabled")
                base_en0 = en0.get("value") if isinstance(en0, dict) else en0
                if base_en0 is not None and not base_en0:
                    input_enabled = False
                if has_snaps:
                    ibypass, _ = _snapshot_arrays(slot, b, 0)
                    if ibypass is not None:
                        input_snap_bypass = ibypass
                continue
            if typ == "output":
                # The lane-0 primary output (b13) carries the path's level/pan;
                # the device param names (gain/pan) match the .hsp names.
                if key == "b13":
                    output_params = _lift_endpoint_params(slot)
                    # #62 phase 2: per-snapshot output-gain trims ride the b13
                    # param wrappers' `snapshots` arrays (dense, base-filled —
                    # written by `mutate.set_flow_param(..., snapshot=)`).
                    # Lifted like `_snapshot_arrays`' param half, but without
                    # the user-block name map (the .hsp names ARE the device
                    # names here); the transcoder emits the matching snapshot
                    # param target keyed by the OutputMatrix instance id.
                    if has_snaps:
                        osnap: Dict[str, List[Any]] = {}
                        for pname, wrapped in (slot.get("params") or {}).items():
                            if not (isinstance(wrapped, dict)
                                    and isinstance(wrapped.get("snapshots"), list)):
                                continue
                            arr = wrapped["snapshots"]
                            if not any(v is not None for v in arr):
                                continue
                            obase = wrapped.get("value")
                            if obase is None and any(v is None for v in arr):
                                continue  # nothing to densify sparse slots with
                            osnap[pname] = [obase if v is None else v
                                            for v in arr]
                        if osnap:
                            output_snap_params = osnap
                continue
            dev_id = resolve_model(model)
            if dev_id is None:
                if strict:
                    raise UnresolvedModel(model)
                continue
            cat = device_category(dev_id)
            if cat in (None, "input", "output"):
                continue
            raw = {}
            for name, wrapped in (slot.get("params") or {}).items():
                val = wrapped.get("value") if isinstance(wrapped, dict) else wrapped
                if isinstance(val, (int, float)):
                    raw[name] = val
            lane, pos = _lane_pos(key)
            name_map = param_name_map(dev_id, list((slot.get("params") or {}).keys()))
            spec: Dict[str, Any] = {
                "block": defs.model_name_for(dev_id),
                "params": {name_map[n]: v for n, v in raw.items() if n in name_map},
                "lane": lane, "pos": pos,
            }
            irhash = slot.get("irhash") or None
            if irhash:
                spec["irhash"] = irhash
            if has_snaps:
                bypass, snap_params = _snapshot_arrays(slot, b, dev_id)
                if bypass is not None:
                    spec["snap_bypass"] = bypass
                if snap_params:
                    spec["snap_params"] = snap_params
            # Controller assignments (spec 2 Part B): source->bypass +
            # source->param (EXP sweeps AND footswitch param toggles — both
            # are `param`-type controllers; behavior tells them apart).
            en = b.get("@enabled")
            # Base bypass: a block whose ``@enabled.value`` is falsy (False or
            # a degenerate 0) loads bypassed — carried so the transcoder can
            # emit ``enbl=0``. Missing/None means enabled, matching
            # ``_snapshot_arrays``'s base-polarity default.
            base_en = en.get("value") if isinstance(en, dict) else en
            if base_en is not None and not base_en:
                spec["enabled"] = False
            if isinstance(en, dict) and isinstance(en.get("controller"), dict):
                c = en["controller"]
                if c.get("type") == "targetbypass" and c.get("source") is not None:
                    spec["fs_bypass"] = _ctl_meta(c, behavior_default="latching")
            ctl: Dict[str, Any] = {}
            for pname, wrapped in (slot.get("params") or {}).items():
                if not (isinstance(wrapped, dict)
                        and isinstance(wrapped.get("controller"), dict)):
                    continue
                cc = wrapped["controller"]
                if cc.get("type") != "param" or cc.get("source") is None:
                    continue
                dev_name = name_map.get(pname)
                if dev_name is None:
                    continue
                meta = _ctl_meta(cc, behavior_default="continuous")
                meta["min"] = cc.get("min", 0.0)
                meta["max"] = cc.get("max", 1.0)
                ctl[dev_name] = meta
            if ctl:
                spec["ctl_params"] = ctl
            # MIDI CC controller bindings (#33): lifted from the helixgen-
            # namespaced ``preset._helixgen_midi`` list (NOT a device-native
            # ``.hsp`` controller — see mutate.wire_midi). Map each library
            # param name to its device name just like ``ctl_params``.
            mrec = midi_by_coord.get((pi, lane, pos))
            if mrec:
                if mrec.get("bypass_cc") is not None:
                    spec["midi_bypass"] = {"cc": mrec["bypass_cc"]}
                mparams: Dict[str, Any] = {}
                for lib_name, cfg in (mrec.get("params") or {}).items():
                    dev_name = name_map.get(lib_name)
                    if dev_name is None:
                        continue
                    mparams[dev_name] = {"cc": cfg["cc"], "min": cfg["min"],
                                         "max": cfg["max"]}
                if mparams:
                    spec["midi_params"] = mparams
            blocks.append(spec)
        path_entry: Dict[str, Any] = {"blocks": blocks}
        if input_mode is not None:
            path_entry["input"] = input_mode
        if input_params:
            path_entry["input_params"] = input_params
        if input_enabled is False:
            path_entry["input_enabled"] = False
        if input_snap_bypass is not None:
            path_entry["input_snap_bypass"] = input_snap_bypass
        if output_params:
            path_entry["output_params"] = output_params
        if output_snap_params:
            path_entry["output_snap_params"] = output_snap_params
        if structural:
            path_entry["structural"] = structural
        out.append(path_entry)
    return out


def _lift_endpoint_params(slot: dict) -> Dict[str, Any]:
    """Lift an input/output endpoint slot's params as device-name-keyed scalar
    values. Stereo per-channel wrappers (``{"1": {...}, "2": {...}}``) become
    the device's ``<name>.1`` / ``<name>.2`` param names (matching ``defs``
    for e.g. ``P35_InputInst1_2``)."""
    out: Dict[str, Any] = {}
    for name, wrapped in (slot.get("params") or {}).items():
        if not isinstance(wrapped, dict):
            continue
        if "1" in wrapped and isinstance(wrapped.get("1"), dict):
            for ch in ("1", "2"):
                w = wrapped.get(ch)
                if isinstance(w, dict) and isinstance(
                        w.get("value"), (int, float, bool)):
                    out[f"{name}.{ch}"] = w["value"]
        elif isinstance(wrapped.get("value"), (int, float, bool)):
            out[name] = wrapped["value"]
    return out


def hsp_snapshot_meta(hsp_body: dict) -> List[Dict[str, Any]]:
    """Snapshot metadata (``name``/``exsw``/``bpm``) from a ``.hsp`` body, in
    snapshot order, for the transcoder's ``cg__`` synthesis. ALL slots are
    returned — the trailing "Snap N" defaults are real snapshot names the
    device shows, not placeholders to strip."""
    preset = hsp_body.get("preset") or {}
    raw = preset.get("snapshots") or []
    meta: List[Dict[str, Any]] = []
    for s in raw:
        meta.append({"name": s.get("name"), "exsw": s.get("expsw", -1),
                     "bpm": s.get("tempo", 120.0)})
    return meta


def hsp_sources(hsp_body: dict) -> Dict[int, Dict[str, Any]]:
    """The ``preset.sources`` scribble-strip config keyed by integer source id.

    Each value carries ``fs_color``/``fs_label``/``fs_topidx`` (and ``bypass``)
    for the footswitch that source drives — synthesized into
    ``pm__.floorboard.stomp.*`` (spec 2 Part B)."""
    raw = (hsp_body.get("preset") or {}).get("sources") or {}
    out: Dict[int, Dict[str, Any]] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            continue
    return out


def hsp_commands(hsp_body: dict) -> List[Dict[str, Any]]:
    """Command Center commands from ``preset.commands`` (backlog #16), flattened
    to a list of ``{source, type, func, channel, params}`` records for the
    transcoder's ``cg__.entt`` cmnd synthesis. ``source`` is the integer switch
    source id; ``type`` the family (``PresetSnapshot``/``MIDI``); ``func`` the
    native ``Command`` subtype; ``params`` the raw ``{name: value}`` map.
    Malformed entries are skipped. Ordinal order per source is preserved."""
    raw = (hsp_body.get("preset") or {}).get("commands")
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, dict):
        return out
    for key, records in raw.items():
        try:
            source = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(records, list):
            continue
        for rec in sorted(
                (r for r in records if isinstance(r, dict)),
                key=lambda r: r.get("ordinal", 0)):
            params_raw = rec.get("params") if isinstance(rec.get("params"), dict) else {}
            params = {name: (w.get("value") if isinstance(w, dict) else w)
                      for name, w in params_raw.items()}
            out.append({
                "source": source,
                "type": rec.get("type"),
                "func": params.get("Command", 0),
                "behavior": rec.get("behavior", "latching"),
                "toggle": bool(rec.get("toggle", False)),
                "params": params,
            })
    return out


def _hsp_midi_by_coord(hsp_body: dict) -> Dict[Tuple[int, int, int], Dict[str, Any]]:
    """Group ``preset._helixgen_midi`` records by ``(path, lane, pos)`` block
    coordinate (backlog #33). Each value is ``{"bypass_cc": int|None,
    "params": {lib_param: {cc, min, max}}}`` — the transcoder maps the library
    param names to device names and synthesizes the ``cg__`` MIDI ctrl records.
    CC-only; malformed records are skipped."""
    recs = (hsp_body.get("preset") or {}).get("_helixgen_midi")
    out: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    if not isinstance(recs, list):
        return out
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        cc = rec.get("cc")
        if not isinstance(cc, int) or isinstance(cc, bool) or not (0 <= cc <= 127):
            continue
        pi = rec.get("path", 0)
        lane = rec.get("lane", 0)
        pos = rec.get("pos")
        if not all(isinstance(v, int) and not isinstance(v, bool)
                   for v in (pi, lane, pos)):
            continue
        entry = out.setdefault((pi, lane, pos), {"bypass_cc": None, "params": {}})
        param = rec.get("param")
        if param is None:
            entry["bypass_cc"] = cc
        else:
            entry["params"][param] = {"cc": cc, "min": rec.get("min", 0.0),
                                      "max": rec.get("max", 1.0)}
    return out


def hsp_ir_hashes(hsp_body: dict) -> set:
    """Every IR hash (``irhash``) referenced by a helixgen ``.hsp`` body."""
    hashes = set()
    for flow in hsp_body.get("preset", {}).get("flow", []):
        if not isinstance(flow, dict):
            continue
        for key, b in flow.items():
            if not (isinstance(key, str) and key.startswith("b") and isinstance(b, dict)):
                continue
            for slot in (b.get("slot") or []):
                if isinstance(slot, dict) and slot.get("irhash"):
                    hashes.add(slot["irhash"])
    return hashes


def check_irs(client, hsp_body: dict) -> Dict[str, set]:
    """Compare a preset's referenced IRs against what's on the device.

    Returns ``{"present": {...hashes...}, "missing": {...hashes...}}``. Missing
    IRs must be imported onto the device (helixgen ``register-irs``/``ir-scan``
    + the editor's IR import) before the preset's cab will sound right.

    Every hash that looks missing is cross-checked against the device's point
    lookup (``verify=``) — the ``-11`` container listing lags a just-completed
    IR upload, and reporting an IR the device already has as "missing" sends
    the user off to re-import it (#38 Task 4).
    """
    want = hsp_ir_hashes(hsp_body)
    have = client.device_ir_hashes(verify=sorted(want))
    return {"present": want & have, "missing": want - have}
