"""The live edit buffer as a signal chain — the structure `device live` renders.

Pure functions over a DECODED edit buffer (``HelixClient.read_edit_buffer()``
or a decoded ``/setEditBuffer`` blob): no device I/O, no `.hsp`. The device is
the only source of truth here; nothing is mapped onto a library tone.

Device model, per DSP path (``sfg_.flow[i]``): a two-row grid of 28 slots.
Row 0 is slots 0..13 (input at 0, output at 13); row 1 is slots 14..27 (input
at 14, output at 27). ``blks`` is a flat ``[grid_slot, block, …]`` list. A
split in row 0 feeds row 1; a join brings row 1 back. A split/join/tap names
its branch target with ``bblk`` — a block INSTANCE id, resolved to a grid slot
through the flow's ``bmap`` — and ``bflw``, the flow that target lives in
(another flow = a cross-DSP tap, which is not a lane of this path).

Shape returned by :func:`build_chain`::

    {"snapshot": int | None, "snapshots": [name, …],
     "paths": [{"path": 0, "segments": [SEG, …], "extra": [SEG, …]}]}

    SEG   = {"kind": "block", "block": BLOCK}
          | {"kind": "parallel", "split": BLOCK, "a": [SEG, …],
             "b": [SEG, …], "join": BLOCK | None}
    BLOCK = {path, block (grid slot), row, model_id, model, name, category,
             enabled, xlink ({"path": n} | None), params: [PARAM, …]}
    PARAM = {pid, name, label, type, ctl ("knob" | "toggle" | "select"),
             value, min, max, default, unit, scale, display_min, display_max,
             labels, internal, snap}

Whether the tone has unsaved edits is NOT in the buffer (its ``hist`` field
stays set after a save); that is the ``volatile.preset.edited`` property.

``extra`` holds row-1 blocks no split feeds (a row with its own real input).
Every modeled block except the "None" endpoints appears exactly once. Values
are RAW units (what ``/ParamValueSet`` takes); bools are encoded 0.0/1.0.
Each block's first model (``mdls[0]``) is editable; a dual cab's second model
is surfaced READ-ONLY (hgc-5gh) and its harness params (e.g. Trails) still
have no known live-write address.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from helixgen.device import defs, paraminfo
from helixgen.device.irmd import irmd_to_irhash

ROW1_INPUT, ROW1_OUTPUT = 14, 27
MAX_SNAPSHOTS = 64        # the device has 8; a wild index must not size a list
_PREFIX = re.compile(r"^[A-Za-z0-9]+_")
_NONE_ENDPOINTS = ("InputNone", "OutputNone")


def _blks_pairs(dsp: Any):
    """Yield ``(grid_slot, block)`` from a flow's ``blks`` (flat alternating
    list on the wire; a dict in some decoded variants)."""
    blks = dsp.get("blks") if isinstance(dsp, dict) else None
    if isinstance(blks, dict):
        for k, v in blks.items():
            if isinstance(k, int) and isinstance(v, dict):
                yield k, v
        return
    if not isinstance(blks, list):
        return
    i = 0
    while i + 1 < len(blks):
        key, b = blks[i], blks[i + 1]
        if isinstance(key, int) and not isinstance(key, bool) and isinstance(b, dict):
            yield key, b
            i += 2
        else:
            i += 1


def _fallback_name(model: Optional[str], mid: int) -> str:
    """Readable name for a model the editor has no display name for (the
    structural blocks): ``P35_AppDSPSplitY`` -> ``Split Y``."""
    if not model:
        return f"Model {mid}"
    s = _PREFIX.sub("", model).replace("AppDSP", "")
    s = re.sub(r"(?<=\d)_(?=\d)", "/", s)                 # USB3_4 -> USB3/4
    s = re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)",
               " ", s)
    return s.strip() or f"Model {mid}"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _params(mid: int, model: Optional[str], m0: dict) -> List[dict]:
    stored: Dict[int, Any] = {}
    parm = m0.get("parm")
    for p in (parm if isinstance(parm, list) else []):
        if isinstance(p, dict) and isinstance(p.get("pid_"), int):
            stored[p["pid_"]] = p
    out = []
    for name, meta in defs.model_params_for(mid).items():
        pid = meta.get("id")
        if not isinstance(pid, int):
            continue
        entry = stored.get(pid) or {}
        raw = entry.get("valu")
        if raw is None:
            raw = meta.get("def")
        value = _num(raw)
        lo, hi = _num(meta.get("min")), _num(meta.get("max"))
        if (value is None or lo is None or hi is None
                or not all(math.isfinite(x) for x in (value, lo, hi))):
            continue                      # nothing a control could address
        try:
            info = paraminfo.param_info(model, name) if model else {}
        except Exception:  # noqa: BLE001 — display metadata is best-effort
            info = {}
        ptype = meta.get("type")
        labels = info.get("enum_labels")
        if ptype == "b":
            ctl = "toggle"
        elif isinstance(labels, list) and labels and ptype == "i":
            ctl = "select"
        else:
            ctl = "knob"
        out.append({
            "pid": pid, "name": name,
            "label": info.get("display_name") or name,
            "type": ptype, "ctl": ctl, "value": value,
            "min": int(lo) if ptype in ("b", "i") else lo,
            "max": int(hi) if ptype in ("b", "i") else hi,
            "default": _num(meta.get("def")),
            "unit": info.get("unit") or "",
            "scale": info.get("scale"),
            "display_min": info.get("display_min"),
            "display_max": info.get("display_max"),
            "labels": labels if isinstance(labels, list) else None,
            "internal": bool(info.get("internal")),
            "snap": bool(entry.get("snap")),
        })
    out.sort(key=lambda p: p["pid"])
    return out


def _block(path: int, slot: int, raw: dict) -> Optional[dict]:
    mdls = raw.get("mdls")
    m0 = mdls[0] if isinstance(mdls, list) and mdls else None
    mid = m0.get("id__") if isinstance(m0, dict) else None
    if not isinstance(mid, int) or isinstance(mid, bool):
        return None
    model = defs.model_name_for(mid)
    name = None
    if model:
        try:
            name = paraminfo.model_display_name(model)
        except Exception:  # noqa: BLE001
            name = None
    block = {
        "path": path, "block": slot,
        "row": 1 if slot >= ROW1_INPUT else 0,
        "model_id": mid, "model": model or str(mid),
        "name": name or _fallback_name(model, mid),
        "category": defs.category_for(mid),
        "enabled": bool(raw.get("enbl", 1)),
        "xlink": None,
        "params": _params(mid, model, m0),
    }
    # An IR-loader block ("ir") carries the loaded IR's content hash as the raw
    # 16-byte mdls[0]["irmd"] blob (no slot indirection). Surface it as a hex
    # irhash so the editor can resolve the user's local .wav and plot its
    # frequency response (built-in cab IRs are DSP-internal; see bead 81v.23).
    if block["category"] == "ir":
        irmd = m0.get("irmd") if isinstance(m0, dict) else None
        block["ir_hash"] = (irmd_to_irhash(irmd)
                            if isinstance(irmd, (bytes, bytearray)) and len(irmd) == 16
                            else None)
    # Dual-cab SECOND model (mdls[1]): surface it READ-ONLY (hgc-5gh). Its param
    # values are already in the edit buffer, so they can be shown, but it has no
    # known live-write address (that needs a hardware probe — the state model
    # would grow from (path,slot,pid) to (path,slot,sub,pid)), so the page shows
    # it and sends NO writes. Without this the second cab was invisible.
    m1 = mdls[1] if isinstance(mdls, list) and len(mdls) > 1 else None
    if isinstance(m1, dict):
        mid1 = m1.get("id__")
        if isinstance(mid1, int) and not isinstance(mid1, bool):
            model1 = defs.model_name_for(mid1)
            try:
                name1 = paraminfo.model_display_name(model1) if model1 else None
            except Exception:  # noqa: BLE001
                name1 = None
            block["second_model"] = {
                "model_id": mid1,
                "model": model1 or str(mid1),
                "name": name1 or _fallback_name(model1, mid1),
                "params": _params(mid1, model1, m1),
                "read_only": False,   # 2nd-cab params are writable via sub=1 (hgc-5gh resolved 2026-11-25)
            }
    return block


def _is_none_endpoint(b: dict) -> bool:
    return b["model"].endswith(_NONE_ENDPOINTS)


def _seg(b: dict) -> dict:
    return {"kind": "block", "block": b}


def _layout(path: int, dsp: dict) -> Optional[dict]:
    bmap = dsp.get("bmap") if isinstance(dsp.get("bmap"), list) else []
    blocks: Dict[int, dict] = {}
    pointer: Dict[int, int] = {}          # grid slot -> in-flow branch slot
    for slot, raw in _blks_pairs(dsp):
        b = _block(path, slot, raw)
        if b is None:
            continue
        blocks[slot] = b
        bblk, bflw = raw.get("bblk"), raw.get("bflw")
        if not (raw.get("hasb") and isinstance(bblk, int)):
            continue
        if isinstance(bflw, int) and bflw != path:
            b["xlink"] = {"path": bflw}   # cross-DSP tap: a tag, never a lane
        elif bblk in bmap:
            pointer[slot] = bmap.index(bblk)

    real = {s: b for s, b in blocks.items() if not _is_none_endpoint(b)}
    if not real:
        return None
    row0 = [real[s] for s in sorted(real) if s < ROW1_INPUT]
    row1 = [real[s] for s in sorted(real) if s >= ROW1_INPUT]
    claimed: set = set()

    def forms_lane(b: dict) -> bool:
        return b["category"] == "split" and b["xlink"] is None

    def branch(lo: int, hi: int) -> List[dict]:
        out = []
        for b in row1:
            s = b["block"]
            if s in claimed or s == ROW1_INPUT or not (lo <= s <= hi):
                continue
            claimed.add(s)
            out.append(_seg(b))
        return out

    def walk(items: List[dict]) -> List[dict]:
        segs, i = [], 0
        while i < len(items):
            b = items[i]
            if not forms_lane(b):
                segs.append(_seg(b))
                i += 1
                continue
            # pair with ITS join: the first join after it, skipping any
            # split/join pairs nested in between
            depth, j = 0, None
            for k in range(i + 1, len(items)):
                if forms_lane(items[k]):
                    depth += 1
                elif items[k]["category"] == "join":
                    if depth == 0:
                        j = k
                        break
                    depth -= 1
            lo = pointer.get(b["block"])
            if lo is None or not (ROW1_INPUT < lo <= ROW1_OUTPUT):
                lo = ROW1_INPUT + 1
            if j is not None:
                join = items[j]
                hi = pointer.get(join["block"])
                if hi is None or not (ROW1_INPUT < hi < ROW1_OUTPUT):
                    hi = join["block"] + ROW1_INPUT
                a = walk(items[i + 1:j])
                segs.append({"kind": "parallel", "split": b, "a": a,
                             "b": branch(lo, hi), "join": join})
                i = j + 1
            else:
                a = walk(items[i + 1:])
                segs.append({"kind": "parallel", "split": b, "a": a,
                             "b": branch(lo, ROW1_OUTPUT), "join": None})
                i = len(items)
        return segs

    segments = walk(row0)
    extra = [_seg(b) for b in row1 if b["block"] not in claimed]
    return {"path": path, "segments": segments, "extra": extra}


def build_chain(eb: Any) -> dict:
    """Decoded edit buffer -> the chain structure (see module docstring).
    Never raises on a malformed buffer: what cannot be parsed is omitted."""
    eb = eb if isinstance(eb, dict) else {}
    flow = (eb.get("sfg_") or {}).get("flow") if isinstance(eb.get("sfg_"), dict) else None
    paths = []
    for i, dsp in enumerate(flow if isinstance(flow, list) else []):
        if not isinstance(dsp, dict):
            continue
        laid = _layout(i, dsp)
        if laid is not None:
            paths.append(laid)
    cg = eb.get("cg__") if isinstance(eb.get("cg__"), dict) else {}
    asnp = cg.get("asnp")
    return {
        "snapshot": asnp if isinstance(asnp, int) and not isinstance(asnp, bool) else None,
        "snapshots": _snapshot_names(cg),
        "snapshot_colors": _snapshot_colors(cg),
        "paths": paths,
    }


def _snps_by_index(cg: dict) -> dict:
    """``cg__.entt.snps[]`` keyed by ``si__`` (0..MAX_SNAPSHOTS-1)."""
    entt = cg.get("entt") if isinstance(cg.get("entt"), dict) else {}
    snps = entt.get("snps")
    out = {}
    for s in (snps if isinstance(snps, list) else []):
        i = s.get("si__") if isinstance(s, dict) else None
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < MAX_SNAPSHOTS:
            out[i] = s
    return out


def _snapshot_names(cg: dict) -> List[str]:
    """Snapshot names by index (``cg__.entt.snps[]``, each keyed by ``si__``)."""
    named = {i: str(s.get("name") or f"Snapshot {i + 1}")
             for i, s in _snps_by_index(cg).items()}
    return [named.get(i, f"Snapshot {i + 1}") for i in range(max(named) + 1)] if named else []


def _snapshot_colors(cg: dict) -> List[int]:
    """Snapshot ``colr`` indices by slot, aligned with :func:`_snapshot_names`
    (an int into the shared preset-colour palette). A slot with no stored colr
    reads 0 ('auto'). Empty when no snapshots are configured."""
    by = _snps_by_index(cg)
    if not by:
        return []
    def _c(s):
        v = s.get("colr")
        return v if isinstance(v, int) and not isinstance(v, bool) else 0
    return [_c(by[i]) if i in by else 0 for i in range(max(by) + 1)]


def _iter_segments(segs: List[dict]) -> Iterator[dict]:
    for s in segs:
        if s["kind"] == "block":
            yield s["block"]
        else:
            yield s["split"]
            yield from _iter_segments(s["a"])
            yield from _iter_segments(s["b"])
            if s["join"] is not None:
                yield s["join"]


def iter_blocks(chain: dict) -> Iterator[dict]:
    """Every block in the chain, once, in render order."""
    for p in chain.get("paths", []):
        yield from _iter_segments(p["segments"])
        yield from _iter_segments(p["extra"])


def block_index(chain: dict) -> Dict[Tuple[int, int], dict]:
    """``(path, grid_slot) -> BLOCK``."""
    return {(b["path"], b["block"]): b for b in iter_blocks(chain)}


def param_index(chain: dict) -> Dict[Tuple[int, int, int], dict]:
    """``(path, grid_slot, pid) -> PARAM``."""
    return {(b["path"], b["block"], p["pid"]): p
            for b in iter_blocks(chain) for p in b["params"]}


def clamp(param: dict, value: float) -> float:
    """``value`` forced into what the device's own control could produce: inside
    the advertised range (the device clamps NOTHING — an out-of-range write
    lands and is applied), whole numbers for int params, 0/1 for bools."""
    v = float(value)
    if param["type"] == "b":
        return 1.0 if v >= 0.5 else 0.0
    if param["type"] == "i":
        v = float(round(v))
    return min(float(param["max"]), max(float(param["min"]), v))
