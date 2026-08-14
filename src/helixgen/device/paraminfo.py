"""Real param ranges, units and enum labels for a library block.

The block library records, per param, only what a single sighted preset had:
``{"type", "default", "observed_range": [v, v]}`` — one sample, not a range
(``ingest.extract_schema``). Everything an author actually needs to set a param
correctly — the min/max, whether the units are dB or 0..1 or Hz, what enum
value 5 MEANS — already ships in this package, unread, in two vendored assets:

- ``_defs_data.json`` (``defs.py``) — ``{id, type, min, max, def}`` per param,
  the device's own RAW range and default.
- ``_param_ui.json`` (``tools/build_param_meta.py``) — what that raw range
  means: display name, unit, raw->display scale, enum labels.

This module overlays both onto a library block AT READ TIME. It never rewrites
the library, and ``ingest`` is unchanged: ``.hsp`` files genuinely carry no
ranges, so there is nothing there to widen.

Model resolution tries three candidates, in order, and takes the first that
knows the param:

1. the helixgen model id verbatim (``HD2_AmpBritPlexiBrt``),
2. ``modelmap.device_model_id()`` (helixgen ids the device spells differently,
   e.g. ``HD2_DrvScream808`` -> ``HD2_DistScream808Mono``),
3. the ``Stereo`` -> ``Mono`` sibling (the device models a stereo variant's
   params once, on the mono model).

Enum labels are aligned to the param's ``min``: ``labels[value - min]``. The
app's label lists always span exactly ``max - min + 1`` entries (asserted in
``tests/test_paraminfo.py``); a value outside that span yields no label rather
than a wrong one.

A param with no UI entry (``IrData``, ``AmpCabPeak*``, ``AmpCabZFir``, …) is an
internal, non-knob param the editor deliberately does not expose. Its absence
is information: report the raw range if there is one, and invent nothing else.

Pure stdlib, no device connection — this is vendored data, not a wire query.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from . import defs, modelmap

_UI_PATH = Path(__file__).with_name("_param_ui.json")


@lru_cache(maxsize=1)
def load_param_ui() -> dict:
    """Load and cache the vendored param-UI asset.

    Raises a clear error if ``_param_ui.json`` is absent (regenerate it with
    ``tools/build_param_meta.py``).
    """
    if not _UI_PATH.exists():
        raise FileNotFoundError(
            f"param UI asset missing: {_UI_PATH}. "
            "Regenerate it with `python tools/build_param_meta.py`."
        )
    with _UI_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=512)
def model_candidates(model_id: str) -> tuple[str, ...]:
    """Device model-id strings to try for ``model_id``, best first.

    Empty when the model is unknown to the device definitions.
    """
    numeric: list[int] = []
    for mid in (
        defs.model_id_for(model_id),
        modelmap.device_model_id(model_id),
        defs.model_id_for(model_id.replace("Stereo", "Mono"))
        if "Stereo" in model_id
        else None,
    ):
        if mid is not None and mid not in numeric:
            numeric.append(mid)
    names = [defs.model_name_for(mid) for mid in numeric]
    return tuple(n for n in names if n)


def _ui_entry(model_id: str, param_name: str) -> Optional[dict]:
    models = load_param_ui().get("models", {})
    for name in model_candidates(model_id):
        entry = models.get(name, {}).get(param_name)
        if entry is not None:
            return entry
    return None


def _defs_entry(model_id: str, param_name: str) -> Optional[dict]:
    for name in model_candidates(model_id):
        meta = defs.param_meta(name, param_name)
        if meta is not None:
            return meta
    return None


def param_info(model_id: str, param_name: str) -> dict[str, Any]:
    """Resolved facts about one param. Keys are present only when known.

    ``{min, max, default, unit, scale, display_name, enum_labels}`` —
    ``default`` is the DEVICE default (not the library's sighted value),
    ``scale`` is the raw->display multiplier (``generic_knob`` = 10, so a
    stored 0.5 displays as 5.0). Returns ``{}`` when nothing is known.
    """
    info: dict[str, Any] = {}
    meta = _defs_entry(model_id, param_name)
    if meta:
        for src, dst in (("min", "min"), ("max", "max"), ("def", "default")):
            if meta.get(src) is not None:
                info[dst] = meta[src]

    entry = _ui_entry(model_id, param_name)
    if entry is None:
        # The editor exposes no control for this param on any candidate model
        # — it is internal plumbing (IrData, AmpCabPeak*, AmpCabZFir, …), not
        # a knob. Say so rather than dressing a raw range up as a knob.
        if model_candidates(model_id):
            info["internal"] = True
    else:
        control = load_param_ui().get("controls", {}).get(entry.get("tag"), {})
        name = entry.get("name")
        if name:
            info["display_name"] = name
        if control.get("unit"):
            info["unit"] = control["unit"]
        if control.get("scale"):
            info["scale"] = control["scale"]
        labels = entry.get("labels") or control.get("labels")
        if labels:
            info["enum_labels"] = list(labels)
    return info


def label_for(info: dict[str, Any], value: Any) -> Optional[str]:
    """Enum label for ``value``, or ``None`` (not an enum / out of range)."""
    labels = info.get("enum_labels")
    if not labels or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    low = info.get("min")
    index = int(round(value)) - (int(low) if isinstance(low, (int, float)) else 0)
    if 0 <= index < len(labels):
        return labels[index]
    return None


def block_param_info(
    model_id: str, params: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Overlay resolved facts onto a library block's param schema.

    Each entry keeps the library's ``type`` and renames its single-sample
    ``default``/``observed_range`` to ``sighted`` — one value seen in one
    ingested preset, never a range — then adds whatever
    :func:`param_info` resolves, plus ``sighted_label`` / ``default_label``
    for enums.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, schema in params.items():
        entry: dict[str, Any] = {"type": schema.get("type")}
        if "default" in schema:
            entry["sighted"] = schema["default"]
        entry.update(param_info(model_id, name))
        if "enum_labels" in entry:
            for key, source in (("sighted_label", "sighted"), ("default_label", "default")):
                label = label_for(entry, entry.get(source))
                if label is not None:
                    entry[key] = label
        out[name] = entry
    return out
