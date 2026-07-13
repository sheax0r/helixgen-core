"""``.hsp`` <-> device ``_sbepgsm`` transcoder (Phase 1: lossless projection).

The device stores presets as ``_sbepgsm`` msgpack (numeric model/param ids, a
flat per-slot block grid, top-level ``cg__``/``pm__``/``sfg_``). helixgen speaks
a recipe of model-id strings + human param names. This module projects a decoded
``_sbepgsm`` dict into a helixgen-style **recipe** and rebuilds the exact dict
back, so that ``recipe_to_sbepgsm(sbepgsm_to_recipe(D)) == D`` for real device
presets (the offline fidelity gate in ``tests/test_transcode.py``).

Design — model the parts we can cleanly, carry everything else verbatim:

* Each DSP flow becomes ``paths[i]["blocks"]``: an ordered list of the flow's
  **user** blocks (endpoints — input/output/looper/split/join — are skipped),
  each ``{"block": <device model-id string>, "params": {<name>: <value>}}``.
  The model id and the named param values are LIFTED OUT of raw (the model id
  string via :func:`defs.model_name_for`, param values by pid->name), so the
  modeling is load-bearing, not a raw passthrough.
* Everything needed to rebuild the exact ``sfg_`` — the flat ``blks`` grid, the
  per-flow structural fields (``bcnt``/``bmap``/``cid_``/``enbl``/``snap``/
  ``tid_``), every block's unmodeled leaves (``hrns``, block ``id__``, ``type``,
  ``favo``, ``hasb``, ``snap``, ``tid_``, ``cid_``, the model instance's
  non-param fields, and any extra ``mdls`` slots) — is carried verbatim under
  ``recipe["raw"]``, together with ``cg__``, ``pm__`` and ``hist``.

A modeled block is marked in raw by the ABSENCE of ``mdls[0]["id__"]``; on
rebuild those blocks are re-hydrated from ``paths`` in flow order, and every
other block/leaf is emitted unchanged.

Pure functions, no device. Stadium-only. See
``docs/superpowers/specs/2026-07-12-hsp-to-device-transcoder-design.md``.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from . import content
from . import defs

# Block categories that are structural endpoints, not user effect blocks.
# These are carried verbatim in raw and never modeled into ``paths``.
_ENDPOINT_CATEGORIES = {"input", "output", "looper", "split", "join"}


def _category_for(model_id: Optional[int]) -> Optional[str]:
    if model_id is None:
        return None
    name = defs.model_name_for(model_id)
    if name is None:
        return None
    return defs.load_defs().get("model_categories", {}).get(name)


def _is_user_block(category: Optional[str]) -> bool:
    """A modelable user effect block (amp/cab/drive/mod/... ), not an endpoint."""
    if category is None:
        return False
    return category not in _ENDPOINT_CATEGORIES


def _pid_name_maps(model_id: int) -> Tuple[Dict[int, str], Dict[str, int]]:
    """``(pid -> param_name, param_name -> pid)`` for a model from ``defs``.

    Param names that are not unique for the model are omitted so that lifting a
    value into a name-keyed dict can never collide.
    """
    pmeta = defs.load_defs().get("model_params", {}).get(str(model_id), {})
    counts: Dict[str, int] = {}
    for pn in pmeta:
        counts[pn] = counts.get(pn, 0) + 1
    pid2name: Dict[int, str] = {}
    name2pid: Dict[str, int] = {}
    for pn, meta in pmeta.items():
        if counts[pn] != 1:
            continue
        pid = meta.get("id")
        if pid is None:
            continue
        pid2name[pid] = pn
        name2pid[pn] = pid
    return pid2name, name2pid


def _resolve_model_id(name: str) -> Optional[int]:
    """Device model name/id string -> numeric id.

    For the device round-trip the block name is already a device model-id string
    (from :func:`defs.model_name_for`), so a direct ``defs`` lookup is an exact
    inverse. Falls back to helixgen's ingest model-id translation (via the
    authoring bridge) for authored recipes whose block names are helixgen model
    strings — the Phase 2 authoring path.
    """
    mid = defs.model_id_for(name)
    if mid is not None:
        return mid
    try:  # pragma: no cover - authoring fallback, not exercised by the gate
        from . import bridge

        return bridge._default_resolve_model(name)
    except Exception:  # noqa: BLE001
        return None


# --- _sbepgsm dict -> recipe -------------------------------------------------

def _lift_block(block: dict) -> Tuple[str, Dict[str, Any], Optional[str], dict]:
    """Split a user block into ``(model_name, params, irhash, stripped_block)``.

    ``params`` are the named parm values lifted out; ``irhash`` is the hex form
    of ``mdls[0].irmd`` (an IR cab's 16-byte hash) when present, else ``None``;
    ``stripped_block`` is the block with ``mdls[0].id__`` removed, each lifted
    parm leaf's ``valu`` removed, and ``mdls[0].irmd`` removed — so those values
    live ONLY in the recipe (proving real modeling, and re-emitted on rebuild).
    """
    mdls = block.get("mdls") or [{}]
    m0 = mdls[0]
    mid = m0.get("id__")
    name = defs.model_name_for(mid)
    pid2name, _ = _pid_name_maps(mid)

    params: Dict[str, Any] = {}
    lifted_pids: set = set()
    new_parm: List[dict] = []
    for leaf in (m0.get("parm") or []):
        pid = leaf.get("pid_")
        pn = pid2name.get(pid)
        if pn is not None and pid not in lifted_pids and "valu" in leaf:
            params[pn] = leaf["valu"]
            lifted_pids.add(pid)
            new_parm.append({k: v for k, v in leaf.items() if k != "valu"})
        else:
            new_parm.append(leaf)

    irhash: Optional[str] = None
    irmd = m0.get("irmd")
    if isinstance(irmd, (bytes, bytearray)):
        irhash = bytes(irmd).hex()

    new_m0 = {k: v for k, v in m0.items() if k not in ("id__", "irmd")}
    new_m0["parm"] = new_parm
    new_mdls = list(mdls)
    new_mdls[0] = new_m0
    stripped = dict(block)
    stripped["mdls"] = new_mdls
    return name, params, irhash, stripped


def sbepgsm_to_recipe(doc: dict) -> dict:
    """Project a decoded ``_sbepgsm`` dict into a helixgen-style recipe.

    Lossless: models + named params are lifted into ``paths``; everything else
    is carried verbatim under ``raw`` so :func:`recipe_to_sbepgsm` rebuilds the
    exact input. Does not mutate ``doc``.
    """
    doc = copy.deepcopy(doc)
    sfg = doc.get("sfg_", {})

    recipe: Dict[str, Any] = {"name": None, "paths": []}
    raw_sfg: Dict[str, Any] = {k: v for k, v in sfg.items() if k != "flow"}
    raw_flows: List[dict] = []

    for flow in sfg.get("flow", []):
        raw_flow = {k: v for k, v in flow.items() if k != "blks"}
        raw_blks: List[Any] = []
        path_blocks: List[dict] = []
        for item in flow.get("blks", []):
            if not isinstance(item, dict):
                raw_blks.append(item)  # scalar bmap index
                continue
            m0 = (item.get("mdls") or [{}])[0]
            mid = m0.get("id__")
            if _is_user_block(_category_for(mid)):
                name, params, irhash, stripped = _lift_block(item)
                block_spec: Dict[str, Any] = {"block": name, "params": params}
                if irhash is not None:
                    block_spec["irhash"] = irhash
                path_blocks.append(block_spec)
                raw_blks.append(stripped)
            else:
                raw_blks.append(item)  # endpoint / unknown -> verbatim
        raw_flow["blks"] = raw_blks
        raw_flows.append(raw_flow)
        recipe["paths"].append({"blocks": path_blocks})

    raw_sfg["flow"] = raw_flows
    raw: Dict[str, Any] = {"sfg_": raw_sfg}
    for key in ("cg__", "pm__", "hist"):
        if key in doc:
            raw[key] = doc[key]
    # future-proof: keep any unrecognised top-level keys verbatim
    extra = {k: v for k, v in doc.items()
             if k not in ("cg__", "pm__", "hist", "sfg_")}
    if extra:
        raw["_extra"] = extra
    recipe["raw"] = raw
    return recipe


# --- recipe -> _sbepgsm dict -------------------------------------------------

def _inject_block(block: dict, spec: dict) -> None:
    """Re-hydrate a stripped block in place from a modeled ``spec``."""
    name = spec["block"]
    params = spec.get("params") or {}
    mid = _resolve_model_id(name)
    if mid is None:
        raise ValueError(f"cannot resolve device model id for {name!r}")
    m0 = block["mdls"][0]
    m0["id__"] = mid
    irhash = spec.get("irhash")
    if irhash:
        m0["irmd"] = bytes.fromhex(irhash)
    pid2name, _ = _pid_name_maps(mid)
    for leaf in (m0.get("parm") or []):
        if "valu" in leaf:
            continue
        pn = pid2name.get(leaf.get("pid_"))
        if pn is None or pn not in params:
            raise ValueError(
                f"missing param value for pid {leaf.get('pid_')} on {name!r}")
        leaf["valu"] = params[pn]


def recipe_to_sbepgsm(recipe: dict) -> dict:
    """Recipe -> ``_sbepgsm`` dict.

    Two modes:

    * **Rebuild (Phase 1)** — when the recipe carries a device-origin ``raw``
      (from :func:`sbepgsm_to_recipe`), reconstruct the exact input document by
      re-hydrating the modeled blocks and emitting every other leaf verbatim.
    * **Synthesize (Phase 2)** — when there is no ``raw`` (an authored ``.hsp``),
      build a fresh, structurally-valid serial ``_sbepgsm`` from scratch: a
      single serial chain (input endpoint + user blocks + output endpoints), a
      canonical ``hrns``/``type``/``tid_`` scheme, and a minimal ``cg__``/``pm__``.
    """
    if recipe.get("raw"):
        return _rebuild_from_raw(recipe)
    return _synthesize(recipe)


def _rebuild_from_raw(recipe: dict) -> dict:
    """Rebuild the exact ``_sbepgsm`` dict from a recipe carrying device-origin
    ``raw`` (inverse of :func:`sbepgsm_to_recipe`)."""
    raw = copy.deepcopy(recipe["raw"])
    doc: Dict[str, Any] = {}
    if "cg__" in raw:
        doc["cg__"] = raw["cg__"]
    if "hist" in raw:
        doc["hist"] = raw["hist"]
    if "pm__" in raw:
        doc["pm__"] = raw["pm__"]

    sfg = raw["sfg_"]
    doc["sfg_"] = sfg
    paths = recipe.get("paths", [])
    for fi, flow in enumerate(sfg.get("flow", [])):
        model_specs = iter(paths[fi]["blocks"]) if fi < len(paths) else iter(())
        for item in flow.get("blks", []):
            if not isinstance(item, dict):
                continue
            m0 = (item.get("mdls") or [{}])[0]
            if "id__" in m0:
                continue  # verbatim (endpoint / unmodeled) block
            _inject_block(item, next(model_specs))

    for k, v in (raw.get("_extra") or {}).items():
        doc[k] = v
    return doc


# --- Phase 2: synthesize a serial _sbepgsm from an authored recipe -----------
#
# An authored ``.hsp`` (via ``hsp_to_sbepgsm``) yields a recipe with only
# ``{name, paths:[{blocks:[{block, params}]}]}`` and NO device-origin ``raw``.
# We synthesize the whole ``_sbepgsm`` structure. The device tolerates a
# canonical/sequential ``tid_`` + identity ``bmap`` scheme (hardware-confirmed:
# a 151 with every block-slot ``tid_`` reassigned sequentially and
# ``bmap=range(n)`` loaded with blocks intact), so exact routing leaves are not
# required — only structural validity.
#
# Scaffold tables below were extracted from the real serial fixtures
# ``preset_151`` / ``preset_157``:
#   * ``_CATEGORY_TYPE`` — the block-slot ``type`` int per device category.
#   * ``_HRNS_BY_CATEGORY`` — ``hrns.id__`` + parm-shape per block kind; effect
#     (fx) blocks fall back to the constant 420 scaffold.
#   * the four endpoint block dicts a serial path needs, captured verbatim.

# Block-slot ``type`` int keyed by device model category (from the fixtures:
# input=8, output=9, fx=1, amp=5, preamp=3, cab=6, looper=2).
_CATEGORY_TYPE = {
    "input": 8,
    "output": 9,
    "looper": 2,
    "amp": 5,
    "preamp": 3,
    "cab": 6,
    "cab_ir_interp": 6,
    "ir": 6,
}
_DEFAULT_FX_TYPE = 1


def _block_type(category: Optional[str]) -> int:
    return _CATEGORY_TYPE.get(category, _DEFAULT_FX_TYPE)


# ``hrns`` scaffold per block kind: ``(id__, parm-shape)``. ``id__`` and the
# parm shapes were read straight out of 151/157 blocks per category. Effect
# blocks (mono/stereo mod/delay/reverb/drive/wah/...) all fall back to the
# constant fx scaffold (420) — the device does not require the stereo-variant
# ids we also observed (264/97/495/40).
_HRNS_BY_CATEGORY = {
    "input": (778, "empty"),
    "output": (793, "empty"),
    "amp": (760, "amp"),
    "preamp": (760, "amp"),
    "cab": (473, "std"),
    "cab_ir_interp": (473, "std"),
    "ir": (473, "std"),
    "looper": (813, "std"),
}
_DEFAULT_FX_HRNS = (420, "std")


def _hrns(hid: int, shape: str) -> dict:
    """Build one ``hrns`` scaffold dict for harness id ``hid``.

    Parm shapes observed in the fixtures:
      * ``empty`` — input/output endpoints carry no harness parm.
      * ``amp``   — amp/preamp carry only pid 13 (``-1``).
      * ``std``   — everything else carries pids 11/12/13 (false/true/-1).
    """
    if shape == "empty":
        parm: List[dict] = []
    elif shape == "amp":
        parm = [{"accs": 0, "cid_": 0, "mid_": hid, "pid_": 13,
                 "snap": False, "tid_": 0, "valu": -1}]
    else:  # "std"
        parm = [
            {"accs": 0, "cid_": 0, "mid_": hid, "pid_": 11,
             "snap": False, "tid_": 0, "valu": False},
            {"accs": 0, "cid_": 0, "mid_": hid, "pid_": 12,
             "snap": False, "tid_": 0, "valu": True},
            {"accs": 0, "cid_": 0, "mid_": hid, "pid_": 13,
             "snap": False, "tid_": 0, "valu": -1},
        ]
    return {"cid_": 0, "enbl": 1, "id__": hid, "lbid": -1, "parm": parm,
            "snap": False, "tid_": 0, "vers": 0}


def _hrns_for(category: Optional[str]) -> dict:
    hid, shape = _HRNS_BY_CATEGORY.get(category, _DEFAULT_FX_HRNS)
    return _hrns(hid, shape)


# Canonical endpoint block dicts, captured verbatim from ``preset_151`` flow 0
# (block-level ``id__`` is reassigned per use; everything else — model id,
# hrns, endpoint parm defaults, type — is the device's own values).
_INPUT_INST1 = {
    "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 778, "lbid": -1, "parm": [],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 0,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 770, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 770, "pid_": 2, "snap": False, "tid_": 0, "valu": 1},
        {"accs": 0, "cid_": 0, "mid_": 770, "pid_": 3, "snap": False, "tid_": 0, "valu": 0.0},
        {"accs": 0, "cid_": 0, "mid_": 770, "pid_": 4, "snap": False, "tid_": 0, "valu": True},
        {"accs": 0, "cid_": 0, "mid_": 770, "pid_": 5, "snap": False, "tid_": 0,
         "valu": -60.70000076293945},
        {"accs": 0, "cid_": 0, "mid_": 770, "pid_": 6, "snap": False, "tid_": 0,
         "valu": 0.009999999776482582},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 8,
}
_OUTPUT_MATRIX = {
    "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 793, "lbid": -1, "parm": [],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 13,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 783, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 783, "pid_": 1, "snap": False, "tid_": 0, "valu": 0.5},
        {"accs": 0, "cid_": 0, "mid_": 783, "pid_": 2, "snap": False, "tid_": 0, "valu": 0.0},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 9,
}
_INPUT_NONE = {
    "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 778, "lbid": -1, "parm": [],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 14,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 771, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 771, "pid_": 1, "snap": False, "tid_": 0, "valu": 0.0},
        {"accs": 0, "cid_": 0, "mid_": 771, "pid_": 3, "snap": False, "tid_": 0, "valu": False},
        {"accs": 0, "cid_": 0, "mid_": 771, "pid_": 4, "snap": False, "tid_": 0, "valu": -48.0},
        {"accs": 0, "cid_": 0, "mid_": 771, "pid_": 5, "snap": False, "tid_": 0,
         "valu": 0.10000000149011612},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 8,
}
_OUTPUT_NONE = {
    "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 793, "lbid": -1, "parm": [],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 27,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 789, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 789, "pid_": 1, "snap": False, "tid_": 0, "valu": 0.5},
        {"accs": 0, "cid_": 0, "mid_": 789, "pid_": 2, "snap": False, "tid_": 0, "valu": 0.0},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 9,
}


def _synth_parm(model_id: int, params: Dict[str, Any]) -> List[dict]:
    """Full parm list for ``model_id`` from ``defs``, in pid order.

    Values come from ``params`` (keyed by DEVICE param name) when present —
    preserving the caller's value type (bool/int/float), so lifted values
    round-trip exactly — else the model default from ``defs``.
    """
    mp = defs.load_defs().get("model_params", {}).get(str(model_id), {})
    parm: List[dict] = []
    for name, meta in mp.items():
        pid = meta.get("id")
        if pid is None:
            continue
        valu = params[name] if name in params else meta.get("def", 0.0)
        parm.append({"accs": 0, "cid_": 0, "mid_": model_id, "pid_": pid,
                     "snap": False, "tid_": 0, "valu": valu})
    parm.sort(key=lambda p: p["pid_"])
    return parm


def _make_user_block(spec: dict, inst_id: int) -> dict:
    """Synthesize a device block dict for a modeled recipe block."""
    name = spec["block"]
    params = spec.get("params") or {}
    mid = _resolve_model_id(name)
    if mid is None:
        raise ValueError(f"cannot resolve device model id for {name!r}")
    category = _category_for(mid)
    m0: Dict[str, Any] = {
        "cid_": 0, "enbl": 1, "id__": mid, "lbid": -1,
        "parm": _synth_parm(mid, params),
        "snap": False, "tid_": 0, "vers": 0,
    }
    # An IR cab references its impulse response by the model instance's ``irmd``
    # = the 16-byte IR hash (``bytes.fromhex(irhash)``). Inject it so the
    # synthesized cab resolves on the device instead of dropping to no-IR.
    irhash = spec.get("irhash")
    if irhash and category == "ir":
        m0["irmd"] = bytes.fromhex(irhash)
    return {
        "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
        "hrns": _hrns_for(category),
        "id__": inst_id,
        "mdls": [m0],
        "snap": False,
        "tid_": inst_id,
        "type": _block_type(category),
    }


def _endpoint(template: dict, inst_id: int) -> dict:
    e = copy.deepcopy(template)
    e["id__"] = inst_id
    return e


def _assemble_flow(blocks: List[dict]) -> dict:
    """Wrap ordered block dicts into a flow: flat ``blks`` grid + ``bmap``.

    Each block's ``id__`` is taken as already-assigned (globally monotonic).
    The flat ``blks`` alternate ``[slot_index, block, …]``; ``bmap[slot]`` is the
    block's instance id; ``bcnt`` == number of blocks.
    """
    blks: List[Any] = []
    bmap: List[int] = []
    for slot, blk in enumerate(blocks):
        blks.append(slot)
        blks.append(blk)
        bmap.append(blk["id__"])
    return {"bcnt": len(blocks), "blks": blks, "bmap": bmap,
            "cid_": 0, "enbl": 1, "snap": False, "tid_": 0}


def synthesize_serial_sfg(paths: List[dict]) -> Tuple[dict, int]:
    """Build an ``sfg_`` dict for a single serial modeled path.

    Flow 0 = input endpoint + each user block + output endpoints
    (``OutputMatrix``, ``InputNone``, ``OutputNone``); flow 1 = a valid empty
    path (``InputNone``, ``OutputMatrix``, ``InputNone``, ``OutputNone``), as the
    real serial fixtures carry. Instance ids are globally monotonic across both
    flows. Returns ``(sfg_dict, next_free_id)``.
    """
    modeled = (paths[0].get("blocks") if paths else None) or []
    next_id = 0

    f0: List[dict] = []
    f0.append(_endpoint(_INPUT_INST1, next_id)); next_id += 1
    for spec in modeled:
        f0.append(_make_user_block(spec, next_id)); next_id += 1
    for tmpl in (_OUTPUT_MATRIX, _INPUT_NONE, _OUTPUT_NONE):
        f0.append(_endpoint(tmpl, next_id)); next_id += 1

    f1: List[dict] = []
    for tmpl in (_INPUT_NONE, _OUTPUT_MATRIX, _INPUT_NONE, _OUTPUT_NONE):
        f1.append(_endpoint(tmpl, next_id)); next_id += 1

    sfg = {"enbl": 1, "fcnt": 2,
           "flow": [_assemble_flow(f0), _assemble_flow(f1)]}
    return sfg, next_id


def _synth_cg(max_id: int) -> dict:
    """A minimal valid ``cg__``: 8 empty snapshot slots, no controllers, next-id
    counters set past the largest instance id. Volatile (the device recomputes
    counters on save) — not part of the fidelity comparison."""
    snps = [{"bpm_": 120.0, "camv": [], "colr": 1, "exsw": -1, "iras": [],
             "name": f"SNAPSHOT {i + 1}", "si__": i, "tamv": [], "tgls": [],
             "vald": True} for i in range(8)]
    return {
        "asnp": 0,
        "entt": {
            "cmnd": [],
            "ctm_": {"htid": [], "ptid": [], "sirt": [], "stid": []},
            "ctrl": [],
            "sm__": {"scid": [], "ssi_": []},
            "snps": snps,
            "srcs": [],
            "trgs": [],
        },
        "nxtc": max_id + 1,
        "nxti": 0,
        "nxtm": 1,
        "nxts": 8,
        "nxtt": max_id + 1,
    }


def _synth_pm() -> List[dict]:
    """A minimal valid ``pm__`` preset-param list, mirroring the standard key set
    an HX Edit import emits (clip, 2x12 floorboard stomps, tempo, exp-switch,
    instrument impedance, xy-controller) with neutral values."""
    pm: List[dict] = [
        {"key_": "preset.clip.end", "type": "f", "val_": 0.0},
        {"key_": "preset.clip.filename", "type": "s", "val_": ""},
        {"key_": "preset.clip.path", "type": "s", "val_": ""},
        {"key_": "preset.clip.start", "type": "f", "val_": 0.0},
        {"key_": "preset.expsw.active", "type": "i", "val_": 1},
    ]
    for row in ("a", "b"):
        for n in range(1, 13):
            base = f"preset.floorboard.stomp.{row}.{n}"
            pm.append({"key_": f"{base}.color", "type": "i", "val_": 1})
            pm.append({"key_": f"{base}.label", "type": "s", "val_": ""})
            pm.append({"key_": f"{base}.topidx", "type": "i", "val_": 0})
    pm += [
        {"key_": "preset.inst1.z", "type": "i", "val_": 1},
        {"key_": "preset.inst2.z", "type": "i", "val_": 1},
        {"key_": "preset.meta.info", "type": "s", "val_": ""},
        {"key_": "preset.tempo.bpm", "type": "f", "val_": 120.0},
        {"key_": "preset.xyctrl.rbtime", "type": "f", "val_": 0.5},
        {"key_": "preset.xyctrl.rubberband", "type": "i", "val_": 1},
        {"key_": "preset.xyctrl.x", "type": "i", "val_": 0},
        {"key_": "preset.xyctrl.y", "type": "i", "val_": 0},
    ]
    return pm


def _synthesize(recipe: dict) -> dict:
    """Synthesize a fresh serial ``_sbepgsm`` dict from a raw-less recipe."""
    paths = recipe.get("paths") or []
    sfg, next_id = synthesize_serial_sfg(paths)
    return {"cg__": _synth_cg(next_id - 1), "pm__": _synth_pm(), "sfg_": sfg}


# --- authored .hsp -> device _sbepgsm bytes ----------------------------------

def hsp_to_sbepgsm(hsp_body: dict, *, dsp: int = 0,
                   strict: bool = False) -> bytes:
    """Transcode a helixgen ``.hsp`` body into stored device content bytes.

    SERIAL only (path ``dsp``, default 0): resolve each user block's helixgen
    model to a device model + device param names via
    :func:`bridge.hsp_to_chain_with_irs`, build a modeled recipe, synthesize the
    ``_sbepgsm`` structure (:func:`recipe_to_sbepgsm`), and serialize with
    :func:`content.encode_content_data`. This is the offline blob the device
    install path installs; no template, no device.
    """
    from . import bridge

    chain = bridge.hsp_to_chain_with_irs(hsp_body, dsp=dsp, strict=strict)
    blocks: List[Dict[str, Any]] = []
    for dev_id, params, irhash in chain:
        spec: Dict[str, Any] = {"block": defs.model_name_for(dev_id),
                                "params": params}
        if irhash:
            spec["irhash"] = irhash
        blocks.append(spec)
    recipe = {"name": None, "paths": [{"blocks": blocks}]}
    doc = recipe_to_sbepgsm(recipe)
    return content.encode_content_data(doc)
