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


def _build_category_groups() -> Dict[str, str]:
    """Union device categories that co-occur in any ``CATEGORY_MAP`` value set.

    The device exposes some physically-interchangeable block slots under several
    distinct category strings (a single Cab slot reports ``ir`` / ``cab`` /
    ``cab_ir_interp`` depending on what it hosts; an amp slot reports ``amp`` or
    ``preamp``; …). ``CATEGORY_MAP`` already records those equivalences (one
    helixgen category -> the set of device categories that satisfy it), so we
    derive the compatibility groups from it rather than hard-coding them. Two
    device categories that share a group are interchangeable for slot matching.
    Returns ``{device_category: group_key}``; a category in no value set is its
    own group.
    """
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for cats in CATEGORY_MAP.values():
        cats = list(cats)
        for c in cats[1:]:
            union(cats[0], c)
    return {c: find(c) for c in parent}


_CATEGORY_GROUPS = _build_category_groups()


def _category_group(device_category: Optional[str]) -> Optional[str]:
    """Canonical group key for a device category (interchangeable-slot family).

    Categories in the same ``CATEGORY_MAP`` value set (transitively) share a key;
    a category in no value set — or ``None`` — maps to itself.
    """
    if device_category is None:
        return None
    return _CATEGORY_GROUPS.get(device_category, device_category)


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
        want_group = _category_group(want)
        # find the first unused slot whose category group matches (physically
        # interchangeable slots — e.g. a Cab slot hosting ir/cab/cab_ir_interp —
        # are compatible even though their category strings differ)
        pick = None
        for i, (pos, blk) in enumerate(slots):
            if i in used:
                continue
            slot_cat = device_category((blk["mdls"][0]).get("id__"))
            if _category_group(slot_cat) == want_group:
                pick = i
                break
        if pick is None:
            raise ValueError(
                f"no free template slot for model {model_id} "
                f"(category {want!r}, group {want_group!r}); "
                f"choose a template with that block")
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
    return client._raw.push_to_slot(container, pos, name, blob)


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


def install_recipe(client, hsp_body: dict, container: int, pos: int, name: str,
                   template_blob: bytes, *, dsp: int = 0,
                   resolve_model=_default_resolve_model,
                   strict: bool = True) -> Optional[int]:
    """Author a helixgen ``.hsp`` body onto ``template_blob`` and install it."""
    chain = hsp_to_chain(hsp_body, dsp=dsp, resolve_model=resolve_model, strict=strict)
    return install_chain(client, container, pos, name, template_blob, chain)
