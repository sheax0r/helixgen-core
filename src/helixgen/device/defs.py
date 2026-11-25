"""Vendored Helix Stadium model/param name<->numeric-id definitions.

The device's network protocol addresses models and params by NUMERIC id
(``/ModelSet`` takes a ``modelId``; ``/ParamValueSet`` takes ``paramId``), while
helixgen speaks model-id strings (e.g. ``HD2_AmpBritPlexiBrt``) and human param
names (e.g. ``Drive``). This module bridges the two using a vendored asset,
``_defs_data.json``, generated from the editor app bundle by
``tools/build_device_defs.py`` — so helixgen never needs the app at runtime.

Pure stdlib. The asset shape (see the build script for provenance)::

    {
      "source":           {...},                       # file names + headers
      "models":           {"<model_str>": <int_id>},   # name  -> numeric id
      "model_names":      {"<int_id>":   "<model_str>"},# id (str key) -> name
      "model_categories": {"<model_str>": "amp", ...},
      "model_params":     {"<int_id>": {                # keyed by numeric id
          "<ParamName>": {"id": N, "type": "f"|"i"|"b",
                          "min": .., "max": .., "def": ..}, ...}},
      "catalog":          {"<model_str>": {category info}},  # editor pick-list
      "commands":         {"<name>": <int_id>, ...}
    }
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

_DATA_PATH = Path(__file__).with_name("_defs_data.json")


@lru_cache(maxsize=1)
def load_defs() -> dict:
    """Load and cache the vendored definitions asset.

    Raises a clear error if ``_defs_data.json`` is absent (regenerate it with
    ``tools/build_device_defs.py``).
    """
    if not _DATA_PATH.exists():
        raise FileNotFoundError(
            f"device definitions asset missing: {_DATA_PATH}. "
            "Regenerate it with `python tools/build_device_defs.py`."
        )
    with _DATA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def model_id_for(name_or_modelstr: str) -> Optional[int]:
    """Numeric model id for a model-id string (e.g. ``HD2_AmpBritPlexiBrt``).

    Returns ``None`` for unknown names.
    """
    return load_defs().get("models", {}).get(name_or_modelstr)


def model_name_for(model_id: int) -> Optional[str]:
    """Model-id string for a numeric model id. ``None`` if unknown."""
    return load_defs().get("model_names", {}).get(str(model_id))


def _params_for(model_id: Any) -> Optional[dict]:
    """Resolve a model's param table, accepting a numeric id or a model string."""
    defs = load_defs()
    if isinstance(model_id, str) and not model_id.lstrip("-").isdigit():
        # a model-id string; translate to its numeric id first
        mid = defs.get("models", {}).get(model_id)
        if mid is None:
            return None
        key = str(mid)
    else:
        key = str(model_id)
    return defs.get("model_params", {}).get(key)


def param_id_for(model_id: Any, param_name: str) -> Optional[int]:
    """Numeric param id for ``param_name`` on ``model_id``.

    ``model_id`` may be a numeric id or a model-id string. ``None`` if either the
    model or the param is unknown.
    """
    meta = param_meta(model_id, param_name)
    # isinstance guard (not just a None check) preserves the deleted
    # transcode._param_pid's behavior for a hypothetical non-dict meta entry.
    return meta.get("id") if isinstance(meta, dict) else None


def param_meta(model_id: Any, param_name: str) -> Optional[dict]:
    """Full param metadata ``{id, type, min, max, def}`` (or ``None``)."""
    params = _params_for(model_id)
    if params is None:
        return None
    return params.get(param_name)


def model_params_for(model_id: Any) -> dict:
    """The model's full ordered param table ``{ParamName: {id,type,..}}``.

    ``model_id`` may be a numeric id or a model-id string. Returns an empty
    dict (never ``None``) for an unknown model, so callers can iterate the
    result directly. The one canonical accessor for the raw ``model_params``
    table — never re-open ``load_defs()["model_params"]`` ad hoc (resolver
    pattern, backlog #14).
    """
    return _params_for(model_id) or {}


def mono_stereo_twin(model_id: Any) -> Optional[int]:
    """The model id of a model's mono↔stereo twin, or ``None`` if it has none.

    A twin is the same model in the opposite channel count — e.g.
    ``HD2_DelayAdriaticDelayMono`` (130) ↔ ``…Stereo`` (93). Found by swapping
    the trailing ``Mono``/``Stereo`` in the model-id string and confirming the
    twin exists in the SAME category. ``None`` for a model with no such suffix
    (amps, cabs, mono-only or stereo-only effects) or no matching twin. This is
    how the editor offers Stadium's "convert mono↔stereo in place": swap to the
    twin, then carry the shared params over by name (the mono param set is a
    subset of the stereo one).
    """
    name = model_name_for(model_id) if not isinstance(model_id, str) else model_id
    if not name:
        return None
    if name.endswith("Mono"):
        twin = name[:-4] + "Stereo"
    elif name.endswith("Stereo"):
        twin = name[:-6] + "Mono"
    else:
        return None
    tid = model_id_for(twin)
    if tid is None:
        return None
    src_id = model_id if not isinstance(model_id, str) else model_id_for(model_id)
    if category_for(tid) != category_for(src_id):   # defensive: never cross categories
        return None
    return tid


def category_for(model_id: Any) -> Optional[str]:
    """Editor category (``amp``/``cab``/``drive``/…) for a model.

    ``model_id`` may be a numeric id or a model-id string. ``None`` if the
    model or its category is unknown (or ``model_id`` is ``None``). Canonical
    model→category resolver (backlog #14) — replaces the per-module copies in
    transcode/bridge.
    """
    if model_id is None:
        return None
    if isinstance(model_id, str) and not model_id.lstrip("-").isdigit():
        name: Optional[str] = model_id
    else:
        name = model_name_for(int(model_id))
    if name is None:
        return None
    return load_defs().get("model_categories", {}).get(name)


def list_models() -> list[str]:
    """Sorted list of all known model-id strings."""
    return sorted(load_defs().get("models", {}))


def models_in_category(category: str) -> list[dict]:
    """Every model in ``category`` as ``{"id", "model", "name"}`` dicts, sorted
    by display name — the swappable choices for a block of that category
    (hgc-cc4). The device only allows a same-category model swap.
    """
    defs = load_defs()
    out = []
    for name, mid in (defs.get("models") or {}).items():
        if not isinstance(mid, int):
            continue
        if category_for(mid) != category:
            continue
        try:
            from helixgen.device import paraminfo
            display = paraminfo.model_display_name(name)
        except Exception:  # noqa: BLE001
            display = None
        out.append({"id": mid, "model": name, "name": display or name})
    out.sort(key=lambda m: (m["name"].lower(), m["id"]))
    return out


def models_by_category() -> dict:
    """``{category: [{"id","model","name"}, ...]}`` for every known model — the
    full swap catalog the live editor's model picker reads once (hgc-cc4)."""
    out: dict = {}
    for name, mid in (load_defs().get("models") or {}).items():
        if not isinstance(mid, int):
            continue
        cat = category_for(mid)
        if cat is None:
            continue
        out.setdefault(cat, [])
    for cat in out:
        out[cat] = models_in_category(cat)
    return out
