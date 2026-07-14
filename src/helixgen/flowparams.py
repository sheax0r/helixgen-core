"""Signal-flow parameter schemas: input/output endpoints, split types, merge
mixer, impedance — the tables behind first-class signal-flow authoring
(parity #18).

Everything here is evidence-backed, never invented:

- Param names/types/ranges/defaults mirror the bundled device defs
  (``src/helixgen/device/_defs_data.json``) and were cross-checked against the
  211-export real-device corpus (see the 2026-07-14 design spec).
- The impedance enum is the device's own self-description of
  ``preset.inst1.z`` (``/PropertyDefWithKeyGet``): 10 values, device int =
  enum index, factory default = index 1 (*First Enabled*).

Pure stdlib, no imports from the rest of helixgen — safe for spec-, generate-,
view-, mutate- and device-layer use alike.
"""
from __future__ import annotations

from typing import Any

# --- impedance (preset-level `preset.params.inst1Z` / `inst2Z`) --------------

# .hsp string per device enum index 0..9. Indices 0/1/8/9 are corpus-observed
# ("FirstBlock" x120, "FirstEnabled" x76, "230K" x2, "1M" x13); the middle
# rungs (10K..136K) are inferred from the observed compact convention
# (uppercase K, no "Ohm") — flagged in the design spec.
IMPEDANCE_VALUES: tuple[str, ...] = (
    "FirstBlock", "FirstEnabled", "10K", "22K", "32K",
    "70K", "90K", "136K", "230K", "1M",
)

# Device-declared factory default: PropertyDef(default=1) == "First Enabled".
IMPEDANCE_DEFAULT = "FirstEnabled"

_IMPEDANCE_INDEX = {v: i for i, v in enumerate(IMPEDANCE_VALUES)}


def impedance_device_int(value: str) -> int:
    """Device ``preset.instN.z`` int for a `.hsp` impedance string.

    The device int is the enum index (device-self-described). An unknown
    string falls back to the factory default index (1 = First Enabled),
    with a stderr warning (mirrors `view`'s unknown-instNZ warning).
    """
    idx = _IMPEDANCE_INDEX.get(value)
    if idx is None:
        import sys
        print(f"warning: unrecognized impedance {value!r}; using the device "
              f"default {IMPEDANCE_DEFAULT!r} (int "
              f"{_IMPEDANCE_INDEX[IMPEDANCE_DEFAULT]}).", file=sys.stderr)
        return _IMPEDANCE_INDEX[IMPEDANCE_DEFAULT]
    return idx


def validate_impedance(value: Any) -> None:
    """Raise ValueError unless `value` is a valid impedance string."""
    if not isinstance(value, str) or value not in _IMPEDANCE_INDEX:
        raise ValueError(
            f"invalid impedance {value!r}; valid values: {list(IMPEDANCE_VALUES)} "
            f"(\"FirstBlock\"/\"FirstEnabled\" are the auto modes)."
        )


# --- input endpoint (b00) -----------------------------------------------------

# Modeled `.hsp` slot params + device-defs defaults (input models 769/770/771/
# 774). Pad exists only on instrument-jack models; StereoLink only on the
# stereo ("both") model — handled by callers via `jacks_for_mode`.
INPUT_HSP_DEFAULTS: dict[str, Any] = {
    "Pad": 1,            # int enum: 1 = off, 2 = on
    "Trim": 0.0,         # dB
    "noiseGate": False,
    "threshold": -48.0,  # dB
    "decay": 0.1,
}
STEREO_LINK_DEFAULT = False

# recipe field -> (hsp param name, kind, min, max). kind: "bool" | "float".
# "pad" is a recipe bool that maps to the int enum 2 (on) / 1 (off).
INPUT_FIELD_SPECS: dict[str, tuple[str, str, float | None, float | None]] = {
    "pad":       ("Pad",        "bool",  None, None),
    "trim":      ("Trim",       "float", -24.0, 6.0),
    "gate":      ("noiseGate",  "bool",  None, None),
    "threshold": ("threshold",  "float", -96.0, 0.0),
    "decay":     ("decay",      "float", 0.01, 1.0),
    "link":      ("StereoLink", "bool",  None, None),
}


def _check_kind(name: str, value: Any, kind: str,
                lo: float | None, hi: float | None) -> None:
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f'"{name}" must be a boolean (got {value!r}).')
        return
    # float
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'"{name}" must be a number (got {value!r}).')
    if lo is not None:
        # Real exports store float32 values that can land a hair outside the
        # nominal range (e.g. decay 0.01 stored as 0.009999999776…), so range
        # checks carry a small relative epsilon.
        tol = 1e-4 * max(1.0, abs(lo), abs(hi))
        if not (lo - tol <= float(value) <= hi + tol):
            raise ValueError(
                f'"{name}" must be between {lo} and {hi} (got {value!r}).')


def validate_input_field(field: str, value: Any) -> None:
    """Validate one scalar recipe input field (pad/trim/gate/threshold/decay/link)."""
    hsp_name, kind, lo, hi = INPUT_FIELD_SPECS[field]
    _check_kind(field, value, kind, lo, hi)


def input_field_to_hsp(field: str, value: Any) -> tuple[str, Any]:
    """Map a validated recipe input field to its `(hsp_param, hsp_value)`."""
    hsp_name, kind, _lo, _hi = INPUT_FIELD_SPECS[field]
    if field == "pad":
        return hsp_name, (2 if value else 1)
    if kind == "float":
        return hsp_name, float(value)
    return hsp_name, bool(value)


DEFAULT_INPUT_MODES = ("both", "none")  # by path index (Path 1, Path 2)


