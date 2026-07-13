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
    name = defs.model_name_for(model_id)
    if name is None:
        return None
    return defs.load_defs().get("model_categories", {}).get(name)


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
    dev = defs.load_defs().get("model_params", {}).get(str(model_id), {})
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


def _snapshot_arrays(slot: dict, bnn: dict, dev_id: int,
                     n_snaps: int) -> Tuple[Optional[List[Any]], Dict[str, List[Any]]]:
    """Extract a block's per-snapshot bypass + param arrays, mapped to device
    param names. Returns ``(bypass_list_or_None, {device_param: [values]})``.

    Only the first ``n_snaps`` slots (the named snapshots) are considered. A
    param whose per-snapshot array is entirely ``None`` (no override) is skipped.
    """
    bypass: Optional[List[Any]] = None
    en = bnn.get("@enabled")
    if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
        arr = en["snapshots"][:n_snaps]
        if any(v is not None for v in arr):
            bypass = [bool(v) for v in arr]

    src_params = slot.get("params") or {}
    name_map = param_name_map(dev_id, list(src_params.keys()))
    params: Dict[str, List[Any]] = {}
    for pname, wrapped in src_params.items():
        if not (isinstance(wrapped, dict) and isinstance(wrapped.get("snapshots"), list)):
            continue
        arr = wrapped["snapshots"][:n_snaps]
        if not any(v is not None for v in arr):
            continue
        dev_name = name_map.get(pname)
        if dev_name is None:
            continue
        base = wrapped.get("value")
        params[dev_name] = [base if v is None else v for v in arr]
    return bypass, params


def hsp_to_paths(hsp_body: dict, *, resolve_model=_default_resolve_model,
                 strict: bool = True) -> List[Dict[str, Any]]:
    """Read EVERY DSP flow of a ``.hsp`` body into per-path recipe entries
    (dual-amp spec §3.1) — the multi-path successor to :func:`hsp_to_chain`.

    Each returned path dict is ``{"blocks": [...], "input": <mode|None>,
    "structural": [...]}`` where every block carries its device model-name
    string, mapped params, ``irhash``, ``(lane, pos)`` coordinate, and (when it
    varies) ``snap_bypass`` / ``snap_params`` arrays. ``structural`` holds each
    split/join as ``{"kind", "model", "params"}`` for the transcoder to
    materialize. Signal order within a lane is preserved; endpoints (b00
    input / outputs / looper / None) are skipped as user blocks (the b00 input
    mode drives ``input``).
    """
    preset = hsp_body.get("preset") or {}
    flows = preset.get("flow") or []
    device_id = (hsp_body.get("meta") or {}).get("device_id") or "stadium_xl"
    # Number of NAMED snapshots (trailing "Snap N" placeholders don't count).
    raw_snaps = preset.get("snapshots") or []
    n_named = 0
    for i, s in enumerate(raw_snaps):
        if s.get("name") != f"Snap {i + 1}":
            n_named = i + 1
    n_snaps = n_named or len(raw_snaps)

    from .. import controllers as _controllers

    out: List[Dict[str, Any]] = []
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        blocks: List[Dict[str, Any]] = []
        structural: List[Dict[str, Any]] = []
        input_mode: Optional[str] = None
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
                structural.append({"kind": typ, "model": model, "params": sp})
                continue
            if key == "b00":
                input_mode = _controllers.input_mode_for_model(device_id, model)
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
            if n_snaps:
                bypass, snap_params = _snapshot_arrays(slot, b, dev_id, n_snaps)
                if bypass is not None:
                    spec["snap_bypass"] = bypass
                if snap_params:
                    spec["snap_params"] = snap_params
            # Controller assignments (spec 2 Part B): FS->bypass + EXP->param.
            en = b.get("@enabled")
            if isinstance(en, dict) and isinstance(en.get("controller"), dict):
                c = en["controller"]
                if c.get("type") == "targetbypass" and c.get("source") is not None:
                    spec["fs_bypass"] = {"source": c["source"],
                                         "behavior": c.get("behavior", "latching")}
            exp: Dict[str, Any] = {}
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
                exp[dev_name] = {"source": cc["source"],
                                 "min": cc.get("min", 0.0), "max": cc.get("max", 1.0)}
            if exp:
                spec["exp_params"] = exp
            blocks.append(spec)
        path_entry: Dict[str, Any] = {"blocks": blocks}
        if input_mode is not None:
            path_entry["input"] = input_mode
        if structural:
            path_entry["structural"] = structural
        out.append(path_entry)
    return out


def hsp_snapshot_meta(hsp_body: dict) -> List[Dict[str, Any]]:
    """Named-snapshot metadata (``name``/``exsw``/``bpm``) from a ``.hsp`` body,
    in snapshot order, for the transcoder's ``cg__`` synthesis."""
    preset = hsp_body.get("preset") or {}
    raw = preset.get("snapshots") or []
    n_named = 0
    for i, s in enumerate(raw):
        if s.get("name") != f"Snap {i + 1}":
            n_named = i + 1
    meta: List[Dict[str, Any]] = []
    for s in raw[:n_named]:
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
    """
    want = hsp_ir_hashes(hsp_body)
    have = client.device_ir_hashes()
    return {"present": want & have, "missing": want - have}
