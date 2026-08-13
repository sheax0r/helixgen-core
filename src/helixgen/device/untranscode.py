"""Device ``_sbepgsm`` content -> helixgen ``.hsp`` (the reverse transcoder).

:mod:`helixgen.device.transcode` goes one way: an authored ``.hsp`` becomes
device content. This module goes the other way, so a preset authored on the
hardware or in HX Edit can be pulled off the device and then viewed, patched,
diffed and re-installed like any helixgen tone.

**The forward direction is the spec.** Every mapping here is the inverse of a
specific piece of ``transcode``/``bridge``:

===========================  =============================================
device                       ``.hsp``
===========================  =============================================
``sfg_.flow[i]``             ``preset.flow[i]``
grid position ``gp``         the ``bNN`` key (``bridge._lane_pos``: lane 1
                             lives at ``14 + pos``)
``mdls[0].id__``             ``slot[0].model`` (``modelmap`` reversed)
``parm[].pid_``              ``slot[0].params`` key (``defs`` pid -> device
                             name -> helixgen name, inverting
                             ``bridge.param_name_map``)
``mdls[0].irmd``             ``slot[0].irhash``
block ``enbl``               ``@enabled.value``
``cg__`` trgs + ``snps``     ``@enabled.snapshots`` / param ``snapshots``
``cg__`` ctrl + ``srcs``     ``@enabled.controller`` / param ``controller``
``pm__``                     ``preset.params`` / ``preset.sources`` / clip
===========================  =============================================

That inversion makes ``.sbe -> .hsp -> .sbe`` a free test oracle: for content
whose block instance ids follow the device's canonical ``id__ == 28*flow + gp``
scheme the round-trip is byte-exact. Presets reordered ON the hardware carry a
permuted ``bmap`` with stable-but-shuffled ids, which the forward transcoder
renumbers canonically — an identity-only, sonically null diff. See
``docs/superpowers/specs/2026-08-12-sbe-to-hsp-reverse-transcoder-design.md``
for the full residual-diff list.

Pure functions, no device, no network. Stadium-only.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from .. import controllers as _controllers
from .. import flowparams
from ..library import Library
from . import defs
from . import irmd as _irmd
from . import modelmap
from .transcode import _GRID_SLOTS, _ROW0_INPUT, _ROW0_OUTPUT, _ROW1_INPUT, _ROW1_OUTPUT

# ``.hsp`` block ``type`` string per device model category. The ``.hsp`` side
# has a coarser vocabulary than ``defs``: everything that is not an endpoint,
# an amp, a cab or a routing node is simply "fx" (verified against the real
# export corpus, whose only type strings are input/output/amp/cab/split/join/fx).
_HSP_TYPE = {
    "input": "input",
    "output": "output",
    "split": "split",
    "join": "join",
    "looper": "looper",
    "amp": "amp",
    "preamp": "amp",
    "cab": "cab",
    "cab_ir_interp": "cab",
    "ir": "cab",
}
_DEFAULT_HSP_TYPE = "fx"

# Endpoint models the forward transcoder synthesizes unconditionally for every
# flow (``transcode._append_output_group``). A real ``.hsp`` export omits the
# row-1 pair entirely, so emitting them would be noise that the forward path
# discards anyway — drop them, but ONLY when they are the None models. A row-1
# slot carrying anything else is real routing and is emitted.
_INPUT_NONE_MODEL = defs.model_id_for("P35_InputNone")
_OUTPUT_NONE_MODEL = defs.model_id_for("P35_OutputNone")

# ``meta.device_id`` / ``meta.device_version`` for a Stadium export. The
# device blob carries neither; ``device_id`` is load-bearing (``bridge`` and
# ``controllers`` resolve the input-model and controller-source tables from
# it), so it must be the real Stadium value rather than a placeholder.
STADIUM_DEVICE_ID = 2490368
STADIUM_DEVICE_VERSION = 318833973

# Device ``ctrl.behv`` enum index -> ``.hsp`` behavior string. Inverse of
# ``transcode._BEHV_INDEX``.
_BEHAVIOR = {0: "latching", 1: "momentary", 2: "continuous", 3: "toedown"}

# Device ``srcs`` (locl, ctxt) -> ``.hsp`` controller source id. Inverse of
# ``transcode._controller_locl_ctxt``; see that function for the anchoring
# evidence behind each row.
def _source_id(locl: Any, ctxt: Any) -> Optional[int]:
    if not isinstance(locl, int) or not isinstance(ctxt, int):
        return None
    if (locl, ctxt) == (37, 0):          # EXP1Toe (wah toe switch)
        return 0x01010500
    if ctxt == 1 and 25 <= locl <= 36:   # stomp bank A footswitch
        return 0x01010100 | (locl - 25)
    if ctxt == 2 and 25 <= locl <= 36:   # stomp bank B footswitch
        return 0x01010200 | (locl - 25)
    if ctxt == 9 and 25 <= locl <= 36:   # looper-function switch bank
        return 0x01010400 | (locl - 25)
    if locl == 42 and ctxt in (0, 1):    # EXP1 / EXP2 pedal
        return 0x01020100 | ctxt
    return None


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# --- vocabulary inverses -----------------------------------------------------

def _reverse_modelmap() -> Dict[int, str]:
    """``device numeric model id -> helixgen model-id string``.

    The vendored map is injective today (330 helixgen ids -> 330 distinct
    device ids); should a regeneration ever collide, the lexicographically
    first helixgen id wins so the result stays deterministic.
    """
    out: Dict[int, str] = {}
    for hg, dev in modelmap.load_modelmap().get("map", {}).items():
        if dev not in out or hg < out[dev]:
            out[dev] = hg
    return out


def _hsp_model(dev_id: Any, rev: Dict[int, str]) -> Optional[str]:
    """Device numeric model id -> the ``.hsp`` model string.

    Inverse of ``bridge._default_resolve_model``: prefer the reconciled
    modelmap, fall back to the device's own name (which ``defs.model_id_for``
    resolves straight back, so the round-trip holds either way).
    """
    if not isinstance(dev_id, int):
        return None
    return rev.get(dev_id) or defs.model_name_for(dev_id)


def _param_plan(dev_id: int, hsp_model: Optional[str],
                library: Optional[Library]) -> List[Tuple[int, str]]:
    """``[(pid, .hsp param name), ...]`` for one model, **in ``.hsp`` order**.

    Inverse of ``bridge.param_name_map``, which maps the ``.hsp``'s param names
    onto the device's by normalized-name match and then positionally. Feeding
    it the library block's param names (the same names, in the same order, that
    a real ``.hsp`` export carries) and inverting the result reproduces the
    forward mapping exactly.

    Order is load-bearing, not cosmetic: the forward transcoder walks a block's
    snapshot/controller params in ``.hsp`` dict order and allocates ``cg__``
    target ids as it goes, so emitting params in device-pid order instead of
    library order shuffles every downstream target id (corpus-observed on
    ``HD2_AmpBritJ45Brt``, whose library order visits pids 6, 1, 7, 3, 5).

    With no library block for the model the device's own param names and order
    are used; they normalize onto themselves, so the transcode round-trip still
    holds — only the human-facing names in ``view`` / ``set-param`` differ.
    """
    dev_params = defs.model_params_for(dev_id)
    pid_of = {name: meta.get("id") for name, meta in dev_params.items()}
    fallback = [(pid, name) for name, pid in pid_of.items() if pid is not None]
    if library is None or not hsp_model:
        return fallback
    try:
        block = library.load_block(hsp_model)
    except KeyError:
        return fallback
    from .bridge import param_name_map

    hsp_names = list((block.params or {}).keys())
    forward = param_name_map(dev_id, hsp_names)
    plan = [(pid_of.get(forward[n]), n) for n in hsp_names if n in forward]
    plan = [(pid, n) for pid, n in plan if pid is not None]
    # Any device param the library block does not name still has to reach the
    # ``.hsp`` — dropping it would silently lose the value on re-install.
    named = {pid for pid, _ in plan}
    plan += [(pid, name) for pid, name in fallback if pid not in named]
    return plan


def _curve_name(curv: Any) -> Optional[str]:
    """Device ``ctrl.curv`` index -> curve name; ``None`` for plain linear (the
    ``.hsp`` omits the field then, matching ``bridge._ctl_meta``)."""
    if not isinstance(curv, int) or isinstance(curv, bool):
        return None
    if 0 <= curv < len(_controllers.CURVES):
        name = _controllers.CURVES[curv]
        return None if name == "linear" else name
    return None


def _color_name(colr: Any) -> str:
    for name, val in _controllers.FS_COLORS.items():
        if val == colr:
            return name
    return "auto"


def _impedance(z: Any) -> str:
    values = flowparams.IMPEDANCE_VALUES
    if isinstance(z, int) and not isinstance(z, bool) and 0 <= z < len(values):
        return values[z]
    return flowparams.IMPEDANCE_DEFAULT


# --- pm__ ---------------------------------------------------------------------

def _pm_index(doc: dict) -> Dict[str, Any]:
    return {e.get("key_"): e.get("val_") for e in (doc.get("pm__") or [])
            if isinstance(e, dict)}


def _preset_sources(doc: dict, pm: Dict[str, Any],
                    used: Dict[int, bool]) -> Dict[str, Any]:
    """``preset.sources`` — the per-footswitch scribble-strip config.

    Inverse of ``transcode._scribble_for`` + ``_synth_pm``: bank A stomp ``n``
    is source ``0x010101(n-1)``, bank B ``0x010102(n-1)``. All 24 slots are
    emitted whether or not a controller uses them, matching a real ``.hsp``
    export; the ``bypass`` flag is added only for sources a controller actually
    drives (``used`` carries each one's ``srcs.byps``).

    Sources with no scribble strip — EXP1/EXP2 and the EXP1 toe switch — get a
    bare ``{"bypass": ...}`` entry, again matching the real export. Without it
    their ``byps`` flag is lost and the forward transcoder falls back to its
    "bypass-driving sources are True" default, flipping the flag on re-install.
    """
    out: Dict[str, Any] = {}
    floorboard: set = set()
    for row, base in (("a", 0x01010100), ("b", 0x01010200)):
        for n in range(1, 13):
            key = f"preset.floorboard.stomp.{row}.{n}"
            label = pm.get(f"{key}.label", "")
            topidx = pm.get(f"{key}.topidx", 0)
            source = base | (n - 1)
            floorboard.add(source)
            entry: Dict[str, Any] = {
                "fs_color": _color_name(pm.get(f"{key}.color", 1)),
                "fs_label": label if isinstance(label, str) else "",
                "fs_topidx": int(topidx) if isinstance(topidx, int) else 0,
            }
            if source in used:
                entry["bypass"] = used[source]
            out[str(source)] = entry
    for source, byps in sorted(used.items()):
        if source not in floorboard:
            out[str(source)] = {"bypass": byps}
    return out


# --- cg__: snapshots + controllers -------------------------------------------

def _tamv_map(snap: dict) -> Dict[int, Any]:
    """One snapshot's flat ``[trg_id, value, ...]`` list as ``{trg_id: value}``."""
    tamv = snap.get("tamv") or []
    return {tamv[i]: tamv[i + 1] for i in range(0, len(tamv) - 1, 2)
            if isinstance(tamv[i], int)}


class _Cg:
    """Decoded ``cg__`` lookups keyed the way the block emitter needs them.

    ``snap_bypass``/``snap_params`` hold the per-snapshot arrays for
    snapshot-tracked targets (``ctm_.stid``); ``ctl_bypass``/``ctl_params`` hold
    the controller assignments. All four are keyed by the block's device
    instance id (``eID_``), params additionally by ``pid_``.
    """

    def __init__(self, doc: dict) -> None:
        entt = (doc.get("cg__") or {}).get("entt") or {}
        # A snapshot's identity is its ``si__``, NOT its position in the list:
        # the device writes the array in DESCENDING si__ order when it saves a
        # preset itself, so reading positionally would rotate every snapshot's
        # name and every ``tamv`` scene onto the wrong slot.
        self.snapshots: List[dict] = sorted(
            (s for s in entt.get("snps") or [] if isinstance(s, dict)),
            key=lambda s: s.get("si__", 0))
        trgs = {t.get("id__"): t for t in (entt.get("trgs") or [])
                if isinstance(t, dict)}
        srcs = {s.get("id__"): s for s in (entt.get("srcs") or [])
                if isinstance(s, dict)}
        tracked = {t for t in (entt.get("ctm_") or {}).get("stid") or []
                   if isinstance(t, int)}
        per_snap = [_tamv_map(s) for s in self.snapshots]

        self.snap_bypass: Dict[int, List[bool]] = {}
        self.snap_params: Dict[Tuple[int, int], List[Any]] = {}
        for tid in sorted(tracked):
            trg = trgs.get(tid)
            if not isinstance(trg, dict):
                continue
            eid = trg.get("eID_")
            values = [m.get(tid) for m in per_snap]
            if any(v is None for v in values) or not values:
                continue  # a target absent from some snapshot's tamv: unreadable
            if trg.get("type") == 1:
                # Device polarity: True == bypassed. The ``.hsp`` array is
                # "@enabled", so it is the inverse (``bridge._snapshot_arrays``).
                self.snap_bypass[eid] = [not bool(v) for v in values]
            elif trg.get("type") == 2:
                self.snap_params[(eid, trg.get("pid_"))] = values

        self.ctl_bypass: Dict[int, dict] = {}
        self.ctl_params: Dict[Tuple[int, int], dict] = {}
        self.source_bypass_flag: Dict[int, bool] = {}
        self.dropped_midi = 0
        for c in (entt.get("ctrl") or []):
            if not isinstance(c, dict):
                continue
            trg = trgs.get(c.get("tid_"))
            if not isinstance(trg, dict):
                continue
            sid = c.get("trig")
            if not sid:
                # ``trig == 0`` is a MIDI CC controller (no physical source);
                # its ``.hsp`` home is the helixgen-namespaced
                # ``preset._helixgen_midi`` list, not modeled here (hgc-zbl
                # follow-up).
                self.dropped_midi += 1
                continue
            src = srcs.get(sid) or {}
            source = _source_id(src.get("locl"), src.get("ctxt"))
            if source is None:
                continue
            self.source_bypass_flag[source] = bool(src.get("byps"))
            meta: Dict[str, Any] = {"source": source}
            curve = _curve_name(c.get("curv"))
            if curve:
                meta["curve"] = curve
            thrs = c.get("thrs")
            if isinstance(thrs, (int, float)) and not isinstance(thrs, bool) and thrs:
                meta["threshold"] = thrs
            if c.get("type") == 1:
                meta["type"] = "targetbypass"
                meta["behavior"] = _BEHAVIOR.get(c.get("behv"), "latching")
                self.ctl_bypass[trg.get("eID_")] = meta
            elif c.get("type") == 3:
                meta["type"] = "param"
                meta["behavior"] = _BEHAVIOR.get(c.get("behv"), "continuous")
                meta["min"] = c.get("min_", 0.0)
                meta["max"] = c.get("max_", 1.0)
                self.ctl_params[(trg.get("eID_"), trg.get("pid_"))] = meta


# --- flow / block emission ----------------------------------------------------

def _iter_blocks(flow: dict):
    """``(gridpos, block)`` pairs out of a flow's alternating ``blks`` list."""
    blks = flow.get("blks") or []
    gp = None
    for item in blks:
        if isinstance(item, dict):
            if gp is not None:
                yield gp, item
            gp = None
        else:
            gp = item


def _endpoint_pointers(entries: Dict[str, dict]) -> None:
    """Wire the ``.hsp`` routing pointers a real export carries, in place.

    ``b00``/``b13`` reference each other; a split points ``endpoint`` at its
    join (and vice versa) and both ``branch`` at the lane-1 span they bracket
    (``view.branch_span`` reads exactly this pair to recover the parallel lane).
    """
    if "b00" in entries and "b13" in entries:
        entries["b00"]["endpoint"] = "b13"
        entries["b13"]["endpoint"] = "b00"
    if "b14" in entries and "b27" in entries:
        entries["b14"]["endpoint"] = "b27"
        entries["b27"]["endpoint"] = "b14"
    splits = [k for k, e in entries.items() if e.get("type") == "split"]
    joins = [k for k, e in entries.items() if e.get("type") == "join"]
    lane1 = sorted(k for k, e in entries.items()
                   if int(k[1:]) >= 14 and e.get("type") not in ("input", "output"))
    if not (splits and joins and lane1):
        return
    split, join = sorted(splits)[0], sorted(joins)[-1]
    entries[split]["endpoint"] = join
    entries[join]["endpoint"] = split
    entries[split]["branch"] = lane1[0]
    entries[join]["branch"] = lane1[-1]


def _slot_params(m0: dict, plan: List[Tuple[int, str]], eid: Any,
                 cg: _Cg) -> Dict[str, Any]:
    """One model instance's ``parm`` list -> a ``.hsp`` param wrapper dict,
    in ``.hsp`` param order, re-attaching any per-snapshot array and controller
    assignment."""
    leaves = {leaf.get("pid_"): leaf for leaf in (m0.get("parm") or [])
              if isinstance(leaf, dict) and "valu" in leaf}
    params: Dict[str, Any] = {}
    for pid, name in plan:
        leaf = leaves.get(pid)
        if leaf is None:
            continue
        wrapper: Dict[str, Any] = {"value": leaf["valu"]}
        snaps = cg.snap_params.get((eid, pid))
        if snaps is not None:
            wrapper["snapshots"] = list(snaps)
        ctl = cg.ctl_params.get((eid, pid))
        if ctl is not None:
            wrapper["controller"] = dict(ctl)
        params[name] = wrapper
    return params


def _block_entry(gp: int, blk: dict, cg: _Cg, rev: Dict[int, str],
                 library: Optional[Library],
                 unresolved: List[int]) -> Optional[dict]:
    m0 = (blk.get("mdls") or [{}])[0]
    dev_id = m0.get("id__")
    if not isinstance(dev_id, int):
        return None
    hsp_model = _hsp_model(dev_id, rev)
    if hsp_model is None:
        unresolved.append(dev_id)
        return None
    category = defs.category_for(dev_id)
    eid = blk.get("id__")
    lane = 1 if gp >= _ROW1_INPUT else 0

    slot: Dict[str, Any] = {
        "model": hsp_model,
        "@enabled": {"value": bool(m0.get("enbl", 1))},
        "params": _slot_params(m0, _param_plan(dev_id, hsp_model, library),
                               eid, cg),
    }
    if isinstance(m0.get("vers"), int):
        slot["version"] = m0["vers"]
    irmd_bytes = m0.get("irmd")
    if isinstance(irmd_bytes, (bytes, bytearray)):
        slot["irhash"] = _irmd.irmd_to_irhash(irmd_bytes)

    enabled: Dict[str, Any] = {"value": bool(blk.get("enbl", 1))}
    snaps = cg.snap_bypass.get(eid)
    if snaps is not None:
        enabled["snapshots"] = list(snaps)
    ctl = cg.ctl_bypass.get(eid)
    if ctl is not None:
        enabled["controller"] = dict(ctl)

    entry: Dict[str, Any] = {
        "@enabled": enabled,
        "type": _HSP_TYPE.get(category, _DEFAULT_HSP_TYPE),
        "position": gp - _GRID_SLOTS // 2 * lane,
        "path": lane,
        "favorite": blk.get("favo", 0),
        "slot": [slot],
    }
    if category in ("input", "output"):
        entry["harness"] = {"@enabled": {"value": bool(
            (blk.get("hrns") or {}).get("enbl", 1))}}
    return entry


def _flow_entry(flow: dict, cg: _Cg, rev: Dict[int, str],
                library: Optional[Library], unresolved: List[int]) -> dict:
    entries: Dict[str, dict] = {}
    for gp, blk in _iter_blocks(flow):
        if not isinstance(gp, int) or not (0 <= gp < _GRID_SLOTS):
            continue
        model = ((blk.get("mdls") or [{}])[0]).get("id__")
        if gp == _ROW1_INPUT and model == _INPUT_NONE_MODEL:
            continue   # forward path re-synthesizes the row-1 endpoint pair
        if gp == _ROW1_OUTPUT and model == _OUTPUT_NONE_MODEL:
            continue
        entry = _block_entry(gp, blk, cg, rev, library, unresolved)
        if entry is not None:
            entries[f"b{gp:02d}"] = entry
    _endpoint_pointers(entries)
    out: Dict[str, Any] = {"@enabled": {"value": bool(flow.get("enbl", 1))}}
    out.update({k: entries[k] for k in sorted(entries)})
    return out


# --- top level ----------------------------------------------------------------

def sbepgsm_to_hsp(doc: dict, *, name: Optional[str] = None,
                   library: Optional[Library] = None,
                   author: Optional[str] = None) -> dict:
    """Decoded device ``_sbepgsm`` content -> a helixgen ``.hsp`` body.

    ``library`` supplies the helixgen param-name vocabulary (the block library
    built by ``ingest``); without it the device's own param names are used,
    which still round-trips but reads less like a helixgen export. ``name``
    lands in ``meta.name`` — the device blob does not carry the preset name
    (it lives in the containing setlist entry), so the caller supplies it.
    """
    rev = _reverse_modelmap()
    cg = _Cg(doc)
    pm = _pm_index(doc)
    unresolved: List[int] = []

    flows = [_flow_entry(f, cg, rev, library, unresolved)
             for f in (doc.get("sfg_") or {}).get("flow") or []]
    if unresolved:
        _warn(f"{len(unresolved)} block(s) had no helixgen model for device "
              f"model id(s) {sorted(set(unresolved))}; they were dropped.")
    if cg.dropped_midi:
        _warn(f"{cg.dropped_midi} MIDI CC controller(s) dropped — MIDI bindings "
              f"are not yet reversed (see bead hgc-zbl follow-up).")
    if (doc.get("cg__") or {}).get("entt", {}).get("cmnd"):
        _warn("Command Center commands dropped — not yet reversed "
              "(see bead hgc-zbl follow-up).")

    snapshots = [{
        "name": s.get("name") or f"SNAPSHOT {i + 1}",
        "color": _color_name(s.get("colr")),
        "expsw": s.get("exsw", -1),
        "source": 0,
        "tempo": s.get("bpm_", 120.0),
        "valid": bool(s.get("vald", True)),
    } for i, s in enumerate(cg.snapshots)]

    preset: Dict[str, Any] = {
        "clip": {
            "end": pm.get("preset.clip.end", 0.0),
            "filename": pm.get("preset.clip.filename", ""),
            "path": pm.get("preset.clip.path", ""),
            "start": pm.get("preset.clip.start", 0.0),
        },
        # The device blob carries no editor cursor; it is pure UI state and
        # the forward transcoder never reads it.
        "cursor": {"flow": 0, "path": 0, "position": 0},
        "flow": flows,
        "params": {
            "activeexpsw": pm.get("preset.expsw.active", 1),
            "activesnapshot": (doc.get("cg__") or {}).get("asnp", 0),
            "inst1Z": _impedance(pm.get("preset.inst1.z")),
            "inst2Z": _impedance(pm.get("preset.inst2.z")),
            "tempo": pm.get("preset.tempo.bpm", 120.0),
        },
        "snapshots": snapshots,
        "sources": _preset_sources(doc, pm, cg.source_bypass_flag),
        "xyctrl": {
            "rbtime": pm.get("preset.xyctrl.rbtime", 0.5),
            "rubberband": pm.get("preset.xyctrl.rubberband", 1),
            "x": pm.get("preset.xyctrl.x", 0),
            "y": pm.get("preset.xyctrl.y", 0),
        },
    }
    meta: Dict[str, Any] = {
        "color": "auto",
        "device_id": STADIUM_DEVICE_ID,
        "device_version": STADIUM_DEVICE_VERSION,
        "info": pm.get("preset.meta.info", ""),
        "name": name or "",
    }
    if author:
        meta["author"] = author
    return {"meta": meta, "preset": preset}


def sbe_bytes_to_hsp(blob: bytes, **kwargs) -> dict:
    """:func:`sbepgsm_to_hsp` over raw stored device-content bytes."""
    from . import content

    return sbepgsm_to_hsp(content.decode_any(blob), **kwargs)
