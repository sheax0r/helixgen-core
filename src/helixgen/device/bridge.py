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


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def map_params(model_id: int, src_params: Dict[str, Any]) -> Dict[str, Any]:
    """Map helixgen param names -> device param names for a model.

    Device and helixgen mostly share param names (Tone/Level/Bass/Mix/…) but a
    few differ (helixgen "Drive" vs device "Gain"). Strategy: exact/normalized
    name match first, then assign any leftover helixgen params to the remaining
    device params by position (both are in canonical param order). Returns
    ``{device_param_name: value}`` for use by :func:`build_parm`.
    """
    dev = defs.load_defs().get("model_params", {}).get(str(model_id), {})
    dev_names = list(dev.keys())
    by_norm = {_norm(n): n for n in dev_names}
    out: Dict[str, Any] = {}
    leftover: List[Tuple[str, Any]] = []
    used = set()
    for hn, v in src_params.items():
        dn = by_norm.get(_norm(hn))
        if dn is not None:
            out[dn] = v
            used.add(dn)
        else:
            leftover.append((hn, v))
    remaining = [n for n in dev_names if n not in used]
    for (_hn, v), dn in zip(leftover, remaining):
        out[dn] = v
    return out


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


def install_recipe(client, hsp_body: dict, container: int, pos: int, name: str,
                   template_blob: bytes, *, dsp: int = 0,
                   resolve_model=_default_resolve_model,
                   strict: bool = True) -> Optional[int]:
    """Author a helixgen ``.hsp`` body onto ``template_blob`` and install it."""
    chain = hsp_to_chain(hsp_body, dsp=dsp, resolve_model=resolve_model, strict=strict)
    return install_chain(client, container, pos, name, template_blob, chain)
