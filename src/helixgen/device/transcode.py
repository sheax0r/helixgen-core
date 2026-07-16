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

from helixgen import flowparams

from . import content
from . import defs
from . import irmd as _irmd

# Block categories that are structural endpoints, not user effect blocks.
# These are carried verbatim in raw and never modeled into ``paths``.
_ENDPOINT_CATEGORIES = {"input", "output", "looper", "split", "join"}


def _category_for(model_id: Optional[int]) -> Optional[str]:
    return defs.category_for(model_id)


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
    pmeta = defs.model_params_for(model_id)
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
        irhash = _irmd.irmd_to_irhash(irmd)

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
        m0["irmd"] = _irmd.irhash_to_irmd(irhash)
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
# Derived from the vendored defs asset (resolver pattern, #14) rather than
# hardcoded, so a defs regeneration can never silently drift these ids.
_INPUT_MODEL = {
    "inst1": defs.model_id_for("P35_InputInst1"),
    "inst2": defs.model_id_for("P35_InputInst2"),
    "both": defs.model_id_for("P35_InputInst1_2"),  # stereo, both jacks
    "none": defs.model_id_for("P35_InputNone"),
}
# Fail LOUDLY at import if a defs regeneration ever drops/renames one of these
# models — a silent None here would flow a None model id into input synthesis.
assert all(isinstance(v, int) for v in _INPUT_MODEL.values()), (
    f"P35_Input* endpoint model missing from the defs asset: {_INPUT_MODEL}"
)
_INPUT_MODEL_INV = {v: k for k, v in _INPUT_MODEL.items()}


def _make_endpoint_model(model_id: int) -> dict:
    """Synthesize an endpoint's ``mdls[0]`` from ``defs`` defaults (for the
    input variants — inst2/both — we did not capture verbatim templates for)."""
    return {"cid_": 0, "enbl": 1, "id__": model_id, "lbid": -1,
            "parm": _synth_parm(model_id, {}), "snap": False, "tid_": 0, "vers": 0}


def _make_input_endpoint(mode: Optional[str], inst_id: int,
                         params: Optional[Dict[str, Any]] = None) -> dict:
    """Build the live-input endpoint block for a flow given its ``input`` mode.

    ``inst1``/``none`` reuse the verbatim captured templates; ``inst2``/``both``
    are synthesized from ``defs`` (valid model + default params). ``params``
    (device-name-keyed, e.g. ``Trim``/``noiseGate`` or the stereo model's
    ``Pad.1``/``Pad.2`` — from :func:`bridge._lift_endpoint_params`) overlays
    the model's defaults so the ``.hsp``'s pad/trim/gate state survives
    transcode (parity #18)."""
    if not params:
        if mode in (None, "inst1"):
            return _endpoint(_INPUT_INST1, inst_id)
        if mode == "none":
            return _endpoint(_INPUT_NONE, inst_id)
    model_id = _INPUT_MODEL.get(mode or "inst1", _INPUT_MODEL["inst1"])
    m0 = _make_endpoint_model(model_id)
    if params:
        m0["parm"] = _synth_parm(model_id, params)
    return {
        "cid_": 0, "enbl": 1, "favo": 0, "hasb": False,
        "hrns": _hrns_for("input"),
        "id__": inst_id,
        "mdls": [m0],
        "snap": False, "tid_": 0, "type": 8,
    }


def _make_output_matrix(inst_id: int,
                        params: Optional[Dict[str, Any]] = None) -> dict:
    """The row-0 ``OutputMatrix`` endpoint, with the ``.hsp``'s ``gain``/``pan``
    overlaid onto the captured template when provided (parity #18)."""
    ep = _endpoint(_OUTPUT_MATRIX, inst_id)
    if params:
        model_id = ep["mdls"][0]["id__"]  # 783 = P35_OutputMatrix
        ep["mdls"][0]["parm"] = _synth_parm(model_id, params)
    return ep