def default_input_mode(path_index: int) -> str:
    """Default logical input source for a DSP path when the recipe omits it."""
    if path_index < len(DEFAULT_INPUT_MODES):
        return DEFAULT_INPUT_MODES[path_index]
    return "none"


def jacks_for_mode(mode: str) -> tuple[str, ...]:
    """The instrument jack(s) a logical input mode listens to."""
    return {
        "inst1": ("inst1",),
        "inst2": ("inst2",),
        "both": ("inst1", "inst2"),
    }.get(mode, ())


# --- output endpoint (lane-0 b13) ---------------------------------------------

OUTPUT_FIELD_TO_HSP = {"level": "gain", "pan": "pan"}
OUTPUT_HSP_DEFAULTS = {"gain": 0.0, "pan": 0.5}
_OUTPUT_RANGES = {"level": (-120.0, 20.0), "pan": (0.0, 1.0)}


def validate_output_field(field: str, value: Any) -> None:
    """Validate one recipe output field (level/pan)."""
    lo, hi = _OUTPUT_RANGES[field]
    _check_kind(field, value, "float", lo, hi)


# --- split / join (wire param schemas) ----------------------------------------

SPLIT_TYPES: dict[str, str] = {
    "y": "P35_AppDSPSplitY",
    "ab": "P35_AppDSPSplitAB",
    "crossover": "P35_AppDSPSplitXOver",
    "dynamic": "P35_AppDSPSplitDyn",
}
SPLIT_MODEL_TO_TYPE = {m: t for t, m in SPLIT_TYPES.items()}

JOIN_MODEL = "P35_AppDSPJoin"

# {param: (kind, min, max, default)} — from device defs, corpus-corroborated.
SPLIT_PARAM_SCHEMAS: dict[str, dict[str, tuple]] = {
    "P35_AppDSPSplitY": {
        "BalanceA": ("float", 0.0, 1.0, 0.5),
        "BalanceB": ("float", 0.0, 1.0, 0.5),
        "enable":   ("bool", None, None, True),
    },
    "P35_AppDSPSplitAB": {
        "RouteTo": ("float", 0.0, 1.0, 0.5),
        "enable":  ("bool", None, None, True),
    },
    "P35_AppDSPSplitXOver": {
        "Frequency": ("float", 25.0, 15000.0, 500.0),
        "Reverse":   ("bool", None, None, False),
        "enable":    ("bool", None, None, True),
    },
    "P35_AppDSPSplitDyn": {
        "Threshold": ("float", -60.0, 0.0, -15.0),
        "Attack":    ("float", 0.05, 5.0, 0.86),
        "Decay":     ("float", 0.05, 5.0, 0.86),
        "Reverse":   ("bool", None, None, False),
        "enable":    ("bool", None, None, True),
    },
}

JOIN_PARAM_SCHEMA: dict[str, tuple] = {
    "A Level":    ("float", -60.0, 12.0, 0.0),
    "A Pan":      ("float", 0.0, 1.0, 0.5),
    "B Level":    ("float", -60.0, 12.0, 0.0),
    "B Pan":      ("float", 0.0, 1.0, 0.5),
    "B Polarity": ("bool", None, None, False),
    # NB: the device default for the master merge Level is +3 dB (defs), not
    # unity — an authored join that OMITS "Level" gets +3 dB filled in by
    # the device/transcode defaults. Set "Level": 0.0 explicitly for unity.
    "Level":      ("float", -60.0, 12.0, 3.0),
}

_WIRE_SCHEMAS = {**SPLIT_PARAM_SCHEMAS, JOIN_MODEL: JOIN_PARAM_SCHEMA}


def wire_param_schema(model: str) -> dict[str, tuple] | None:
    """The `{param: (kind, min, max, default)}` schema for a split/join model,
    or None for a model we have no schema for (validated permissively)."""
    return _WIRE_SCHEMAS.get(model)


def coerce_wire_params(model: str, params: dict[str, Any]) -> dict[str, Any]:
    """Coerce validated split/join wire params to their schema kinds — an int
    given for a float param becomes a float (the same int-for-float guard
    `generate._coerce_param_value` applies to block params; a raw int in a
    float slot can corrupt the block on-device). Unknown models/params pass
    through unchanged."""
    schema = wire_param_schema(model)
    if schema is None:
        return dict(params)
    out: dict[str, Any] = {}
    for k, v in params.items():
        kind = schema[k][0] if k in schema else None
        if kind == "float" and isinstance(v, int) and not isinstance(v, bool):
            v = float(v)
        out[k] = v
    return out


def validate_wire_params(model: str, params: dict[str, Any]) -> None:
    """Validate split/join wire params against the model's schema.

    Unknown models pass through untouched (forward-compat with future device
    routing models); known models reject unknown names (listing the valid
    set), wrong kinds, and out-of-range numbers.
    """
    schema = wire_param_schema(model)
    if schema is None:
        return
    for name, value in params.items():
        if name not in schema:
            raise ValueError(
                f"unknown param {name!r} for {model}; "
                f"valid params: {sorted(schema)}."
            )
        kind, lo, hi, _default = schema[name]
        _check_kind(name, value, kind, lo, hi)


# --- trails scope ---------------------------------------------------------------

_FXLOOP_MODEL_PREFIX = "HD2_FXLoop"


def trails_capable(category: str | None, model_id: str | None) -> bool:
    """True when a block can carry the author-facing `trails` field: delay,
    reverb, or an FX-Loop block (device manual: FX Loop blocks have a Trails
    param; Send-/Return-only blocks do not)."""
    if category in ("delay", "reverb"):
        return True
    return bool(model_id) and str(model_id).startswith(_FXLOOP_MODEL_PREFIX)
