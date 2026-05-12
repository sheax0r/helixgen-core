"""Chassis: empty-preset shell extracted from a real export, used as generation template."""
from __future__ import annotations

import copy
from typing import Any

from helixgen.hsp import ENDPOINT_KEYS
from helixgen.ingest import (
    PRESET_DSP_KEYS,
    _is_cab_key,
    _is_user_block_key,
)


# Marker key written into the chassis dict so downstream code (generate, CLI)
# can tell which export shape the chassis came from. Absent or "hlx" → legacy
# Helix .hlx shape; "hsp" → Stadium .hsp shape.
CHASSIS_SHAPE_KEY = "_helixgen_chassis_shape"


def extract_chassis(preset: dict[str, Any]) -> dict[str, Any]:
    """Return a chassis: a deep copy of `preset` with all user blocks + cabs removed.

    Strips every `block*` and `cab*` slot from each dsp; preserves everything
    else (inputA/inputB/outputA/outputB/split/join under each dsp; and at the
    `tone` level, snapshot0..7, global, footswitch, controller). On
    generation we copy this chassis and place new blocks/cabs back into the
    cleared slots.
    """
    chassis = copy.deepcopy(preset)
    tone = chassis.setdefault("data", {}).setdefault("tone", {})

    for dsp_key in PRESET_DSP_KEYS:
        dsp = tone.get(dsp_key)
        if not isinstance(dsp, dict):
            continue
        for k in [k for k in dsp if _is_user_block_key(k) or _is_cab_key(k)]:
            del dsp[k]

    return chassis


def extract_chassis_from_hsp(hsp_data: dict[str, Any]) -> dict[str, Any]:
    """Return a Stadium-shape chassis: the .hsp payload with user blocks stripped.

    Strips every `bNN` key in `preset.flow[*]` except the input/output
    endpoints (b00, b13). Preserves path-level metadata (@enabled, etc.) and
    top-level `meta`. The result is tagged with `_helixgen_chassis_shape`
    = "hsp" so generate-side code can distinguish it from a .hlx chassis.
    """
    chassis = copy.deepcopy(hsp_data)
    flow = chassis.get("preset", {}).get("flow")
    if isinstance(flow, list):
        for path in flow:
            if not isinstance(path, dict):
                continue
            for k in [
                k for k in path
                if isinstance(k, str)
                and k.startswith("b")
                and k not in ENDPOINT_KEYS
                and k[1:].isdigit()
            ]:
                del path[k]
    chassis[CHASSIS_SHAPE_KEY] = "hsp"
    return chassis
