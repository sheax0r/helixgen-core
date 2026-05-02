"""Chassis: empty-preset shell extracted from a real export, used as generation template."""
from __future__ import annotations

import copy
from typing import Any

from helixgen.ingest import (
    PRESET_DSP_KEYS,
    _is_cab_key,
    _is_user_block_key,
)


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
