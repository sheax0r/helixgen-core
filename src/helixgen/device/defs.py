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
