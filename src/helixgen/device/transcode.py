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
        structural: List[dict] = []
        input_mode: Optional[str] = None
        for item in flow.get("blks", []):
            if not isinstance(item, dict):
                raw_blks.append(item)  # scalar bmap index
                continue
            m0 = (item.get("mdls") or [{}])[0]
            mid = m0.get("id__")
            category = _category_for(mid)
            if category == "input" and input_mode is None:
                input_mode = _INPUT_MODEL_INV.get(mid)
            if _is_user_block(category):
                name, params, irhash, stripped = _lift_block(item)
                block_spec: Dict[str, Any] = {"block": name, "params": params}
                if irhash is not None:
                    block_spec["irhash"] = irhash
                path_blocks.append(block_spec)
                raw_blks.append(stripped)
            else:
                # Split/join are ALSO surfaced (verbatim) as a routing skeleton
                # OUTSIDE raw, so the synthesis path can re-emit the parallel
                # structure after ``raw`` is dropped (dual-amp spec §3.1).
                if category in ("split", "join"):
                    structural.append(copy.deepcopy(item))
                raw_blks.append(item)  # endpoint / unknown -> verbatim
        raw_flow["blks"] = raw_blks
        raw_flows.append(raw_flow)
        path_entry: Dict[str, Any] = {"blocks": path_blocks}
        if input_mode is not None:
            path_entry["input"] = input_mode
        if structural:
            path_entry["structural"] = structural
        recipe["paths"].append(path_entry)

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

# Block-slot ``type`` int keyed by device model category. Read out of the real
# fixtures (input=8, output=9, fx=1, amp=5, cab=6, looper=2, split=3, join=4).
#
# NOTE (bugfix, dual-amp spec §3.3): ``preamp`` was previously mapped to 3, which
# COLLIDES with the split type-int. A preamp is an amp head without the power/cab
# stage, so it takes the amp slot-type (5); ``split``/``join`` own 3/4.
_CATEGORY_TYPE = {
    "input": 8,
    "output": 9,
    "looper": 2,
    "split": 3,
    "join": 4,
    "amp": 5,
    "preamp": 5,
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
    # split/join carry hrns id 479 with a single pid-11 bypass flag (from 152).
    "split": (479, "split"),
    "join": (479, "split"),
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
    elif shape == "split":
        parm = [{"accs": 0, "cid_": 0, "mid_": hid, "pid_": 11,
                 "snap": False, "tid_": 0, "valu": False}]
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


# ``P35_OutputPath2A`` (model 779) — the lane-A output of an intra-flow split,
# captured verbatim from ``preset_152`` flow 0. Paired with a second
# InputNone/OutputNone group when a flow carries a split.
_OUTPUT_PATH2A = {
    "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 793, "lbid": -1, "parm": [],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 13,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 779, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 779, "pid_": 1, "snap": False, "tid_": 0, "valu": 0.5},
        {"accs": 0, "cid_": 0, "mid_": 779, "pid_": 2, "snap": False, "tid_": 0, "valu": 0.0},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 9,
}

# Split (``P35_AppDSPSplitY`` model 475) / join (``P35_AppDSPJoin`` model 478)
# scaffolds captured verbatim from ``preset_152`` flow 0. ``bblk``/``bflw`` are
# partner cross-references the device uses to pair a split with its join; the
# exact semantics are not decoded offline (see the module note + spec §5), so on
# synthesis we re-point them at the emitted partner's id as a best effort.
_SPLIT_SCAFFOLD = {
    "bblk": 0, "bflw": 0, "cid_": 0, "enbl": 1, "favo": 0, "hasb": True,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 479, "lbid": -1,
             "parm": [{"accs": 0, "cid_": 0, "mid_": 479, "pid_": 11,
                       "snap": False, "tid_": 0, "valu": False}],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 0,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 475, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 475, "pid_": 1, "snap": False, "tid_": 0, "valu": 0.5},
        {"accs": 0, "cid_": 0, "mid_": 475, "pid_": 2, "snap": False, "tid_": 0, "valu": 0.5},
        {"accs": 0, "cid_": 0, "mid_": 475, "pid_": 3, "snap": False, "tid_": 0, "valu": False},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 3,
}
_JOIN_SCAFFOLD = {
    "bblk": 0, "bflw": 0, "cid_": 0, "enbl": 1, "favo": 0, "hasb": True,
    "hrns": {"cid_": 0, "enbl": 1, "id__": 479, "lbid": -1,
             "parm": [{"accs": 0, "cid_": 0, "mid_": 479, "pid_": 11,
                       "snap": False, "tid_": 0, "valu": False}],
             "snap": False, "tid_": 0, "vers": 0},
    "id__": 0,
    "mdls": [{"cid_": 0, "enbl": 1, "id__": 478, "lbid": -1, "parm": [
        {"accs": 0, "cid_": 0, "mid_": 478, "pid_": 1, "snap": False, "tid_": 0, "valu": 0.0},
        {"accs": 0, "cid_": 0, "mid_": 478, "pid_": 2, "snap": False, "tid_": 0, "valu": 0.0},
        {"accs": 0, "cid_": 0, "mid_": 478, "pid_": 3, "snap": False, "tid_": 0, "valu": 0.0},
        {"accs": 0, "cid_": 0, "mid_": 478, "pid_": 4, "snap": False, "tid_": 0, "valu": 1.0},
        {"accs": 0, "cid_": 0, "mid_": 478, "pid_": 5, "snap": False, "tid_": 0, "valu": False},
        {"accs": 0, "cid_": 0, "mid_": 478, "pid_": 6, "snap": False, "tid_": 0, "valu": 0.0},
    ], "snap": False, "tid_": 0, "vers": 0}],
    "snap": False, "tid_": 0, "type": 4,
}

