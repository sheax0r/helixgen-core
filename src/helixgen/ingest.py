"""Ingest module: parse exported .hlx and single-block JSON, extract schemas."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# DOCUMENTED ASSUMPTIONS about the Helix export wire format.
# Verify against a real exported .hlx in Task 34. If the real shape differs,
# update these constants and the synthetic fixtures together.
# ---------------------------------------------------------------------------
RAW_BLOCK_MODEL_KEY = "@model"          # block JSON: model identifier
RAW_BLOCK_CATEGORY_KEY = "@category"    # block JSON: optional category override
RAW_BLOCK_NAME_KEY = "@name"            # block JSON: optional human-readable name
RAW_BLOCK_SYSTEM_KEY_PREFIX = "@"       # any key starting with this is metadata, not a param

PRESET_TONE_KEY = ("data", "tone")      # full preset: path to dsp0/dsp1 root
PRESET_DSP_KEYS = ("dsp0", "dsp1")
PRESET_BLOCKS_KEY = "blocks"            # within each dsp, the block dict


# ---------------------------------------------------------------------------
# Category inference from model_id prefix.
# Order matters: most specific first. Add new prefixes here as discovered.
# ---------------------------------------------------------------------------
_CATEGORY_PREFIXES: list[tuple[str, str]] = [
    ("HD2_Amp", "amp"),
    ("HD2_Cab", "cab"),
    ("HD2_Drv", "drive"),
    ("HD2_Dist", "drive"),
    ("HD2_Rvb", "reverb"),
    ("HD2_Dly", "delay"),
    ("HD2_EQ", "eq"),
    ("HD2_Dynamics", "dynamics"),
    ("HD2_Dyn", "dynamics"),
    ("HD2_Mod", "modulation"),
    ("HD2_Pitch", "pitch"),
    ("HD2_Wah", "filter"),
]


def infer_category(model_id: str) -> str:
    """Return the category for a model_id, or 'uncategorized' if unknown."""
    for prefix, category in _CATEGORY_PREFIXES:
        if model_id.startswith(prefix):
            return category
    return "uncategorized"


# Strip a known category prefix; insert a space before any uppercase letter
# that follows a lowercase letter or digit; insert a space before digits that
# follow a lowercase letter (except 'x' for unit specs like "4x12").
# Finally collapse whitespace.
_HUMANIZE_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<![x])(?<=[a-z])(?=[0-9])")


def humanize_model_id(model_id: str) -> str:
    """Turn a model_id like 'HD2_AmpBrit2204Custom' into 'Brit 2204 Custom'."""
    body = model_id
    for prefix, _ in _CATEGORY_PREFIXES:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    else:
        # No known prefix: also strip any leading "HD2_" if present
        if body.startswith("HD2_"):
            body = body[4:]
    spaced = _HUMANIZE_SPLIT_RE.sub(" ", body)
    return " ".join(spaced.split())


class Shape(Enum):
    PRESET = "preset"
    SINGLE_BLOCK = "single_block"
    UNKNOWN = "unknown"


def detect_shape(data: Any) -> Shape:
    """Detect whether a parsed JSON value is a full preset, a single block, or neither."""
    if not isinstance(data, dict):
        return Shape.UNKNOWN

    if (
        "version" in data
        and "schema" in data
        and isinstance(data.get("data"), dict)
        and isinstance(data["data"].get("tone"), dict)
        and any(dsp in data["data"]["tone"] for dsp in PRESET_DSP_KEYS)
    ):
        return Shape.PRESET

    if RAW_BLOCK_MODEL_KEY in data:
        return Shape.SINGLE_BLOCK

    return Shape.UNKNOWN
