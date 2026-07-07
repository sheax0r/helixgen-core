"""Per-device tables for input endpoints and controller (FS/EXP) source IDs.

These tables are empirically derived from real .hsp exports. The outer key
is the chassis's `meta.device_id` (with `stadium_xl` as the canonical alias
used as fallback when the device_id is missing or unrecognized). Inner keys
are the logical names the spec uses.
"""
from __future__ import annotations

import sys


class ControllerError(ValueError):
    """Raised when a logical input/FS/EXP name cannot be resolved."""


INPUT_MODELS: dict[str, dict[str, str]] = {
    "stadium_xl": {
        "inst1": "P35_InputInst1",
        "inst2": "P35_InputInst2",
        "both":  "P35_InputInst1_2",
        "none":  "P35_InputNone",
    },
}


# Empirically derived from the user's real .hsp exports (see
# scripts/derive_controller_table.py). FS1..FS10 are Stadium XL's 10
# physical stomp-mode footswitches; their source IDs follow 0x010101NN.
# FS6 (0x01010105) had no assignments in the scanned exports but the
# contiguous pattern is unambiguous. A source ID 0x0101010a was observed
# in data (an 11th stomp / MODE-switch context) but is out of scope for v1.
CONTROLLER_SOURCE_IDS: dict[str, dict[str, int]] = {
    "stadium_xl": {
        "FS1":  0x01010100,
        "FS2":  0x01010101,
        "FS3":  0x01010102,
        "FS4":  0x01010103,
        "FS5":  0x01010104,
        "FS6":  0x01010105,
        "FS7":  0x01010106,
        "FS8":  0x01010107,
        "FS9":  0x01010108,
        "FS10": 0x01010109,
        # Expression pedals (derived empirically from data/*.hsp);
        # both wrap Pedal-position params with source IDs in the 0x010201NN
        # range (distinct from the 0x010101NN FS range).
        # 0x01020102 was seen in 2 files only — likely a 3rd EXP slot, out of
        # scope for v1. EXPONBOARD was not observed in the scanned exports.
        "EXP1": 0x01020100,
        "EXP2": 0x01020101,
        # The onboard expression pedal's toe switch (the click switch under the
        # pedal, engaged by pushing it fully forward). This is the standard wah
        # auto-engage: bypass toggles here while EXP1 sweeps the pedal. Source
        # 0x01010500 is observed on ~all real wah exports (198 occurrences in
        # data/*.hsp); it sits in its own 0x010105NN bank, distinct from both
        # the FS range (0x010101NN) and the EXP-position range (0x010201NN).
        "EXP1Toe": 0x01010500,
    },
}


# Observed `meta.device_id` values that identify Stadium XL hardware. Real
# exports carry a numeric id (e.g. 2490368), not the canonical string —
# this set lets us recognise those without warning. Add new values as they
# are observed in the field.
STADIUM_XL_DEVICE_IDS: frozenset = frozenset({"stadium_xl", 2490368})


_warned_devices: set = set()  # de-dup warnings within a single process


def _resolve_device(device_id) -> str:
    """Pick the active device table key, falling back to stadium_xl.

    Both INPUT_MODELS and CONTROLLER_SOURCE_IDS use the same outer keys
    (currently only "stadium_xl"). _resolve_device is shared by both
    resolve_input_model and resolve_controller_source — keep the outer
    keys of both tables in sync when adding new device support.

    Accepts both the canonical string key and the numeric `meta.device_id`
    values real chassis exports carry; numeric aliases for known hardware
    are listed in STADIUM_XL_DEVICE_IDS.
    """
    if isinstance(device_id, str) and device_id in INPUT_MODELS and device_id in CONTROLLER_SOURCE_IDS:
        return device_id
    if device_id in STADIUM_XL_DEVICE_IDS:
        return "stadium_xl"
    if device_id is not None and device_id not in _warned_devices:
        print(
            f"warning: chassis device_id {device_id!r} not in controller tables; "
            f"assuming stadium_xl.",
            file=sys.stderr,
        )
        _warned_devices.add(device_id)
    return "stadium_xl"


def resolve_input_model(device_id: str, mode: str) -> str:
    """Look up the Stadium model_id for a logical input mode.

    Raises ControllerError listing valid modes if the mode is unknown.
    """
    table = INPUT_MODELS[_resolve_device(device_id)]
    if mode not in table:
        raise ControllerError(
            f"Unknown input mode {mode!r}. Valid modes: {sorted(table.keys())}."
        )
    return table[mode]


def resolve_controller_source(device_id: str, logical_name: str) -> int:
    """Look up a controller source ID for a logical FS/EXP name.

    Raises ControllerError listing valid names if the logical name is unknown.
    """
    table = CONTROLLER_SOURCE_IDS[_resolve_device(device_id)]
    if logical_name not in table:
        raise ControllerError(
            f"Unknown controller name {logical_name!r}. "
            f"Valid names: {sorted(table.keys())}."
        )
    return table[logical_name]


def input_mode_for_model(device_id, model: str) -> str | None:
    """Reverse of resolve_input_model: Stadium input model_id → logical mode."""
    table = INPUT_MODELS[_resolve_device(device_id)]
    for mode, model_id in table.items():
        if model_id == model:
            return mode
    return None


def controller_name_for_source(device_id, source_id: int) -> str | None:
    """Reverse of resolve_controller_source: source id → logical FS/EXP name."""
    table = CONTROLLER_SOURCE_IDS[_resolve_device(device_id)]
    for name, sid in table.items():
        if sid == source_id:
            return name
    return None