# Live-input endpoint device model id per recipe ``input`` routing keyword.
_INPUT_MODEL = {
    "inst1": 770,   # P35_InputInst1
    "inst2": 774,   # P35_InputInst2
    "both": 769,    # P35_InputInst1_2 (stereo, both jacks)
    "none": 771,    # P35_InputNone
}
_INPUT_MODEL_INV = {v: k for k, v in _INPUT_MODEL.items()}


def _make_endpoint_model(model_id: int) -> dict:
    """Synthesize an endpoint's ``mdls[0]`` from ``defs`` defaults (for the
    input variants — inst2/both — we did not capture verbatim templates for)."""
    return {"cid_": 0, "enbl": 1, "id__": model_id, "lbid": -1,
            "parm": _synth_parm(model_id, {}), "snap": False, "tid_": 0, "vers": 0}


def _make_input_endpoint(mode: Optional[str], inst_id: int) -> dict:
    """Build the live-input endpoint block for a flow given its ``input`` mode.

    ``inst1``/``none`` reuse the verbatim captured templates; ``inst2``/``both``
    are synthesized from ``defs`` (valid model + default params)."""
    if mode in (None, "inst1"):
        return _endpoint(_INPUT_INST1, inst_id)
    if mode == "none":
        return _endpoint(_INPUT_NONE, inst_id)
    model_id = _INPUT_MODEL.get(mode, _INPUT_MODEL["inst1"])
    return {
        "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
        "hrns": _hrns_for("input"),
        "id__": inst_id,
        "mdls": [_make_endpoint_model(model_id)],
        "snap": False, "tid_": 0, "type": 8,
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


# A DSP flow is a FIXED 28-slot grid: two rows of 14. Row 0 = grid positions
# 0..13 (input at 0, output at 13); row 1 = 14..27 (input at 14, output at 27).
# The device wires a path from this grid — the old "len(blocks) slots, sequential
# positions, id-list bmap" scheme drew NO connecting lines (hardware-confirmed
# 2026-07-13). See docs/superpowers/specs/2026-07-13-device-re-findings.md.
_GRID_SLOTS = 28
_ROW0_INPUT = 0
_ROW0_OUTPUT = 13
_ROW1_INPUT = 14
_ROW1_OUTPUT = 27
_ROW0_LAST_USER = 12   # row 0 user blocks live at grid positions 1..12
_ROW1_LAST_USER = 26   # row 1 user blocks live at grid positions 15..26


def _canonical_flow(placements: List[Tuple[int, dict]], base: int) -> dict:
    """Assemble a flow from ``(gridpos, block)`` placements onto the real 28-slot
    device grid.

    Each block's ``id__``/``tid_`` is set to ``base + gridpos`` so the identity
    ``bmap = [base .. base+27]`` is self-consistent (``bmap[gridpos] == id at
    gridpos`` — the encoding real device presets use, verified against fixtures
    151/152). ``bcnt`` is the fixed grid size, NOT the block count. ``blks``
    alternates ``[gridpos, block, …]`` for occupied positions only.
    """
    blks: List[Any] = []
    for gp, blk in sorted(placements, key=lambda t: t[0]):
        blk["id__"] = base + gp
        blk["tid_"] = base + gp
        blks.append(gp)
        blks.append(blk)
    return {"bcnt": _GRID_SLOTS, "blks": blks,
            "bmap": [base + i for i in range(_GRID_SLOTS)],
            "cid_": 0, "enbl": 1, "snap": False, "tid_": 0}


def _assemble_flow(blocks: List[dict]) -> dict:
    """Legacy contiguous assembler (kept for callers/tests that pass a flat block
    list); placements land at grid positions 0,1,2,… Prefer :func:`_canonical_flow`
    for device-faithful routing."""
    return _canonical_flow([(i, b) for i, b in enumerate(blocks)], 0)


def _default_input_mode(path_index: int) -> str:
    """Default live-input routing per DSP path when the recipe omits ``input``.

    Path 0 defaults to the mono Instrument-1 jack (matches the historical serial
    behaviour + the offline structural gate); every later path defaults to
    ``none`` (a valid empty carrier, as the serial fixtures show)."""
    return "inst1" if path_index == 0 else "none"


def _make_structural_block(scaffold: dict, inst_id: int) -> dict:
    """Emit a split/join block from a scaffold (verbatim device dict OR one of
    the captured ``_SPLIT_SCAFFOLD``/``_JOIN_SCAFFOLD`` templates), stamping a
    fresh instance id. ``bblk``/``bflw`` are re-pointed by the caller."""
    blk = copy.deepcopy(scaffold)
    blk["id__"] = inst_id
    blk["tid_"] = inst_id
    return blk


def synthesize_sfg(paths: List[dict]) -> Tuple[dict, int, Dict[Tuple[int, int, int], int]]:
    """Build an ``sfg_`` dict for every modeled DSP path (dual-amp spec §3.2).

    Emits ONE populated ``sfg_.flow`` per path (not a fixed empty carrier): each
    flow gets its live input endpoint (per the path's ``input`` routing), its
    user blocks, any split/join routing skeleton (``paths[i]["structural"]``),
    and an ``OutputMatrix``/``InputNone``/``OutputNone`` group (both paths sum at
    the matrix). The device always carries ``fcnt`` = 2 DSP flows, so a single
    modeled path still emits a second (empty) flow.

    Instance ids (``id__``/``tid_``) are globally monotonic across flows and the
    ``bmap`` is the ordered id grid — the sequential/identity scheme the serial
    path already proved hardware-tolerant (split/join routing is the residual
    hardware-validation risk; see the module note + spec §5).

    Returns ``(sfg_dict, next_free_id, instance_ids)`` where ``instance_ids`` maps
    each user block's ``(path_index, lane, pos)`` coordinate to its assigned
    device instance id (``eID_``) — the coupling point the snapshot/controller
    synthesis consumes.
    """
    instance_ids: Dict[Tuple[int, int, int], int] = {}
    flows: List[dict] = []

    n_flows = max(2, len(paths))  # device always carries fcnt == 2 DSP flows
    for pi in range(n_flows):
        base = _GRID_SLOTS * pi
        path = paths[pi] if pi < len(paths) else {}
        modeled = path.get("blocks") or []
        structural = path.get("structural") or []
        mode = path.get("input") or _default_input_mode(pi)

        # (gridpos, block) placements on this flow's 28-slot grid.
        placements: List[Tuple[int, dict]] = [
            (_ROW0_INPUT, _make_input_endpoint(mode, 0))]

        if not structural:
            # SERIAL / dual-DSP path: user blocks fill row 0 (positions 1..12),
            # then the OutputMatrix group. (Hardware-confirmed 2026-07-13.)
            gp = 1
            for bi, spec in enumerate(modeled):
                if gp > _ROW0_LAST_USER:
                    break  # row 0 is full; overflow blocks are dropped
                lane = int(spec.get("lane", 0))
                pos = int(spec.get("pos", bi))
                placements.append((gp, _make_user_block(spec, 0)))
                instance_ids[(pi, lane, pos)] = base + gp
                gp += 1
            placements.append((_ROW0_OUTPUT, _endpoint(_OUTPUT_MATRIX, 0)))
            placements.append((_ROW1_INPUT, _endpoint(_INPUT_NONE, 0)))
            placements.append((_ROW1_OUTPUT, _endpoint(_OUTPUT_NONE, 0)))
        else:
            # SPLIT path (best-effort, hardware-iterated): lane-0 blocks in row 0,
            # lane-1 blocks in row 1, split/join between them, OutputPath2A at the
            # row-0 output. Split routing bytes (bblk/bflw) remain the residual
            # RE risk — see spec §5.
            lane0 = [s for s in modeled if int(s.get("lane", 0)) == 0]
            lane1 = [s for s in modeled if int(s.get("lane", 0)) == 1]
            gp = 1
            for bi, spec in enumerate(lane0):
                if gp > _ROW0_LAST_USER - 1:
                    break
                placements.append((gp, _make_user_block(spec, 0)))
                instance_ids[(pi, 0, int(spec.get("pos", bi)))] = base + gp
                gp += 1
            split_gp = gp
            join_gp = gp + 1
            gp1 = 15
            for bi, spec in enumerate(lane1):
                if gp1 > _ROW1_LAST_USER:
                    break
                placements.append((gp1, _make_user_block(spec, 0)))
                instance_ids[(pi, 1, int(spec.get("pos", bi)))] = base + gp1
                gp1 += 1
            for scaffold in structural:
                typ = scaffold.get("type")
                slot = split_gp if typ == 3 else join_gp
                blk = _make_structural_block(scaffold, 0)
                if typ == 3:
                    blk["bblk"], blk["bflw"] = base + join_gp, pi
                elif typ == 4:
                    blk["bblk"], blk["bflw"] = base + split_gp, pi
                placements.append((slot, blk))
            placements.append((_ROW0_OUTPUT, _endpoint(_OUTPUT_PATH2A, 0)))
            placements.append((_ROW1_INPUT, _endpoint(_INPUT_NONE, 0)))
            placements.append((_ROW1_OUTPUT, _endpoint(_OUTPUT_NONE, 0)))

        flows.append(_canonical_flow(placements, base))

    sfg = {"enbl": 1, "fcnt": n_flows, "flow": flows}
    return sfg, _GRID_SLOTS * n_flows, instance_ids


def synthesize_serial_sfg(paths: List[dict]) -> Tuple[dict, int]:
    """Back-compat shim: single-path serial synthesis (drops the instance map).

    Superseded by :func:`synthesize_sfg`, which reads every DSP path and returns
    the ``(path, lane, pos) -> instance id`` map. Retained so existing callers /
    tests that only want ``(sfg, next_id)`` keep working."""
    sfg, next_id, _ = synthesize_sfg(paths[:1] if paths else [])
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


def _snap_meta(meta: dict, i: int) -> Tuple[str, int, float]:
    """``(name, exsw, bpm_)`` for snapshot ``i`` from a recipe snapshot-meta dict
    (accepts both device keys ``exsw``/``bpm_`` and ``.hsp`` keys
    ``expsw``/``bpm``/``tempo``)."""
    name = meta.get("name") or f"SNAPSHOT {i + 1}"
    exsw = meta.get("exsw", meta.get("expsw", -1))
    bpm = meta.get("bpm", meta.get("bpm_", meta.get("tempo", 120.0)))
    return name, exsw, float(bpm)


def _param_pid(model_id: int, param_name: str) -> Optional[int]:
    mp = defs.load_defs().get("model_params", {}).get(str(model_id), {})
    meta = mp.get(param_name)
    return meta.get("id") if isinstance(meta, dict) else None


def _controller_locl_ctxt(source: Any) -> Optional[Tuple[int, int]]:
    """Map a ``.hsp`` controller source id -> device ``(locl, ctxt)``.

    Confirmed by the 2026-07-13 device-RE capture (packet-verified vs preset
    cid 1064): A-bank footswitch ``0x010101NN`` -> ``(25 + NN, 1)``; the
    expression toe switch ``0x01010500`` (EXP1Toe) and EXP pedals ``0x0102010M``
    -> ``(42, ctxt)`` with ctxt 0 for EXP1, 1 for EXP2. Returns ``None`` for
    out-of-scope sources (stomp bank B ``0x010102NN``, looper command
    ``0x010104NN``) so the caller can skip them."""
    if not isinstance(source, int) or isinstance(source, bool):
        return None
    if source == 0x01010500:            # EXP1Toe (wah toe switch)
        return (42, 0)
    hi = source & 0xFFFFFF00
    lo = source & 0xFF
    if hi == 0x01010100:                # stomp bank A footswitch (NN = FS#-1)
        return (25 + lo, 1)
    if hi == 0x01020100:                # EXP1 / EXP2 pedal (M = 0/1)
        return (42, 0 if lo == 0 else 1)
    return None                          # bank B / looper command -> out of scope


def _make_src(src_id: int, locl: int, ctxt: int, byps: bool) -> dict:
    """A controller ``srcs`` entry (mirrors the fixture shape)."""
    return {"byps": byps, "cmds": [-1, -1], "cnt1": 0, "cnt2": 0, "cnt3": 0,
            "ctxt": ctxt, "id__": src_id, "locl": locl, "mtms": 0, "mtyp": 0,
            "type": 1}


def _synth_cg_from_recipe(
    recipe: dict,
    instance_ids: Dict[Tuple[int, int, int], int],
    max_id: int,
) -> dict:
    """Build the device ``cg__`` snapshot machinery from a recipe's inline
    snapshot arrays (snapshots spec Part A).

    Each user block may carry ``snap_bypass`` (per-snapshot bool list) and
    ``snap_params`` (``{device_param_name: per-snapshot value list}``). For every
    block/param that ACTUALLY VARIES across snapshots we emit one ``trgs`` target
    (type1/enty2/pid0 bypass, or type2/enty3/pidN param), keyed by that block's
    device instance id (``eID_``, from ``instance_ids``). Each snapshot's
    ``tamv`` is the flat ``[trg_id, value, …]`` over every tracked target,
    ``ctm_.stid`` lists them, and ``ctm_.ptid`` packs the param targets
    (``(eID_<<16 | pid_) -> trg_id``).

    A tone with no snapshot variation falls back to the blank-8 ``cg__``.
    """
    snap_meta = recipe.get("snapshots") or []
    trgs: List[dict] = []
    stid: List[int] = []
    ptid: List[int] = []
    tracked: List[Tuple[int, List[Any]]] = []  # (trg_id, per-snapshot values)
    trg_index: Dict[Tuple[int, int, int], int] = {}  # (eID_, pid_, type) -> trg id
    # Controller/target/source ids are 1-BASED — the device treats id 0 as
    # null/unassigned, so a 0-based id silently kills the binding (hardware-
    # confirmed against HX Edit's own encoding, 2026-07-13).
    next_trg = 1

    def _new_trg(entry: dict, key: Tuple[int, int, int]) -> int:
        nonlocal next_trg
        tid = next_trg
        next_trg += 1
        entry["id__"] = tid
        trgs.append(entry)
        trg_index[key] = tid
        return tid

    # 1) Snapshot-tracked targets (Part A).
    for pi, path in enumerate(recipe.get("paths") or []):
        for bi, spec in enumerate(path.get("blocks") or []):
            lane = int(spec.get("lane", 0))
            pos = int(spec.get("pos", bi))
            eid = instance_ids.get((pi, lane, pos))
            if eid is None:
                continue
            mid = _resolve_model_id(spec["block"])
            if mid is None:
                continue
            bypass = spec.get("snap_bypass")
            if isinstance(bypass, list) and len({bool(x) for x in bypass}) > 1:
                tid = _new_trg({"eID_": eid, "enty": 2, "mmid": mid,
                                "pid_": 0, "slot": 0, "type": 1}, (eid, 0, 1))
                stid.append(tid)
                tracked.append((tid, [bool(x) for x in bypass]))
            for pname, pvals in (spec.get("snap_params") or {}).items():
                if not (isinstance(pvals, list) and len({repr(x) for x in pvals}) > 1):
                    continue
                pid = _param_pid(mid, pname)
                if pid is None:
                    continue
                tid = _new_trg({"eID_": eid, "enty": 3, "mmid": mid, "pid_": pid,
                                "pmid": mid, "ppid": pid, "slot": 0, "type": 2},
                               (eid, pid, 2))
                stid.append(tid)
                ptid.extend([(eid << 16) | pid, tid])
                tracked.append((tid, list(pvals)))

    # 2) FS/EXP controller graph (Part B). Reuse a snapshot trg when the same
    #    target is both scene-tracked and controller-driven.
    srcs: List[dict] = []
    ctrl: List[dict] = []
    scid: List[Any] = []
    next_src = 1        # 1-based (0 == null on the device)
    next_ctrl = 1       # 1-based
    for pi, path in enumerate(recipe.get("paths") or []):
        for bi, spec in enumerate(path.get("blocks") or []):
            lane = int(spec.get("lane", 0))
            pos = int(spec.get("pos", bi))
            eid = instance_ids.get((pi, lane, pos))
            if eid is None:
                continue
            mid = _resolve_model_id(spec["block"])
            if mid is None:
                continue
            fsb = spec.get("fs_bypass")
            if isinstance(fsb, dict):
                lc = _controller_locl_ctxt(fsb.get("source"))
                if lc is not None:
                    locl, ctxt = lc
                    sid = next_src; next_src += 1
                    srcs.append(_make_src(sid, locl, ctxt, byps=True))
                    tid = trg_index.get((eid, 0, 1))
                    if tid is None:
                        tid = _new_trg({"eID_": eid, "enty": 2, "mmid": mid,
                                        "pid_": 0, "slot": 0, "type": 1},
                                       (eid, 0, 1))
                    cid = next_ctrl; next_ctrl += 1
                    ctrl.append({"behv": 0, "cid_": cid, "curv": 5, "dlay": 0,
                                 "goid": 0, "max_": True, "min_": False,
                                 "thrs": 0.0, "tid_": tid,
                                 "togl": fsb.get("behavior") == "momentary",
                                 "trig": sid, "type": 1})
                    scid.extend([tid, [cid]])
            for pname, meta in (spec.get("exp_params") or {}).items():
                lc = _controller_locl_ctxt(meta.get("source"))
                pid = _param_pid(mid, pname)
                if lc is None or pid is None:
                    continue
                locl, ctxt = lc
                sid = next_src; next_src += 1
                srcs.append(_make_src(sid, locl, ctxt, byps=False))
                tid = trg_index.get((eid, pid, 2))
                if tid is None:
                    tid = _new_trg({"eID_": eid, "enty": 3, "mmid": mid,
                                    "pid_": pid, "pmid": mid, "ppid": pid,
                                    "slot": 0, "type": 2}, (eid, pid, 2))
                    ptid.extend([(eid << 16) | pid, tid])
                cid = next_ctrl; next_ctrl += 1
                ctrl.append({"behv": 2, "cid_": cid, "curv": 5, "dlay": 0,
                             "goid": 0, "max_": float(meta.get("max", 1.0)),
                             "min_": float(meta.get("min", 0.0)), "thrs": 0.0,
                             "tid_": tid, "togl": False, "trig": sid, "type": 3})
                scid.extend([tid, [cid]])

    if not tracked and not ctrl:
        return _synth_cg(max_id)

    snps: List[dict] = []
    for i in range(8):
        tamv: List[Any] = []
        for tid, vals in tracked:
            v = vals[i] if i < len(vals) else vals[-1]
            tamv.extend([tid, v])
        meta = snap_meta[i] if i < len(snap_meta) else {}
        name, exsw, bpm = _snap_meta(meta, i)
        snps.append({"bpm_": bpm, "camv": [], "colr": 1, "exsw": exsw,
                     "iras": [], "name": name, "si__": i, "tamv": tamv,
                     "tgls": [], "vald": True})

    return {
        "asnp": 0,
        "entt": {
            "cmnd": [],
            "ctm_": {"htid": [], "ptid": ptid, "sirt": [], "stid": stid},
            "ctrl": ctrl,
            "sm__": {"scid": scid, "ssi_": []},
            "snps": snps,
            "srcs": srcs,
            "trgs": trgs,
        },
        "nxtc": next_ctrl,   # next-free controller id (not tied to block ids)
        "nxti": 0,
        "nxtm": 1,
        "nxts": next_src,    # next-free source id
        "nxtt": next_trg,    # next-free target id
    }


def _scribble_for(sources: Optional[Dict[int, dict]]) -> Dict[Tuple[str, int], dict]:
    """Map a recipe ``sources`` dict (``{source_id: {fs_color,fs_label,
    fs_topidx}}``) onto ``(row, stomp_index)`` scribble-strip entries.

    Stomp bank A source ``0x010101NN`` -> row ``a`` stomp ``NN+1``; bank B
    ``0x010102NN`` -> row ``b`` (spec 2 Part B). Out-of-scope sources are
    ignored."""
    out: Dict[Tuple[str, int], dict] = {}
    for src, cfg in (sources or {}).items():
        if not isinstance(src, int):
            continue
        hi, lo = src & 0xFFFFFF00, src & 0xFF
        if hi == 0x01010100:
            out[("a", lo + 1)] = cfg
        elif hi == 0x01010200:
            out[("b", lo + 1)] = cfg
    return out


def _synth_pm(sources: Optional[Dict[int, dict]] = None) -> List[dict]:
    """A minimal valid ``pm__`` preset-param list, mirroring the standard key set
    an HX Edit import emits (clip, 2x12 floorboard stomps, tempo, exp-switch,
    instrument impedance, xy-controller). Footswitch scribble-strip colour/label/
    topidx come from ``sources`` (spec 2 Part B) when supplied, else neutral."""
    scrib = _scribble_for(sources)
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
            cfg = scrib.get((row, n)) or {}
            color = cfg.get("fs_color", 1)
            if not isinstance(color, int) or isinstance(color, bool):
                color = 1  # "auto" / non-int -> default palette slot
            pm.append({"key_": f"{base}.color", "type": "i", "val_": color})
            pm.append({"key_": f"{base}.label", "type": "s",
                       "val_": str(cfg.get("fs_label", ""))})
            pm.append({"key_": f"{base}.topidx", "type": "i",
                       "val_": int(cfg.get("fs_topidx", 0))})
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
    """Synthesize a fresh ``_sbepgsm`` dict from a raw-less recipe.

    Reads every DSP path (dual-amp), synthesizes the ``sfg_`` (with any
    split/join routing), and builds the ``cg__`` snapshot machinery from the
    recipe's inline snapshot arrays (falling back to a blank-8 ``cg__`` when the
    tone has no snapshot variation)."""
    recipe = copy.deepcopy(recipe)
    paths = recipe.get("paths") or []
    # Normalize block coordinates so the instance-id map and the snapshot
    # lookups key off identical ``(path, lane, pos)`` tuples.
    for path in paths:
        for bi, spec in enumerate(path.get("blocks") or []):
            spec.setdefault("lane", 0)
            spec.setdefault("pos", bi)
    sfg, next_id, instance_ids = synthesize_sfg(paths)
    cg = _synth_cg_from_recipe(recipe, instance_ids, next_id - 1)
    pm = _synth_pm(recipe.get("sources"))
    return {"cg__": cg, "pm__": pm, "sfg_": sfg}


# --- authored .hsp -> device _sbepgsm bytes ----------------------------------

def _build_structural_block(entry: dict) -> dict:
    """Materialize a split/join routing block from a bridge structural descriptor
    (``{"kind", "model", "params"}``) into a full device block dict.

    ``id__``/``bblk``/``bflw`` are placeholders; :func:`synthesize_sfg` stamps the
    instance id and re-points the split<->join partners."""
    kind = entry.get("kind")
    scaffold = copy.deepcopy(_SPLIT_SCAFFOLD if kind == "split" else _JOIN_SCAFFOLD)
    mid = _resolve_model_id(entry.get("model", ""))
    if mid is not None:
        scaffold["mdls"][0]["id__"] = mid
        # Re-map helixgen split/join param names -> device + fill defaults.
        from . import bridge as _bridge
        params = entry.get("params") or {}
        dev_params = _bridge.map_params(mid, {
            k: v for k, v in params.items() if isinstance(v, (int, float, bool))
        })
        scaffold["mdls"][0]["parm"] = _synth_parm(mid, dev_params)
    return scaffold


def hsp_to_sbepgsm(hsp_body: dict, *, dsp: Optional[int] = None,
                   strict: bool = False) -> bytes:
    """Transcode a helixgen ``.hsp`` body into stored device content bytes.

    Reads EVERY DSP flow (dual-amp) via :func:`bridge.hsp_to_paths`: each flow's
    user blocks (device model + device param names + IR hash), its live-input
    routing, its split/join routing skeleton, and its per-snapshot bypass/param
    deltas. Builds a modeled recipe, synthesizes the ``_sbepgsm`` structure
    (:func:`recipe_to_sbepgsm`), and serializes with
    :func:`content.encode_content_data`. No template, no device.

    ``dsp`` is retained for back-compat: pass an int to transcode only that one
    DSP path (legacy serial behaviour); the default (``None``) reads all paths.
    """
    from . import bridge

    paths = bridge.hsp_to_paths(hsp_body, strict=strict)
    if dsp is not None:
        paths = paths[dsp:dsp + 1]
    for path in paths:
        if path.get("structural"):
            path["structural"] = [_build_structural_block(e)
                                  for e in path["structural"]]
    recipe: Dict[str, Any] = {"name": None, "paths": paths or [{"blocks": []}]}
    snaps = bridge.hsp_snapshot_meta(hsp_body)
    if snaps:
        recipe["snapshots"] = snaps
    sources = bridge.hsp_sources(hsp_body)
    if sources:
        recipe["sources"] = sources
    doc = recipe_to_sbepgsm(recipe)
    return content.encode_content_data(doc)