def _synth_parm(model_id: int, params: Dict[str, Any]) -> List[dict]:
    """Full parm list for ``model_id`` from ``defs``, in pid order.

    Values come from ``params`` (keyed by DEVICE param name) when present —
    preserving the caller's value type (bool/int/float), so lifted values
    round-trip exactly — else the model default from ``defs``.
    """
    mp = defs.model_params_for(model_id)
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
        m0["irmd"] = _irmd.irhash_to_irmd(irhash)
    # Block-level ``enbl`` is the BASE bypass (0 = the block loads bypassed);
    # the model instance's ``enbl`` stays 1 regardless (device-verified).
    return {
        "cid_": 0, "enbl": 0 if spec.get("enabled") is False else 1,
        "favo": 0, "hasb": False,
        "hrns": _hrns_for(category),
        "id__": inst_id,
        "mdls": [m0],
        "snap": False,
        "tid_": 0,
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

    Each block's ``id__`` is set to ``base + gridpos`` so the identity
    ``bmap = [base .. base+27]`` is self-consistent (``bmap[gridpos] == id at
    gridpos`` — the encoding real device presets use, verified against fixtures
    151/152). ``tid_`` is zeroed: on a real device blob a block's ``tid_`` is
    the id of its snapshot-tracked bypass TARGET (``cg__…trgs``), 0 otherwise —
    ``_synthesize`` binds tracked blocks after the ``cg__`` is built. (The old
    ``tid_ = id__`` scheme collided with real target ids once snapshot targets
    existed.) ``bcnt`` is the fixed grid size, NOT the block count. ``blks``
    alternates ``[gridpos, block, …]`` for occupied positions only.
    """
    blks: List[Any] = []
    for gp, blk in sorted(placements, key=lambda t: t[0]):
        blk["id__"] = base + gp
        blk["tid_"] = 0
        blks.append(gp)
        blks.append(blk)
    return {"bcnt": _GRID_SLOTS, "blks": blks,
            "bmap": [base + i for i in range(_GRID_SLOTS)],
            "cid_": 0, "enbl": 1, "snap": False, "tid_": 0}


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
    blk["tid_"] = 0
    return blk


def _append_output_group(placements: List[Tuple[int, dict]], out_params) -> None:
    """Append the row-0 OutputMatrix + row-1 InputNone/OutputNone endpoint
    group that terminates every synthesized flow (identical across the serial
    and both split placement strategies)."""
    placements.append((_ROW0_OUTPUT, _make_output_matrix(0, out_params)))
    placements.append((_ROW1_INPUT, _endpoint(_INPUT_NONE, 0)))
    placements.append((_ROW1_OUTPUT, _endpoint(_OUTPUT_NONE, 0)))


def _place_serial_flow(placements, instance_ids, pi, base, modeled) -> None:
    """SERIAL / dual-DSP flow: user blocks fill row 0 (positions 1..12).
    (Hardware-confirmed 2026-07-13.) Overflow blocks past row 0 are dropped."""
    gp = 1
    for bi, spec in enumerate(modeled):
        if gp > _ROW0_LAST_USER:
            break  # row 0 is full; overflow blocks are dropped
        lane = int(spec.get("lane", 0))
        pos = int(spec.get("pos", bi))
        placements.append((gp, _make_user_block(spec, 0)))
        instance_ids[(pi, lane, pos)] = base + gp
        gp += 1


def _place_split_flow_coords(placements, instance_ids, pi, base, modeled, structural) -> None:
    """SPLIT flow, faithful placement from .hsp grid coordinates (hardware-
    derived 2026-07-13): lane-0 blocks/split/join at gridpos == pos (row 0),
    lane-1 blocks at gridpos == 14 + pos (row 1). The split's branch pointer
    (bblk) is the first lane-1 grid slot; the join's is the row-1 slot beneath
    the join (14 + join.pos)."""
    for spec in modeled:
        lane = int(spec.get("lane", 0))
        pos = int(spec["pos"])
        gp = pos if lane == 0 else _ROW1_INPUT + pos
        placements.append((gp, _make_user_block(spec, 0)))
        instance_ids[(pi, lane, pos)] = base + gp
    lane1_gps = [_ROW1_INPUT + int(s["pos"]) for s in modeled
                 if int(s.get("lane", 0)) == 1]
    first_lane1_gp = min(lane1_gps) if lane1_gps else _ROW1_INPUT + 1
    for scaffold in structural:
        blk = {k: v for k, v in scaffold.items() if not k.startswith("_")}
        spos = int(scaffold["_pos"])
        if blk.get("type") == 3:      # split -> first lane-1 slot
            blk["bblk"], blk["bflw"] = base + first_lane1_gp, pi
        elif blk.get("type") == 4:    # join <- row-1 slot beneath it
            blk["bblk"], blk["bflw"] = base + _ROW1_INPUT + spos, pi
        placements.append((spos, blk))


def _place_split_flow_nocoords(placements, instance_ids, pi, base, modeled, structural) -> None:
    """SPLIT flow WITHOUT .hsp coordinates (round-trip of a device preset whose
    modeled blocks lost their grid pos): best-effort contiguous placement —
    lane-0 in row 0, lane-1 in row 1, split/join between."""
    lane0 = [s for s in modeled if int(s.get("lane", 0)) == 0]
    lane1 = [s for s in modeled if int(s.get("lane", 0)) == 1]
    gp = 1
    for bi, spec in enumerate(lane0):
        if gp > _ROW0_LAST_USER - 1:
            break
        placements.append((gp, _make_user_block(spec, 0)))
        instance_ids[(pi, 0, int(spec.get("pos", bi)))] = base + gp
        gp += 1
    split_gp, join_gp = gp, gp + 1
    gp1 = _ROW1_INPUT + 1
    for bi, spec in enumerate(lane1):
        if gp1 > _ROW1_LAST_USER:
            break
        placements.append((gp1, _make_user_block(spec, 0)))
        instance_ids[(pi, 1, int(spec.get("pos", bi)))] = base + gp1
        gp1 += 1
    for scaffold in structural:
        typ = scaffold.get("type")
        slot = split_gp if typ == 3 else join_gp
        blk = {k: v for k, v in scaffold.items() if not k.startswith("_")}
        if typ == 3:
            blk["bblk"], blk["bflw"] = base + (_ROW1_INPUT + 1), pi
        elif typ == 4:
            blk["bblk"], blk["bflw"] = base + join_gp, pi
        placements.append((slot, blk))


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
        in_params = path.get("input_params") or None
        out_params = path.get("output_params") or None

        # (gridpos, block) placements on this flow's 28-slot grid. The input
        # endpoint carries its own base bypass (#23: ``enbl=0`` loads bypassed)
        # and is registered in ``instance_ids`` under the ``(pi, -1, -1)``
        # sentinel so the snapshot synthesis can bind it as a bypass target
        # (the Stadium app snapshot-tracks the DSP input).
        input_block = _make_input_endpoint(mode, 0, in_params)
        if path.get("input_enabled") is False:
            input_block["enbl"] = 0
        instance_ids[(pi, -1, -1)] = base + _ROW0_INPUT
        # The row-0 OutputMatrix endpoint gets the ``(pi, -2, -2)`` sentinel:
        # its gain/pan can be snapshot-tracked param targets (#62 phase 2 —
        # per-snapshot output-level trims).
        instance_ids[(pi, -2, -2)] = base + _ROW0_OUTPUT
        placements: List[Tuple[int, dict]] = [(_ROW0_INPUT, input_block)]

        # Three mutually-exclusive placement strategies, each populating
        # ``placements`` + registering user-block ``instance_ids``; every flow
        # then terminates with the same OutputMatrix/InputNone/OutputNone group.
        if not structural:
            _place_serial_flow(placements, instance_ids, pi, base, modeled)
        elif all("_pos" in s for s in structural) and \
                all("pos" in s for s in modeled):
            _place_split_flow_coords(placements, instance_ids, pi, base,
                                     modeled, structural)
        else:
            _place_split_flow_nocoords(placements, instance_ids, pi, base,
                                       modeled, structural)
        _append_output_group(placements, out_params)

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
    if exsw is None:
        exsw = -1
    bpm = meta.get("bpm", meta.get("bpm_", meta.get("tempo", 120.0)))
    return name, exsw, float(bpm if bpm is not None else 120.0)


def _controller_locl_ctxt(source: Any) -> Optional[Tuple[int, int]]:
    """Map a ``.hsp`` controller source id -> device ``(locl, ctxt)``.

    Every mapping is anchored by pairing factory presets' ``.hsp`` exports with
    their live device content (non-activating ``GetContentData`` pulls,
    2026-07-14) plus the ``preset_151/152/157`` fixtures:

    - stomp bank A ``0x010101NN`` -> ``(25 + NN, 1)``
    - stomp bank B ``0x010102NN`` -> ``(25 + NN, 2)``
    - looper-function bank ``0x010104NN`` -> ``(25 + NN, 9)`` (Nash Sesh's 7
      looper controllers map NN 0,1,2,3,7,8,9 -> locl 25,26,27,28,32,33,34
      exactly)
    - expression toe switch ``0x01010500`` (EXP1Toe) -> ``(37, 0)``
      (Deconstructed Bliss's only (37,0) src is its wah-toe bypass; the old
      ``(42, 0)`` mapping collided with EXP1)
    - EXP pedals ``0x0102010M`` -> ``(42, M)`` for M in {0, 1} ONLY (EXP1 /
      EXP2). ``0x01020102`` (likely EXP3) has no anchored device encoding —
      it is skipped, NOT collapsed onto EXP2 (corpus-real: `Marshall and
      vh4` sweeps a wah from it while using real EXP2 elsewhere).

    Returns ``None`` for anything else so the caller can skip it."""
    if not isinstance(source, int) or isinstance(source, bool):
        return None
    if source == 0x01010500:            # EXP1Toe (wah toe switch)
        return (37, 0)
    hi = source & 0xFFFFFF00
    lo = source & 0xFF
    if hi == 0x01010100:                # stomp bank A footswitch (NN = FS#-1)
        return (25 + lo, 1)
    if hi == 0x01010200:                # stomp bank B footswitch
        return (25 + lo, 2)
    if hi == 0x01010400:                # looper-function switch bank
        return (25 + lo, 9)
    if hi == 0x01020100 and lo in (0, 1):   # EXP1 / EXP2 pedal
        return (42, lo)
    return None                              # EXP3 etc.: un-anchored, skip


# ``.hsp`` behavior string -> device ``ctrl.behv`` enum index. Anchored:
# latching=0 (pervasive), momentary=1 (Deconstructed Bliss's Transport ctrl),
# continuous=2 (every EXP sweep). "toedown"=3 presumed from the app binary's
# enum table order (0 corpus uses). ``togl`` is NOT the momentary flag — it
# varies freely on latching controllers across real device presets (volatile
# latch state with no ``.hsp`` counterpart) and is always synthesized False.
_BEHV_INDEX = {"latching": 0, "momentary": 1, "continuous": 2, "toedown": 3}


def _behv(behavior: Any, default: int) -> int:
    return _BEHV_INDEX.get(behavior, default)


def _curv(meta: dict) -> int:
    """Device ``ctrl.curv`` = 0-based index into the curve vocabulary
    (linear = 5; see ``controllers.curve_index``). An unknown curve string
    (future firmware vocabulary in a device-written ``.hsp``) falls back to
    linear rather than failing the whole transcode."""
    from ..controllers import ControllerError, curve_index
    curve = meta.get("curve")
    if not isinstance(curve, str):
        return 5  # linear
    try:
        return curve_index(curve)
    except ControllerError:
        return 5  # linear


def _thrs(meta: dict) -> float:
    thr = meta.get("threshold")
    if isinstance(thr, (int, float)) and not isinstance(thr, bool):
        return float(thr)
    return 0.0


def _make_src(src_id: int, locl: int, ctxt: int, byps: bool) -> dict:
    """A controller ``srcs`` entry (mirrors the fixture shape)."""
    return {"byps": byps, "cmds": [-1, -1], "cnt1": 0, "cnt2": 0, "cnt3": 0,
            "ctxt": ctxt, "id__": src_id, "locl": locl, "mtms": 0, "mtyp": 0,
            "type": 1}


# Command Center (#16). Switch source id -> device (locl, ctxt, srcs-type),
# anchored by the live Mandarin Fuzz (FS1 = locl 25, ctxt 1, type 1) + ZZCAP-CC
# (Instant 1 = locl 0, ctxt 0, type 4) content pulls, 2026-07-14.
def _command_locl_ctxt_type(source: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(source, int) or isinstance(source, bool):
        return None
    hi, lo = source & 0xFFFFFF00, source & 0xFF
    if hi == 0x01010100:            # footswitch bank A
        return (25 + lo, 1, 1)
    if hi == 0x04040100 and 0 <= lo <= 5:   # Instant 1..6
        return (lo, 0, 4)
    return None


# The ``.hsp`` native ``Command`` subtype value (corpus-anchored: PC=0, CC=1,
# MMC=2, Note=3 — see :data:`mutate._MIDI_SUBTYPE`) maps to the DEVICE
# footswitch/Instant ``func`` (HW capture 2026-07-15, findings §TARGET D):
# PC=0, CC=1, **Note=2, MMC=3** — Note and MMC are SWAPPED between the two
# encodings. Applies to both footswitch and Instant sources.
_HSP_TO_DEVICE_MIDI_FUNC = {0: 0, 1: 1, 2: 3, 3: 2}


def _command_payload(ctype: Any, func: int, params: dict, *,
                     ctxt: int) -> Optional[dict]:
    """The family-specific ``cg__`` ``cmnd`` payload — ``type`` + ``pvl*`` int
    slots + ``psp*`` bool slots — for a native ``preset.commands`` record.

    ``func`` is the ``.hsp`` native ``Command`` subtype value (PC=0/CC=1/MMC=2/
    Note=3); ``ctxt`` is the device source class (``1`` = footswitch, ``0`` =
    Instant) — the two use DIFFERENT MIDI slot layouts.

    Byte-exact HW anchors: PresetSnapshot (5 int + 5 bool, Mandarin Fuzz
    all-zero, 2026-07-14); Instant MIDI PC (12 int + 12 bool, ZZCAP-CC:
    ``[0, ch, msb, lsb, -1, 0,0,0, 100, 1, 0,0]``, 2026-07-14); and the
    **footswitch** CC / Note / MMC 12-slot layouts (2026-07-15, findings
    §TARGET D) — the footswitch reserves ``pvl1``=subtype and shifts data +1
    vs the Instant layout, and the emitted ``func`` uses the device
    Note/MMC-swapped enum. Continuous/EXP MIDI (a different 5-slot layout) is
    not authored (out of scope, #16 residual)."""
    def _pv(prefix: str, vals: list) -> dict:
        return {f"{prefix}{chr(ord('a') + i)}": v for i, v in enumerate(vals)}

    def _g(name: str, default: int = 0) -> int:
        v = params.get(name, default)
        return v if isinstance(v, int) and not isinstance(v, bool) else default

    if ctype == "PresetSnapshot":
        pvl = [_g("Action"), _g("Command"), _g("Preset"), _g("Setlist"),
               _g("Snapshot")]
        out = {"type": 1, "func": int(func)}
        out.update(_pv("pvl", pvl))
        out.update(_pv("psp", [False] * 5))
        return out
    if ctype == "MIDI":
        ch = _g("MIDI Ch", 1)
        dev_func = _HSP_TO_DEVICE_MIDI_FUNC.get(int(func), int(func))
        if ctxt == 1:
            # Footswitch 12-slot layout (HW capture 2026-07-15): pvl0=PC program
            # (Bank/Program subtype), pvl1=subtype, pvl2=channel, pvl3/4=Bank
            # MSB/LSB, pvl5=reserved(-1), pvl6/7=CC#/value, pvl8/9=note/velocity
            # (9 defaults 100 for non-Note), pvl10=const 1, pvl11=MMC message.
            if func == 0:      # PC / Bank
                pvl = [_g("PC"), 0, ch, _g("MSB", -1), _g("LSB", -1),
                       -1, 0, 0, 0, 100, 1, 0]
            elif func == 1:    # CC
                pvl = [0, 1, ch, -1, -1, -1, _g("CC#"), _g("Value"),
                       0, 100, 1, 0]
            elif func == 3:    # Note (.hsp Command 3 -> device func 2)
                pvl = [0, 2, ch, -1, -1, -1, 0, 0, _g("Note"),
                       _g("Velocity", 100), 1, 0]
            elif func == 2:    # MMC (.hsp Command 2 -> device func 3)
                pvl = [0, 3, ch, -1, -1, -1, 0, 0, 0, 100, 1, _g("Message")]
            else:
                # Out-of-range ``Command`` (a hand-edited .hsp reaching
                # install/sync): drop with a warning, per project convention.
                import sys
                print(f"warning: unknown MIDI Command subtype {func!r} on a "
                      f"footswitch command; command dropped.", file=sys.stderr)
                return None
        else:
            # Instant layout (ch@pvl1, NO subtype slot) — HW-anchored for PC
            # (ZZCAP-CC). Note/MMC slot placement here is uncaptured (#16
            # residual); the emitted ``func`` uses the device enum (Note/MMC
            # swap) by ASSUMPTION — the func enum is treated as a property of
            # the cmnd record schema, not the source class; no Instant
            # Note/MMC capture exists yet (user-gated). An out-of-range
            # ``Command`` here stays best-effort (base pvl defaults emitted,
            # unmapped func passed through) — the Instant layout has no
            # subtype slot to corrupt, so the record degrades gracefully
            # instead of dropping.
            pvl = [0, ch, _g("MSB"), _g("LSB"), -1, 0, 0, 0, 100, 1, 0, 0]
            if func == 0:      # PC
                pvl[0] = _g("PC")
            elif func == 1:    # CC
                pvl[5] = _g("CC#")
                pvl[6] = _g("Value")
            elif func == 3:    # Note
                pvl[8] = _g("Velocity", 100)
                pvl[10] = _g("Note")
                pvl[11] = _g("NoteOff")
            elif func == 2:    # MMC
                pvl[7] = _g("Message")
        out = {"type": 6, "func": dev_func}
        out.update(_pv("pvl", pvl))
        out.update(_pv("psp", [False] * 12))
        return out
    return None


def _synth_commands(recipe, srcs, trgs, next_trg, instance_ids):
    """Synthesize the Command Center records (#16) for a recipe.

    Each command is an ENTITY (its ``cmnd.cid_`` == its trg ``eID_``, ``enty``
    6, ``type`` 4). Authored NATIVELY in ``preset.commands``; here we build the
    ``cg__`` srcs->cmnd->trgs relational records. Command ``srcs`` are APPENDED
    after the controller srcs — helixgen rejects a switch shared by a footswitch
    controller AND a command, so the two never contend for one srcs entry, and
    ``sm__.scid`` (controllers only) is untouched.

    Mutates ``srcs`` and ``trgs`` in place (appends command sources/targets) and
    returns ``(cmnd, next_cmd_entity, next_trg)``.
    """
    cmnd: List[dict] = []
    next_cmd_entity = max(instance_ids.values(), default=0) + 1
    cmd_src_index: Dict[int, int] = {}       # source id -> srcs id
    cmd_src_cmds: Dict[int, List[int]] = {}   # srcs id -> [command entity ids]
    for cmd in (recipe.get("commands") or []):
        lct = _command_locl_ctxt_type(cmd.get("source"))
        if lct is None:
            continue
        locl, ctxt, srtype = lct
        payload = _command_payload(cmd.get("type"), cmd.get("func", 0),
                                   cmd.get("params") or {}, ctxt=ctxt)
        if payload is None:
            continue
        entity = next_cmd_entity
        next_cmd_entity += 1
        tid = next_trg
        next_trg += 1
        trgs.append({"eID_": entity, "enty": 6, "id__": tid, "pid_": 0,
                     "slot": 0, "type": 4})
        src = cmd["source"]
        sid = cmd_src_index.get(src)
        if sid is None:
            sid = len(srcs) + 1
            cmd_src_index[src] = sid
            srcs.append({"byps": False, "cmds": [-1, -1], "cnt1": 0, "cnt2": 0,
                         "cnt3": 0, "ctxt": ctxt, "id__": sid, "locl": locl,
                         "mtms": 0, "mtyp": 0, "type": srtype})
            cmd_src_cmds[sid] = []
        cmd_src_cmds[sid].append(entity)
        rec = {"behv": _behv(cmd.get("behavior"), 0), "cid_": entity, "curv": 5,
               "dlay": 0, "goid": 0, "thrs": 0.0, "tid_": tid,
               "togl": bool(cmd.get("toggle", False)), "trig": sid}
        rec.update(payload)
        cmnd.append(rec)
    for sid, cids in cmd_src_cmds.items():
        srcs[sid - 1]["cmds"] = cids + [-1] * max(0, 2 - len(cids))
    return cmnd, next_cmd_entity, next_trg


def _emit_snapshots(tracked, snap_meta):
    """Build the 8 ``snps`` dicts. Each snapshot's ``tamv`` is the flat
    ``[trg_id, value, …]`` over every snapshot-tracked target (an unset trailing
    value reuses the last), plus its name/exsw/bpm metadata."""
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
    return snps


def _synth_cg_from_recipe(
    recipe: dict,
    instance_ids: Dict[Tuple[int, int, int], int],
    max_id: int,
) -> Tuple[dict, Dict[str, dict]]:
    """Build the device ``cg__`` snapshot machinery from a recipe's inline
    snapshot arrays (snapshots spec Part A).

    Each user block may carry ``snap_bypass`` (per-snapshot bool list, DEVICE
    polarity: ``True`` = bypassed in that snapshot) and ``snap_params``
    (``{device_param_name: per-snapshot value list}``). For every block/param
    that ACTUALLY VARIES across snapshots we emit one ``trgs`` target
    (type1/enty2/pid0 bypass, or type2/enty3/pidN param), keyed by that block's
    device instance id (``eID_``, from ``instance_ids``). Each snapshot's
    ``tamv`` is the flat ``[trg_id, value, …]`` over every tracked target,
    ``ctm_.stid`` lists them, and ``ctm_.ptid`` packs the param targets
    (``(eID_<<16 | pid_) -> trg_id``).

    Returns ``(cg__, bindings)`` where ``bindings`` maps the snapshot-tracked
    entities back to their target ids — ``{"bypass": {eID_: trg_id},
    "param": {(eID_, pid_): trg_id}}`` — so the caller can stamp
    ``snap=True, tid_=<trg id>`` onto the tracked block dicts / parm leaves
    (the device applies snapshot values through that binding; controller-only
    targets are NOT bound). A tone with no snapshot variation falls back to
    the blank-8 ``cg__``.
    """
    snap_meta = recipe.get("snapshots") or []
    trgs: List[dict] = []
    stid: List[int] = []
    ptid: List[int] = []
    tracked: List[Tuple[int, List[Any]]] = []  # (trg_id, per-snapshot values)
    trg_index: Dict[Tuple[int, int, int], int] = {}  # (eID_, pid_, type) -> trg id
    # ``bypass``/``param`` hold snapshot-tracked targets (bound with
    # ``snap=True``); ``param_ctl`` holds controller-ONLY param targets (#24:
    # bound with ``snap=False`` — a controller-driven param leaf still carries
    # its ``tid_`` on real device blobs, matching the ``preset_15x`` fixtures).
    bindings: Dict[str, dict] = {"bypass": {}, "param": {}, "param_ctl": {}}
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
                bindings["bypass"][eid] = tid
            for pname, pvals in (spec.get("snap_params") or {}).items():
                if not (isinstance(pvals, list) and len({repr(x) for x in pvals}) > 1):
                    continue
                pid = defs.param_id_for(mid, pname)
                if pid is None:
                    continue
                tid = _new_trg({"eID_": eid, "enty": 3, "mmid": mid, "pid_": pid,
                                "pmid": mid, "ppid": pid, "slot": 0, "type": 2},
                               (eid, pid, 2))
                stid.append(tid)
                ptid.extend([(eid << 16) | pid, tid])
                tracked.append((tid, list(pvals)))
                bindings["param"][(eid, pid)] = tid

    # 1b) Input-endpoint snapshot bypass (#23). The DSP input is a bypass
    #     target too — its instance id is stashed under the ``(pi, -1, -1)``
    #     sentinel by :func:`synthesize_sfg`. Emit a bypass trg (device
    #     polarity: ``True`` = muted) whenever the per-snapshot array varies.
    for pi, path in enumerate(recipe.get("paths") or []):
        ibypass = path.get("input_snap_bypass")
        if not (isinstance(ibypass, list) and len({bool(x) for x in ibypass}) > 1):
            continue
        eid = instance_ids.get((pi, -1, -1))
        if eid is None:
            continue
        tid = _new_trg({"eID_": eid, "enty": 2, "pid_": 0, "slot": 0,
                        "type": 1}, (eid, 0, 1))
        stid.append(tid)
        tracked.append((tid, [bool(x) for x in ibypass]))
        bindings["bypass"][eid] = tid

    # 1c) Output-endpoint snapshot params (#62 phase 2). Per-snapshot
    #     output-level trims ride ``output_snap_params`` (lifted by
    #     ``bridge.hsp_to_paths`` from the b13 gain/pan ``snapshots``
    #     arrays); the OutputMatrix instance id is stashed under the
    #     ``(pi, -2, -2)`` sentinel. Emit a param trg per varying array —
    #     the values are raw device units (dB for ``gain``), exactly like a
    #     user-block ``snap_params`` row.
    out_mid = _OUTPUT_MATRIX["mdls"][0]["id__"]  # 783 = P35_OutputMatrix
    for pi, path in enumerate(recipe.get("paths") or []):
        osnap = path.get("output_snap_params")
        if not isinstance(osnap, dict):
            continue
        eid = instance_ids.get((pi, -2, -2))
        if eid is None:
            continue
        for pname, pvals in osnap.items():
            if not (isinstance(pvals, list) and len({repr(x) for x in pvals}) > 1):
                continue
            pid = defs.param_id_for(out_mid, pname)
            if pid is None:
                continue
            tid = _new_trg({"eID_": eid, "enty": 3, "mmid": out_mid,
                            "pid_": pid, "pmid": out_mid, "ppid": pid,
                            "slot": 0, "type": 2}, (eid, pid, 2))
            stid.append(tid)
            ptid.extend([(eid << 16) | pid, tid])
            tracked.append((tid, list(pvals)))
            bindings["param"][(eid, pid)] = tid

    # 2) Controller graph (Part B): source->bypass + source->param (EXP sweeps
    #    and footswitch param toggles). One physical source gets ONE ``srcs``
    #    entry no matter how many controllers it drives (a merge switch);
    #    ``sm__.scid`` maps the source to the LIST of its ctrl ids — the exact
    #    shape real device presets carry (fixtures: ``1, [1, 3]``). A snapshot
    #    trg is reused when the same target is also controller-driven.
    srcs: List[dict] = []
    ctrl: List[dict] = []
    src_index: Dict[Tuple[int, int], int] = {}  # (locl, ctxt) -> src id
    src_cids: Dict[int, List[int]] = {}         # src id -> [ctrl ids]
    next_ctrl = 1       # 1-based (0 == null on the device)
    hsp_sources = recipe.get("sources") or {}

    src_explicit: set = set()  # src ids whose byps came from an explicit .hsp flag

    def _src_for(source: Any, *, drives_bypass: bool) -> Optional[int]:
        lc = _controller_locl_ctxt(source)
        if lc is None:
            return None
        sid = src_index.get(lc)
        if sid is None:
            sid = len(srcs) + 1  # 1-based
            # ``byps`` mirrors the ``.hsp`` ``preset.sources[sid].bypass``
            # flag when present (paired evidence: Stadium Rock Rig's sources
            # flags match its device srcs exactly; the flag is functionally
            # inert either way — factory presets toggle fine with both values,
            # e.g. 2 Guitar Rig's working FS bypasses carry byps=False); else
            # the historical default (bypass-driving sources True, param
            # sources False).
            cfg = hsp_sources.get(source)
            byps = cfg.get("bypass") if isinstance(cfg, dict) else None
            if isinstance(byps, bool):
                src_explicit.add(sid)
            else:
                byps = drives_bypass
            srcs.append(_make_src(sid, lc[0], lc[1], byps=byps))
            src_index[lc] = sid
        elif drives_bypass and sid not in src_explicit:
            # A merged source created by a param controller later gains a
            # bypass target: upgrade the default so the result is
            # order-independent (an explicit .hsp flag still wins).
            srcs[sid - 1]["byps"] = True
        return sid

    def _new_ctrl(entry: dict, sid: int) -> None:
        nonlocal next_ctrl
        entry["cid_"] = next_ctrl
        next_ctrl += 1
        entry["trig"] = sid
        ctrl.append(entry)
        src_cids.setdefault(sid, []).append(entry["cid_"])

    def _new_midi_ctrl(entry: dict) -> None:
        """A MIDI CC controller (#33). Unlike FS/EXP it has NO physical ``srcs``
        entry — the source is the incoming CC, carried inline as ``cnt2`` (CC#)
        + ``midi`` (packed CC on the device's global base channel, ``0xB0``
        status). ``trig`` is 0 (no source slot); not added to ``sm__.scid``.
        Fields follow the parity-capture findings §6; the remaining ctrl fields
        mirror the uniform ctrl schema so the device parser reads it."""
        nonlocal next_ctrl
        entry["cid_"] = next_ctrl
        next_ctrl += 1
        entry["trig"] = 0
        ctrl.append(entry)

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
                sid = _src_for(fsb.get("source"), drives_bypass=True)
                if sid is not None:
                    tid = trg_index.get((eid, 0, 1))
                    if tid is None:
                        tid = _new_trg({"eID_": eid, "enty": 2, "mmid": mid,
                                        "pid_": 0, "slot": 0, "type": 1},
                                       (eid, 0, 1))
                    _new_ctrl({"behv": _behv(fsb.get("behavior"), 0),
                               "curv": _curv(fsb), "dlay": 0, "goid": 0,
                               "max_": True, "min_": False,
                               "thrs": _thrs(fsb), "tid_": tid,
                               "togl": False, "type": 1}, sid)
            # ``ctl_params``: EXP sweeps AND footswitch param toggles (the
            # behavior string tells them apart; min/max are raw param units
            # either way). ``exp_params`` is the pre-#21 spelling, still read.
            params_ctl = spec.get("ctl_params") or spec.get("exp_params") or {}
            for pname, meta in params_ctl.items():
                pid = defs.param_id_for(mid, pname)
                if pid is None:
                    continue
                behavior = meta.get("behavior", "continuous")
                sid = _src_for(meta.get("source"), drives_bypass=False)
                if sid is None:
                    continue
                tid = trg_index.get((eid, pid, 2))
                if tid is None:
                    tid = _new_trg({"eID_": eid, "enty": 3, "mmid": mid,
                                    "pid_": pid, "pmid": mid, "ppid": pid,
                                    "slot": 0, "type": 2}, (eid, pid, 2))
                    ptid.extend([(eid << 16) | pid, tid])
                    # #24: a controller-ONLY param target still binds its leaf
                    # (``tid_``) — but with ``snap=False`` (it is not snapshot-
                    # tracked). A param that is ALSO snapshot-tracked already
                    # sits in ``bindings["param"]`` (snap=True), which wins.
                    bindings["param_ctl"][(eid, pid)] = tid
                _new_ctrl({"behv": _behv(behavior, 2), "curv": _curv(meta),
                           "dlay": 0, "goid": 0,
                           "max_": meta.get("max", 1.0),
                           "min_": meta.get("min", 0.0),
                           "thrs": _thrs(meta), "tid_": tid,
                           "togl": False, "type": 3}, sid)

            # MIDI CC controllers (#33). ``midi_bypass`` = a CC toggling this
            # block's bypass; ``midi_params`` = {device_param: {cc,min,max}}
            # sweeps. Both reuse the SAME target ids as snapshot/FS/EXP bindings
            # (a bypass or param can be driven by a MIDI CC AND a footswitch).
            midi_byp = spec.get("midi_bypass")
            if isinstance(midi_byp, dict):
                cc = midi_byp.get("cc")
                if isinstance(cc, int) and not isinstance(cc, bool):
                    tid = trg_index.get((eid, 0, 1))
                    if tid is None:
                        tid = _new_trg({"eID_": eid, "enty": 2, "mmid": mid,
                                        "pid_": 0, "slot": 0, "type": 1},
                                       (eid, 0, 1))
                    _new_midi_ctrl({"behv": 0, "cnt2": cc, "curv": 5,
                                    "dlay": 0, "goid": 0, "max_": True,
                                    "midi": 0xB000 | cc, "min_": False,
                                    "thrs": 0.0, "tid_": tid, "togl": False,
                                    "type": 1})
            for pname, meta in (spec.get("midi_params") or {}).items():
                cc = meta.get("cc")
                if not (isinstance(cc, int) and not isinstance(cc, bool)):
                    continue
                pid = defs.param_id_for(mid, pname)
                if pid is None:
                    continue
                tid = trg_index.get((eid, pid, 2))
                if tid is None:
                    tid = _new_trg({"eID_": eid, "enty": 3, "mmid": mid,
                                    "pid_": pid, "pmid": mid, "ppid": pid,
                                    "slot": 0, "type": 2}, (eid, pid, 2))
                    ptid.extend([(eid << 16) | pid, tid])
                    bindings["param_ctl"][(eid, pid)] = tid
                _new_midi_ctrl({"behv": 2, "cnt2": cc, "curv": 5, "dlay": 0,
                                "goid": 0, "max_": meta.get("max", 1.0),
                                "midi": 0xB000 | cc, "min_": meta.get("min", 0.0),
                                "thrs": 0.0, "tid_": tid, "togl": False,
                                "type": 3})

    scid: List[Any] = []
    for sid in sorted(src_cids):
        scid.extend([sid, src_cids[sid]])

    # 3) Command Center commands (#16) — appends command srcs/trgs in place.
    cmnd, next_cmd_entity, next_trg = _synth_commands(
        recipe, srcs, trgs, next_trg, instance_ids)

    if not tracked and not ctrl and not cmnd:
        return _synth_cg(max_id), bindings

    snps = _emit_snapshots(tracked, snap_meta)

    return {
        "asnp": 0,
        "entt": {
            "cmnd": cmnd,
            "ctm_": {"htid": [], "ptid": ptid, "sirt": [], "stid": stid},
            "ctrl": ctrl,
            "sm__": {"scid": scid, "ssi_": []},
            "snps": snps,
            "srcs": srcs,
            "trgs": trgs,
        },
        "nxtc": next_ctrl,      # next-free controller id (not tied to block ids)
        "nxti": 0,
        # Command entities extend the entity space; a command-free preset keeps
        # the historical nxtm=1 (the 2.21.1-blessed snapshot/controller output)
        # so this change is a no-op for every non-command tone.
        "nxtm": next_cmd_entity if cmnd else 1,
        "nxts": len(srcs) + 1,  # next-free source id (incl. command srcs)
        "nxtt": next_trg,       # next-free target id (incl. command trgs)
    }, bindings


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


def _synth_pm(sources: Optional[Dict[int, dict]] = None,
              inst_z: Optional[Dict[str, str]] = None) -> List[dict]:
    """A minimal valid ``pm__`` preset-param list, mirroring the standard key set
    an HX Edit import emits (clip, 2x12 floorboard stomps, tempo, exp-switch,
    instrument impedance, xy-controller). Footswitch scribble-strip colour/label/
    topidx come from ``sources`` (spec 2 Part B) when supplied, else neutral.
    ``inst_z`` maps ``inst1``/``inst2`` to `.hsp` impedance strings; the device
    ``preset.instN.z`` int is the self-described enum index
    (``flowparams.impedance_device_int``), defaulting to First Enabled (1)."""
    scrib = _scribble_for(sources)
    pm: List[dict] = [
        {"key_": "preset.clip.end", "type": "f", "val_": 0.0},
        {"key_": "preset.clip.filename", "type": "s", "val_": ""},
        {"key_": "preset.clip.path", "type": "s", "val_": ""},
        {"key_": "preset.clip.start", "type": "f", "val_": 0.0},
        {"key_": "preset.expsw.active", "type": "i", "val_": 1},
    ]
    from ..controllers import ControllerError, FS_LABEL_MAX, color_int
    for row in ("a", "b"):
        for n in range(1, 13):
            base = f"preset.floorboard.stomp.{row}.{n}"
            cfg = scrib.get((row, n)) or {}
            color = cfg.get("fs_color", 1)
            if isinstance(color, str):
                # .hsp color name -> device palette int (anchored by live
                # pulls pairing factory exports with device content: auto=1,
                # red=2, dkorange=3, ltorange=4, purple=9, white=11).
                try:
                    color = color_int(color)
                except ControllerError:
                    color = 1  # unknown name -> "auto" palette slot
            if not isinstance(color, int) or isinstance(color, bool):
                color = 1  # unknown -> "auto" palette slot
            # The device stores at most 12 scribble chars (a 13-char .hsp
            # label was observed truncated on the hardware).
            label = str(cfg.get("fs_label", ""))[:FS_LABEL_MAX]
            pm.append({"key_": f"{base}.color", "type": "i", "val_": color})
            pm.append({"key_": f"{base}.label", "type": "s", "val_": label})
            pm.append({"key_": f"{base}.topidx", "type": "i",
                       "val_": int(cfg.get("fs_topidx", 0))})
    def _z(jack: str) -> int:
        z = (inst_z or {}).get(jack)
        return flowparams.impedance_device_int(z) if isinstance(z, str) else 1

    pm += [
        {"key_": "preset.inst1.z", "type": "i", "val_": _z("inst1")},
        {"key_": "preset.inst2.z", "type": "i", "val_": _z("inst2")},
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
    cg, bindings = _synth_cg_from_recipe(recipe, instance_ids, next_id - 1)
    _bind_snapshot_targets(sfg, bindings)
    pm = _synth_pm(recipe.get("sources"), recipe.get("inst_z"))
    return {"cg__": cg, "pm__": pm, "sfg_": sfg}


def _bind_snapshot_targets(sfg: dict, bindings: Dict[str, dict]) -> None:
    """Stamp every target-bound entity with its binding, in place.

    On a real device blob a snapshot-tracked block carries ``snap=True,
    tid_=<bypass trg id>`` at BLOCK level, and a snapshot-tracked param carries
    ``snap=True, tid_=<param trg id>`` on its parm leaf — that binding is how
    the device applies ``tamv`` values on a snapshot switch (verified against
    the Stadium app's own import of the same tone, 2026-07-13). A
    controller-ONLY param leaf (EXP sweep / FS param toggle) instead carries
    ``snap=False, tid_=<param trg id>`` (#24 — matching the ``preset_15x``
    fixtures). Untracked entities (including controller-only *bypass* targets)
    stay ``snap=False, tid_=0``. Input endpoints CAN be bypass targets (#23),
    so they are no longer skipped.
    """
    by_bypass = bindings.get("bypass") or {}
    by_param = bindings.get("param") or {}
    by_param_ctl = bindings.get("param_ctl") or {}
    if not by_bypass and not by_param and not by_param_ctl:
        return
    for flow in sfg.get("flow", []):
        for item in flow.get("blks", []):
            if not isinstance(item, dict):
                continue
            eid = item.get("id__")
            tid = by_bypass.get(eid)
            if tid:
                item["snap"] = True
                item["tid_"] = tid
            # Only the primary model slot: a future second slot (dual-cab)
            # could share a pid with the tracked param and must not be stamped.
            for mdl in (item.get("mdls") or [])[:1]:
                for leaf in mdl.get("parm") or []:
                    pid = leaf.get("pid_")
                    ptid = by_param.get((eid, pid))
                    if ptid:
                        leaf["snap"] = True
                        leaf["tid_"] = ptid
                        continue
                    cptid = by_param_ctl.get((eid, pid))
                    if cptid:
                        leaf["snap"] = False
                        leaf["tid_"] = cptid


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
            built = []
            for e in path["structural"]:
                blk = _build_structural_block(e)
                # Carry the .hsp grid coordinate so synthesize_sfg can place the
                # split/join faithfully (private keys, stripped before emit).
                if e.get("pos") is not None:
                    blk["_pos"] = int(e["pos"])
                    blk["_lane"] = int(e.get("lane", 0))
                built.append(blk)
            path["structural"] = built
    recipe: Dict[str, Any] = {"name": None, "paths": paths or [{"blocks": []}]}
    snaps = bridge.hsp_snapshot_meta(hsp_body)
    if snaps:
        recipe["snapshots"] = snaps
    sources = bridge.hsp_sources(hsp_body)
    if sources:
        recipe["sources"] = sources
    commands = bridge.hsp_commands(hsp_body)
    if commands:
        recipe["commands"] = commands
    preset_params = (hsp_body.get("preset") or {}).get("params") or {}
    inst_z = {jack: preset_params[f"{jack}Z"]
              for jack in ("inst1", "inst2")
              if isinstance(preset_params.get(f"{jack}Z"), str)}
    if inst_z:
        recipe["inst_z"] = inst_z
    doc = recipe_to_sbepgsm(recipe)
    return content.encode_content_data(doc)
