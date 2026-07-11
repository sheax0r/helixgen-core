"""Authoring bridge: assemble a device preset content blob from a chain of
blocks (model + params), by mutating a template preset in place.

v2.2 scope — a single serial chain mapped onto a template's same-category block
slots (unused slots are bypassed). Building/removing blocks or reshaping routing
is out of scope; pick a template whose chain shape covers the target.

A "chain" here is device-native: a list of ``(device_model_id, {param_name:
value})`` in signal order. The higher-level recipe/.hsp -> chain resolution
(helixgen model names -> device ids via modelmap) layers on top.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import defs
from . import content as _content

# helixgen block categories -> device model categories (defs.model_categories)
CATEGORY_MAP = {
    "amp": {"amp", "preamp"},
    "cab": {"ir", "cab", "cab_ir_interp"},
    "drive": {"distortion"},
    "distortion": {"distortion"},
    "delay": {"delay"},
    "reverb": {"reverb"},
    "modulation": {"modulation"},
    "filter": {"filter", "wah"},
    "eq": {"eq", "filter"},
    "dynamics": {"dynamics"},
    "pitch": {"pitch", "synth"},
    "volume": {"volume", "pan"},
}


def device_category(model_id: int) -> Optional[str]:
    name = defs.model_name_for(model_id)
    if name is None:
        return None
    return defs.load_defs().get("model_categories", {}).get(name)


def build_parm(model_id: int, overrides: Dict[str, Any]) -> List[dict]:
    """Full parm list for a model from defs, applying ``{param_name: value}``."""
    mp = defs.load_defs().get("model_params", {}).get(str(model_id), {})
    parm = []
    for name, meta in mp.items():
        pid = meta["id"]
        valu = overrides.get(name, meta.get("def", 0.0))
        parm.append({
            "accs": 0, "cid_": 0, "mid_": model_id, "pid_": pid,
            "snap": False, "tid_": 0, "valu": float(valu),
        })
    parm.sort(key=lambda p: p["pid_"])
    return parm


def _user_blocks(doc: dict) -> List[Tuple[int, dict]]:
    """Return (flow-position, block-dict) for user blocks (skip input/output)."""
    out = []
    blks = doc["sfg_"]["flow"][0]["blks"]
    pos = -1
    for b in blks:
        if not isinstance(b, dict):
            continue
        pos += 1
        mid = (b.get("mdls") or [{}])[0].get("id__")
        cat = device_category(mid) if mid is not None else None
        if cat in (None, "input", "output"):
            continue
        out.append((pos, b))
    return out


def author_chain(doc: dict, chain: Sequence[Tuple[int, Dict[str, Any]]]) -> dict:
    """Map ``chain`` onto ``doc``'s same-category user-block slots (in order);
    set model id + params on matched slots and bypass the rest.

    Raises ValueError if a chain block has no free template slot of its category.
    """
    slots = _user_blocks(doc)
    used = set()
    for model_id, params in chain:
        want = device_category(model_id)
        # find the first unused slot whose category matches
        pick = None
        for i, (pos, blk) in enumerate(slots):
            if i in used:
                continue
            if device_category((blk["mdls"][0]).get("id__")) == want:
                pick = i
                break
        if pick is None:
            raise ValueError(
                f"no free template slot for model {model_id} "
                f"(category {want!r}); choose a template with that block")
        used.add(pick)
        _pos, blk = slots[pick]
        m = blk["mdls"][0]
        m["id__"] = model_id
        m["parm"] = build_parm(model_id, params)
        blk["enbl"] = 1
    # bypass every user slot we didn't assign
    for i, (_pos, blk) in enumerate(slots):
        if i not in used:
            blk["enbl"] = 0
    return doc


def content_from_template(template_blob: bytes,
                          chain: Sequence[Tuple[int, Dict[str, Any]]]) -> bytes:
    """Author a chain onto a template content blob; return a stored-content blob
    ready for /SetContentData."""
    doc = _content.decode_any(template_blob)
    author_chain(doc, chain)
    return _content.encode_content_data(doc)


def install_chain(client, container: int, pos: int, name: str,
                  template_blob: bytes,
                  chain: Sequence[Tuple[int, Dict[str, Any]]]) -> Optional[int]:
    """Author ``chain`` onto ``template_blob`` and install it as a new preset."""
    blob = content_from_template(template_blob, chain)
    return client.push_to_slot(container, pos, name, blob)
