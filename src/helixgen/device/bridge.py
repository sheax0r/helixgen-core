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


def map_params(model_id: int, src_params: Dict[str, Any]) -> Dict[str, Any]:
    """Map helixgen param names -> device param names for a model.

    Device and helixgen mostly share param names (Tone/Level/Bass/Mix/…) but a
    few differ (helixgen "Drive" vs device "Gain"). Strategy: exact/normalized
    name match first, then assign any leftover helixgen params to the remaining
    device params by position (both are in canonical param order). Returns
    ``{device_param_name: value}``.
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
