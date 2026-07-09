"""In-place mutation verbs for a parsed `.hsp` body dict.

This is the heart of the `.hsp`-canonical redesign
(`docs/superpowers/plans/2026-07-08-hsp-canonical-redesign.md`): instead of
compiling a spec into a fresh `.hsp` body, we address a block already placed
in `preset.flow[*].bNN` and mutate its `slot` dict directly, in place.

Block addressing mirrors `patch.resolve_block`'s disambiguation semantics
(display name, optionally narrowed by `path`/`lane`/`pos`) but resolves
against the `.hsp` body's `preset.flow` structure instead of a spec dict, and
returns a `(flow_index, bnn_key, slot_index)` coordinate rather than a
`(path_index, block_index)` one. `slot_index` is always `0` today — the only
addressable slot in a `bNN` entry is `slot[0]`; a dual-cab's second physical
slot (`slot[1]`) is opaque verbatim state (see `decompile._block_entry`'s
`raw.slots`) and is not independently addressable here.

More verbs (`set_enabled`, `add_block`, controller wiring, ...) land in this
same module in later phases of the redesign; keep additions here rather than
spawning new modules per verb.
"""
from __future__ import annotations

from typing import Any

from helixgen.generate import (
    HSP_SNAPSHOT_SLOTS,
    ParamValidationError,
    _coerce_param_value,
    _is_stereo_param,
    validate_params,
)
from helixgen.hsp import CHASSIS_MODEL_PREFIX, ENDPOINT_KEYS, _translate_model_id
from helixgen.library import Block, Library

__all__ = ["MutateError", "resolve_slot", "set_param", "set_enabled"]


class MutateError(ValueError):
    """A `.hsp` body-level mutation could not be applied (bad address, etc.)."""


def _bnn_keys(path_dict: dict[str, Any]) -> list[str]:
    """Sorted user-block keys (`b01`..`b12`) in a flow path dict, endpoints excluded."""
    return sorted(
        k for k in path_dict
        if isinstance(k, str) and k.startswith("b") and k not in ENDPOINT_KEYS and k[1:].isdigit()
    )


def _lane_pos(key: str) -> tuple[int, int]:
    """Decode a `bNN` key into (lane, pos): lane 1 starts at b14, lane 0 is b01-b13."""
    num = int(key[1:])
    lane = 1 if num >= 14 else 0
    return lane, num - 14 * lane


def _iter_slots(
    body: dict[str, Any], library: Library
) -> list[tuple[int, str, int, Block, int, int]]:
    """Walk `preset.flow[*]` and return every resolvable user block's primary
    (index-0) slot as `(flow_index, bnn_key, slot_index, block, lane, pos)`.

    Skips `b00`/`b13` endpoints, split/join/input/output structural slots,
    `P35_` chassis-routing models, and any slot whose model the library
    cannot resolve (mirrors `decompile._name_index`'s skip-on-KeyError).
    """
    flow = (body.get("preset") or {}).get("flow") or []
    out: list[tuple[int, str, int, Block, int, int]] = []
    for fi, path_dict in enumerate(flow):
        if not isinstance(path_dict, dict):
            continue
        for key in _bnn_keys(path_dict):
            bnn = path_dict.get(key)
            if not isinstance(bnn, dict) or bnn.get("type") in ("split", "join", "input", "output"):
                continue
            slots = bnn.get("slot")
            if not slots or not isinstance(slots, list):
                continue
            slot0 = slots[0]
            if not isinstance(slot0, dict) or "model" not in slot0:
                continue
            model = slot0["model"]
            if isinstance(model, str) and model.startswith(CHASSIS_MODEL_PREFIX):
                continue
            try:
                block = library.load_block(_translate_model_id(model))
            except KeyError:
                continue
            lane, pos = _lane_pos(key)
            out.append((fi, key, 0, block, lane, pos))
    return out


