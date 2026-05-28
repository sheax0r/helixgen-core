"""Per-device tables for input endpoints and controller (FS/EXP) source IDs.

These tables are empirically derived from real .hsp exports. The outer key
is the chassis's `meta.device_id` (with `stadium_xl` as the canonical alias
used as fallback when the device_id is missing or unrecognized). Inner keys
are the logical names the spec uses.
"""
from __future__ import annotations


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


def _resolve_device(device_id: str) -> str:
    """Pick the active device table key, falling back to stadium_xl."""
    if device_id in INPUT_MODELS:
        return device_id
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
