"""Chassis: empty-preset shell extracted from a real export, used as generation template."""
from __future__ import annotations

import copy
from typing import Any

from helixgen.ingest import PRESET_BLOCKS_KEY, PRESET_DSP_KEYS


def extract_chassis(preset: dict[str, Any]) -> dict[str, Any]:
    """Return a chassis: a deep copy of `preset` with all blocks removed.

    Records the original position keys (the keys of each dsp's `blocks` dict)
    under `_helixgen.position_keys.{dsp0, dsp1}` so generation can reuse them.
    """
    chassis = copy.deepcopy(preset)
    tone = chassis.setdefault("data", {}).setdefault("tone", {})

    position_keys: dict[str, list[str]] = {}
    for dsp_key in PRESET_DSP_KEYS:
        dsp = tone.get(dsp_key)
        if dsp is None:
            position_keys[dsp_key] = []
            continue
        blocks = dsp.get(PRESET_BLOCKS_KEY, {})
        position_keys[dsp_key] = list(blocks.keys())
        dsp[PRESET_BLOCKS_KEY] = {}

    chassis.setdefault("_helixgen", {})["position_keys"] = position_keys
    return chassis