def resolve_slot(
    body: dict[str, Any],
    name: str,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> tuple[int, str, int]:
    """Resolve a display name (or model_id) to a `(flow_index, bnn_key, slot_index)`
    coordinate in `body`, mirroring `patch.resolve_block`'s disambiguation.

    `name` matches a placed block's `display_name` or `model_id`. `path`/
    `lane`/`pos` narrow the match when the name is ambiguous. Raises
    `MutateError` if no block matches (message lists every placed block) or
    if more than one does (message says to disambiguate).
    """
    placed = _iter_slots(body, library)
    name_matches = [t for t in placed if name in (t[3].display_name, t[3].model_id)]

    matches = name_matches
    if path is not None:
        matches = [t for t in matches if t[0] == path]
    if lane is not None:
        matches = [t for t in matches if t[4] == lane]
    if pos is not None:
        matches = [t for t in matches if t[5] == pos]

    if not matches:
        placed_names = [t[3].display_name for t in placed]
        raise MutateError(
            f"Block {name!r} is not in the preset (with the given path/lane/pos). "
            f"Placed blocks: {placed_names}."
        )
    if len(matches) > 1:
        raise MutateError(
            f"Block {name!r} matches {len(matches)} placements; "
            f"disambiguate with path=/lane=/pos=."
        )
    fi, key, si, _block, _lane, _pos = matches[0]
    return (fi, key, si)


def _slot_dict(body: dict[str, Any], fi: int, key: str, si: int) -> dict[str, Any]:
    return body["preset"]["flow"][fi][key]["slot"][si]


def set_param(
    body: dict[str, Any],
    block: str,
    param: str,
    value: Any,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> None:
    """Set one param on one block, in place, preserving the wrapper shape.

    Validates `param` against the library schema (`generate.validate_params`,
    raising `ParamValidationError` for an unknown name) and coerces `value`
    to the schema's declared type (`generate._coerce_param_value` — an int
    given for a float-schema param becomes a float, matching the guard
    `generate._to_hsp_bnn` already applies, since a raw int there can
    silently brick the block on-device).

    Writes into the existing `params[param]` wrapper:
      - plain `{"value": x}` — updates `value`.
      - controlled `{"controller": {...}, "value": x}` — updates `value`,
        leaves `controller` untouched.
      - stereo `{"1": {"value": x}, "2": {"value": y}}` — updates both
        channels' `value`.
      - missing entirely — creates a plain `{"value": x}` wrapper.
    """
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    slot = _slot_dict(body, fi, key, si)

    lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
    validate_params(lib_block, {param: value})
    coerced = _coerce_param_value(lib_block, param, value)

    params = slot.setdefault("params", {})
    wrapped = params.get(param)
    if isinstance(wrapped, dict) and _is_stereo_param(wrapped):
        for channel in ("1", "2"):
            chan = wrapped.get(channel)
            if isinstance(chan, dict):
                chan["value"] = coerced
            else:
                wrapped[channel] = {"value": coerced}
    elif isinstance(wrapped, dict):
        wrapped["value"] = coerced
    else:
        params[param] = {"value": coerced}


# --- set_enabled -----------------------------------------------------------

def _active_snapshot(body: dict[str, Any]) -> int:
    """The device's on-load snapshot index (`preset.params.activesnapshot`,
    defaulting to 0 when absent — matches `generate._compose_preset_hsp`,
    which always writes 0)."""
    params = (body.get("preset") or {}).get("params") or {}
    value = params.get("activesnapshot", 0)
    return value if isinstance(value, int) else 0


def _resolve_snapshot_index(body: dict[str, Any], snapshot: Any) -> int:
    """Resolve a snapshot name (matched against `preset.snapshots[*].name`)
    or a bare int index to an index into the 8-slot snapshot arrays."""
    if isinstance(snapshot, int) and not isinstance(snapshot, bool):
        return snapshot
    meta = (body.get("preset") or {}).get("snapshots") or []
    for i, s in enumerate(meta):
        if isinstance(s, dict) and s.get("name") == snapshot:
            return i
    names = [s.get("name") for s in meta if isinstance(s, dict)]
    raise MutateError(f"Snapshot {snapshot!r} not found. Known snapshots: {names}.")


def set_enabled(
    body: dict[str, Any],
    block: str,
    enabled: bool,
    library: Library,
    *,
    snapshot: Any = None,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> None:
    """Bypass-toggle one block, in place, at the `bNN`-level `@enabled`
    wrapper (device-validated: Stadium reads bypass there, NOT at the
    slot-level `@enabled`, which stays untouched).

    `snapshot=None` (default) sets the base `@enabled.value` directly.

    `snapshot=<name-or-index>` instead flips that one slot of the 8-element
    `@enabled.snapshots` array (resolved by matching `preset.snapshots[*].name`,
    or used directly if already an int). Any other `null` slot in the array is
    densified to the pre-edit base value — a sparse (null-containing) array
    left on-device snapshot recall unreliable (see 0.5.1). After a snapshot
    edit, `@enabled.value` is re-synced to `snapshots[<active snapshot>]`
    (`preset.params.activesnapshot`) so the block shows its active-snapshot
    state on load, matching `generate._wrap_value_with_snapshots`.
    """
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    bnn = body["preset"]["flow"][fi][key]
    enabled = bool(enabled)

    wrapped = bnn.get("@enabled")
    if not isinstance(wrapped, dict):
        wrapped = {"value": True}
        bnn["@enabled"] = wrapped

    if snapshot is None:
        wrapped["value"] = enabled
        return

    idx = _resolve_snapshot_index(body, snapshot)
    base = wrapped.get("value", True)
    snaps = wrapped.get("snapshots")
    snaps = list(snaps) if isinstance(snaps, list) else [None] * HSP_SNAPSHOT_SLOTS
    if len(snaps) < HSP_SNAPSHOT_SLOTS:
        snaps.extend([None] * (HSP_SNAPSHOT_SLOTS - len(snaps)))
    if not (0 <= idx < len(snaps)):
        raise MutateError(f"Snapshot index {idx} out of range (0..{len(snaps) - 1}).")

    snaps[idx] = enabled
    snaps = [base if s is None else s for s in snaps]  # densify
    wrapped["snapshots"] = snaps
    wrapped["value"] = snaps[_active_snapshot(body)]
