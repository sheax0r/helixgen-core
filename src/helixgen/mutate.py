"""In-place mutation verbs for a parsed `.hsp` body dict.

This is the heart of the `.hsp`-canonical redesign: instead of
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

from helixgen import controllers, flowparams
from helixgen.controllers import ControllerError
from helixgen.generate import (
    HSP_SNAPSHOT_SLOTS,
    GenerateError,
    _build_exp_controller,
    _build_fs_controller,
    _build_fs_param_controller,
    _chassis_device_id,
    _coerce_param_value,
    _is_stereo_param,
    _resolve_irhash,
    _rewrite_input_endpoint,
    _to_hsp_bnn,
    validate_params,
)
from helixgen.hsp import (
    CHASSIS_MODEL_PREFIX,
    ENDPOINT_KEYS,
    LOOPER_MODEL_PREFIX,
    _translate_model_id,
    translate_to_hsp,
)
from helixgen.ir import IR_MODEL_PREFIX
from helixgen.library import Block, Library

__all__ = [
    "MutateError",
    "resolve_slot",
    "set_param",
    "set_flow_param",
    "set_enabled",
    "add_block",
    "remove_block",
    "swap_model",
    "set_ir",
    "set_trails",
    "set_input",
    "wire_footswitch",
    "wire_expression",
    "wire_wah_toe",
]

_MAX_LANE_SLOTS = 12  # b01..b12 user-block slots per lane

# Signal-flow pseudo-block names accepted by `set_param` (routed to
# `set_flow_param`). These address a path's endpoints / split / merge mixer
# rather than a library block; the names win over any same-named library
# block by design (display names are humanized model titles, so a collision
# does not occur in practice).
_FLOW_PSEUDO_BLOCKS = ("input", "output", "split", "join", "merge")


class MutateError(ValueError):
    """A `.hsp` body-level mutation could not be applied (bad address, etc.)."""


def _bnn_keys(path_dict: dict[str, Any]) -> list[str]:
    """Sorted user-block keys (`b01`..`b12`) in a flow path dict, endpoints excluded."""
    return sorted(
        k for k in path_dict
        if isinstance(k, str) and k.startswith("b") and k not in ENDPOINT_KEYS and k[1:].isdigit()
    )


def _lane_pos(key: str) -> tuple[int, int]:
    """Decode a `bNN` key into (lane, pos): num >= 14 decodes as lane 1 (pos =
    num - 14), num < 14 as lane 0. Note `generate._assign_positions` numbers
    lane 1 starting at pos=1 (i.e. the first lane-1 key is `b15`) -- `b14`
    itself is never assigned by the generator, though it decodes as lane 1
    pos 0 here if ever encountered on read."""
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
            # Skip P35_ chassis-routing models, but NOT loopers
            # (`P35_LooperHelix*`), which are real user blocks in the library
            # and can carry a footswitch (mirrors `hsp.extract_blocks_from_hsp`).
            if (isinstance(model, str) and model.startswith(CHASSIS_MODEL_PREFIX)
                    and not model.startswith(LOOPER_MODEL_PREFIX)):
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
    snapshot: Any = None,
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

    `snapshot=None` (default) writes into the existing `params[param]` wrapper:
      - plain `{"value": x}` — updates `value`.
      - controlled `{"controller": {...}, "value": x}` — updates `value`,
        leaves `controller` untouched.
      - stereo `{"1": {"value": x}, "2": {"value": y}}` — updates both
        channels' `value`.
      - missing entirely — creates a plain `{"value": x}` wrapper.

    `snapshot=<name-or-index>` (resolved by `_resolve_snapshot_index`) instead
    sets that one slot of the wrapper's 8-element per-snapshot `snapshots`
    overrides array (see `_write_snapshot_slot` for the densify/re-sync
    contract). The wrapper must already carry a base `value` (there is no
    base to densify the other slots to otherwise); an existing `controller`
    is left untouched (a param can be controller-driven AND snapshot-tracked
    on the device); stereo-shaped params are not supported.

    The exact names ``input`` / ``output`` / ``split`` / ``join`` / ``merge``
    are signal-flow PSEUDO-BLOCKS and route to :func:`set_flow_param` (they
    address the path's endpoints / split / merge mixer, not a library block);
    those names win over any same-named library block by design. Per-snapshot
    values on pseudo-blocks are supported only for ``output`` (level/pan).
    """
    if block in _FLOW_PSEUDO_BLOCKS:
        if lane is not None:
            raise MutateError(
                f"{block!r} is a signal-flow pseudo-block; 'lane' does not "
                f"apply (address it with path=/pos=)."
            )
        set_flow_param(body, block, param, value, path=path or 0, pos=pos,
                       snapshot=snapshot)
        return
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    slot = _slot_dict(body, fi, key, si)

    lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
    validate_params(lib_block, {param: value})
    coerced = _coerce_param_value(lib_block, param, value)

    params = slot.setdefault("params", {})
    wrapped = params.get(param)
    if snapshot is not None:
        if isinstance(wrapped, dict) and _is_stereo_param(wrapped):
            raise MutateError(
                f"Param {block!r}.{param!r} is stereo-shaped; stereo params "
                f"are not supported for per-snapshot values."
            )
        if not isinstance(wrapped, dict):
            raise MutateError(
                f"Block {block!r} has no existing value for param {param!r}; "
                f"set the base value first (per-snapshot overrides densify "
                f"the untouched slots to the base)."
            )
        idx = _resolve_snapshot_index(body, snapshot)
        _write_snapshot_slot(body, wrapped, idx, coerced)
        return
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


# --- set_flow_param (input/output/split/join pseudo-blocks) ------------------

def _flow_path_dict(body: dict[str, Any], path: int) -> dict[str, Any]:
    flow = (body.get("preset") or {}).get("flow") or []
    if not (0 <= path < len(flow)) or not isinstance(flow[path], dict):
        raise MutateError(
            f"Path {path} not in body flow (flow has {len(flow)} path(s)).")
    return flow[path]


def _write_slot_param(slot: dict[str, Any], name: str, value: Any) -> None:
    """Write one slot param preserving the wrapper shape (plain / controlled /
    stereo per-channel), mirroring `set_param`'s write semantics."""
    params = slot.setdefault("params", {})
    wrapped = params.get(name)
    if isinstance(wrapped, dict) and _is_stereo_param(wrapped):
        for channel in ("1", "2"):
            chan = wrapped.get(channel)
            if isinstance(chan, dict):
                chan["value"] = value
            else:
                wrapped[channel] = {"value": value}
    elif isinstance(wrapped, dict):
        wrapped["value"] = value
    else:
        params[name] = {"value": value}


def _set_input_flow_param(body: dict[str, Any], path_dict: dict[str, Any],
                          param: str, value: Any) -> None:
    b00 = path_dict.get("b00")
    if not (isinstance(b00, dict) and b00.get("slot")):
        raise MutateError("Path has no b00 input endpoint.")
    slot = b00["slot"][0]
    device_id = _chassis_device_id(body)
    mode = controllers.input_mode_for_model(device_id, slot.get("model", ""))

    if param == "impedance":
        if not isinstance(value, str):
            raise MutateError('"impedance" takes a string (e.g. "1M", '
                              '"FirstEnabled").')
        try:
            flowparams.validate_impedance(value)
        except ValueError as exc:
            raise MutateError(str(exc)) from exc
        jacks = flowparams.jacks_for_mode(mode or "")
        if not jacks:
            raise MutateError(
                f"input source {mode!r} uses no instrument jack; impedance "
                f"does not apply.")
        params = body.setdefault("preset", {}).setdefault("params", {})
        for jack in jacks:
            params[f"{jack}Z"] = value
        return

    if param not in flowparams.INPUT_FIELD_SPECS:
        raise MutateError(
            f"unknown input param {param!r}; valid params: "
            f"{['impedance', *flowparams.INPUT_FIELD_SPECS]}.")
    if param == "link" and mode != "both":
        raise MutateError('"link" (StereoLink) applies only to the stereo '
                          '"both" input.')
    if param == "pad" and mode in (None, "none"):
        raise MutateError('"pad" requires an instrument input source '
                          '(inst1/inst2/both).')
    try:
        flowparams.validate_input_field(param, value)
    except ValueError as exc:
        raise MutateError(str(exc)) from exc
    hsp_name, hsp_value = flowparams.input_field_to_hsp(param, value)
    _write_slot_param(slot, hsp_name, hsp_value)


# Device defaults for the output endpoint's params, used as the densify base
# when a per-snapshot override is written before any base value exists
# (`gain` def 0.0 dB / `pan` def 0.5, from the vendored device defs for
# P35_OutputMatrix — a chassis-fresh b13 often carries no wrapper at all).
_OUTPUT_HSP_DEFAULTS = {"gain": 0.0, "pan": 0.5}


def _set_output_flow_param(
    body: dict[str, Any], path_dict: dict[str, Any], param: str, value: Any,
    *, snapshot: Any = None,
) -> None:
    b13 = path_dict.get("b13")
    if not (isinstance(b13, dict) and b13.get("type") == "output"
            and b13.get("slot")):
        raise MutateError("Path has no lane-0 output endpoint (b13).")
    if param not in flowparams.OUTPUT_FIELD_TO_HSP:
        raise MutateError(f"unknown output param {param!r}; valid params: "
                          f"{sorted(flowparams.OUTPUT_FIELD_TO_HSP)}.")
    try:
        flowparams.validate_output_field(param, value)
    except ValueError as exc:
        raise MutateError(str(exc)) from exc
    hsp_name = flowparams.OUTPUT_FIELD_TO_HSP[param]
    if snapshot is not None:
        slot = b13["slot"][0]
        params = slot.setdefault("params", {})
        wrapped = params.get(hsp_name)
        if not isinstance(wrapped, dict):
            wrapped = {"value": _OUTPUT_HSP_DEFAULTS[hsp_name]}
            params[hsp_name] = wrapped
        idx = _resolve_snapshot_index(body, snapshot)
        _write_snapshot_slot(body, wrapped, idx, float(value))
        return
    _write_slot_param(b13["slot"][0], hsp_name, float(value))


def _set_split_join_param(path_dict: dict[str, Any], kind: str, param: str,
                          value: Any, pos: int | None) -> None:
    candidates = []
    for key in _bnn_keys(path_dict):
        bnn = path_dict.get(key)
        if isinstance(bnn, dict) and bnn.get("type") == kind and bnn.get("slot"):
            candidates.append((key, bnn))
    if pos is not None:
        candidates = [(k, b) for (k, b) in candidates if _lane_pos(k)[1] == pos]
    if not candidates:
        raise MutateError(
            f"Path has no {kind} block"
            + (f" at pos {pos}" if pos is not None else "") + ".")
    if len(candidates) > 1:
        raise MutateError(
            f"Path has {len(candidates)} {kind} blocks; disambiguate with pos= "
            f"(positions: {[_lane_pos(k)[1] for k, _ in candidates]}).")
    slot = candidates[0][1]["slot"][0]
    model = slot.get("model", "")
    try:
        flowparams.validate_wire_params(model, {param: value})
    except ValueError as exc:
        raise MutateError(str(exc)) from exc
    value = flowparams.coerce_wire_params(model, {param: value})[param]
    _write_slot_param(slot, param, value)


def set_flow_param(
    body: dict[str, Any],
    kind: str,
    param: str,
    value: Any,
    *,
    path: int = 0,
    pos: int | None = None,
    snapshot: Any = None,
) -> None:
    """Set one signal-flow param on a path's pseudo-block, in place.

    ``kind`` is one of the pseudo-block names ``input`` / ``output`` /
    ``split`` / ``join`` (``merge`` is an alias of ``join``):

    - ``input`` params use the recipe vocabulary — ``impedance`` (preset-level
      instNZ for the jacks the path's source uses), ``pad`` (bool → enum 2/1),
      ``trim``, ``gate`` (bool → noiseGate), ``threshold``, ``decay``,
      ``link`` (stereo only). Stereo inputs write both channels.
    - ``output`` params: ``level`` (dB → gain) and ``pan``, on the lane-0
      ``b13`` endpoint.
    - ``split``/``join`` params are the literal wire names (``BalanceA``,
      ``Frequency``, ``A Level``, …), validated against the placed model's
      schema; ``pos`` disambiguates when a path carries two split regions.

    ``snapshot=<name-or-index>`` writes that one slot of the param's 8-element
    per-snapshot overrides array instead of the base value (see
    `_write_snapshot_slot`). Supported only for ``output`` — the loudness
    phase-2 actuator (`docs/superpowers/specs/2026-07-14-loudness-feedback-
    normalization.md`); the transcoder synthesizes the matching device
    snapshot target from the b13 array. Per-snapshot input/split/join values
    have no transcoder support and are rejected.
    """
    if kind == "merge":
        kind = "join"
    if snapshot is not None and kind != "output":
        raise MutateError(
            f"per-snapshot values are supported only on the \"output\" "
            f"pseudo-block (level/pan), not {kind!r}."
        )
    path_dict = _flow_path_dict(body, path)
    if kind == "input":
        _set_input_flow_param(body, path_dict, param, value)
    elif kind == "output":
        _set_output_flow_param(body, path_dict, param, value, snapshot=snapshot)
    elif kind in ("split", "join"):
        _set_split_join_param(path_dict, kind, param, value, pos)
    else:
        raise MutateError(
            f"unknown flow pseudo-block {kind!r}; valid: {list(_FLOW_PSEUDO_BLOCKS)}.")


# --- set_enabled -----------------------------------------------------------

def _active_snapshot(body: dict[str, Any]) -> int:
    """The device's on-load snapshot index (`preset.params.activesnapshot`,
    defaulting to 0 when absent — matches `generate._compose_preset_hsp`,
    which always writes 0)."""
    params = (body.get("preset") or {}).get("params") or {}
    value = params.get("activesnapshot", 0)
    return value if isinstance(value, int) else 0


def _clamped_active_snapshot(body: dict[str, Any], length: int) -> int:
    """`_active_snapshot`, clamped to a valid index into a `length`-slot
    array. Guards `snaps[_active_snapshot(body)]` against `IndexError` if
    `preset.params.activesnapshot` ever points past a malformed/short
    snapshots array (`length` should normally be `HSP_SNAPSHOT_SLOTS`, but a
    caller may pass whatever array it actually has in hand)."""
    if length <= 0:
        raise MutateError("Cannot index an empty snapshots array.")
    return min(max(_active_snapshot(body), 0), length - 1)


def _resolve_snapshot_index(body: dict[str, Any], snapshot: Any) -> int:
    """Resolve a snapshot name (matched against `preset.snapshots[*].name`)
    or a bare int index to an index into the 8-slot snapshot arrays. A
    digit-only string that matches no snapshot NAME falls back to its int
    value (the CLI passes `--snapshot` values as strings), so `--snapshot 2`
    addresses index 2 unless a snapshot is literally named "2"."""
    if isinstance(snapshot, int) and not isinstance(snapshot, bool):
        return snapshot
    meta = (body.get("preset") or {}).get("snapshots") or []
    for i, s in enumerate(meta):
        if isinstance(s, dict) and s.get("name") == snapshot:
            return i
    if isinstance(snapshot, str) and snapshot.isdigit():
        return int(snapshot)
    names = [s.get("name") for s in meta if isinstance(s, dict)]
    raise MutateError(f"Snapshot {snapshot!r} not found. Known snapshots: {names}.")


def _write_snapshot_slot(
    body: dict[str, Any], wrapped: dict[str, Any], idx: int, value: Any
) -> None:
    """Write one slot of a param wrapper's 8-element `snapshots` overrides
    array, in place, then densify and re-sync.

    Any `null` slot is densified to the PRE-EDIT base value — param snapshot
    arrays densify to base, matching `generate._wrap_value_with_snapshots`
    (`@enabled` arrays densify to True instead; the two are deliberately
    different — see `set_enabled`). `wrapped["value"]` is re-synced to the
    active snapshot's slot (`preset.params.activesnapshot`) so the block
    shows its active-snapshot value on load.
    """
    if not (0 <= idx < HSP_SNAPSHOT_SLOTS):
        raise MutateError(
            f"Snapshot index {idx} out of range (0..{HSP_SNAPSHOT_SLOTS - 1}).")
    base = wrapped.get("value")
    snaps = wrapped.get("snapshots")
    snaps = list(snaps) if isinstance(snaps, list) else [None] * HSP_SNAPSHOT_SLOTS
    if len(snaps) < HSP_SNAPSHOT_SLOTS:
        snaps.extend([None] * (HSP_SNAPSHOT_SLOTS - len(snaps)))
    snaps[idx] = value
    snaps = [base if s is None else s for s in snaps]
    wrapped["snapshots"] = snaps
    wrapped["value"] = snaps[_clamped_active_snapshot(body, len(snaps))]


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
        snaps = wrapped.get("snapshots")
        if isinstance(snaps, list) and snaps:
            snaps[_clamped_active_snapshot(body, len(snaps))] = enabled
        return

    idx = _resolve_snapshot_index(body, snapshot)
    snaps = wrapped.get("snapshots")
    snaps = list(snaps) if isinstance(snaps, list) else [None] * HSP_SNAPSHOT_SLOTS
    if len(snaps) < HSP_SNAPSHOT_SLOTS:
        snaps.extend([None] * (HSP_SNAPSHOT_SLOTS - len(snaps)))
    if not (0 <= idx < len(snaps)):
        raise MutateError(f"Snapshot index {idx} out of range (0..{len(snaps) - 1}).")

    snaps[idx] = enabled
    # Densify null slots to True (an unset snapshot is ENABLED, independent of
    # the base value) -- matches `generate._to_hsp_bnn`'s @enabled fill. Filling
    # with the base value instead would wrongly bypass a base-bypassed block in
    # every untouched snapshot. (Param snapshots densify to base; @enabled does
    # NOT -- the two are deliberately different.)
    snaps = [True if s is None else s for s in snaps]
    wrapped["snapshots"] = snaps
    wrapped["value"] = snaps[_clamped_active_snapshot(body, len(snaps))]


# --- add_block / remove_block -----------------------------------------------

def _is_parallel_routed(path_dict: dict[str, Any]) -> bool:
    """True if `path_dict` contains a `split`/`join` structural block --
    i.e. this path has a parallel-routed lane 1 branch.

    `split`/`join` blocks live in lane 0 and carry `branch`/`endpoint` keys
    that cross-reference specific `bNN` keys by name (see module docstring).
    `_renumber_lane` rewrites `bNN` keys wholesale and knows nothing about
    those pointers, so running it on a parallel-routed path would silently
    corrupt the split/join wiring and desync lane 1's positions.
    """
    return any(
        isinstance(path_dict.get(k), dict) and path_dict[k].get("type") in ("split", "join")
        for k in _bnn_keys(path_dict)
        if _lane_pos(k)[0] == 0
    )


def _find_block(model: str, library: Library) -> Block:
    try:
        return library.find_block(model)
    except (KeyError, LookupError) as exc:
        raise MutateError(str(exc)) from exc


def _renumber_lane(path_dict: dict[str, Any], lane: int, ordered: list[dict[str, Any]]) -> dict[int, str]:
    """Replace every `bNN` entry in `path_dict` for `lane` with `ordered`
    (already in the desired final sequence), assigning sequential
    `position` (1-based) and `bNN` keys (`b01..b12` for lane 0, `b15..b25`
    for lane 1 -- lane 1 numbering starts at pos=1, i.e. `b15`; `b14` is
    never assigned, matching `generate._assign_positions`). Returns
    {index-in-ordered: new_key} for caller bookkeeping.
    """
    if len(ordered) > _MAX_LANE_SLOTS:
        raise MutateError(
            f"Lane {lane} would have {len(ordered)} blocks; only "
            f"{_MAX_LANE_SLOTS} user slots (b01..b{_MAX_LANE_SLOTS:02d}) available."
        )
    for k in _bnn_keys(path_dict):
        if _lane_pos(k)[0] == lane:
            del path_dict[k]

    new_keys: dict[int, str] = {}
    for i, bnn in enumerate(ordered, start=1):
        bnn["position"] = i
        new_key = f"b{14 * lane + i:02d}"
        path_dict[new_key] = bnn
        new_keys[i - 1] = new_key
    return new_keys


def _midi_records(body: dict[str, Any]) -> list:
    """The ``preset._helixgen_midi`` list, or ``[]`` when absent/malformed."""
    recs = (body.get("preset") or {}).get("_helixgen_midi")
    return recs if isinstance(recs, list) else []


def _remap_midi_positions(
    body: dict[str, Any], fi: int, lane: int,
    pos_map: dict[int, int], *, removed_pos: int | None = None,
) -> None:
    """Reconcile ``preset._helixgen_midi`` coordinates after a lane renumber.

    The MIDI records live OUTSIDE the block dicts (unlike FS/EXP controllers,
    which ride inside the block's own wrappers and survive `_renumber_lane`
    for free), so every renumbering path must remap them or the bindings go
    stale — `bridge._hsp_midi_by_coord` keys strictly on ``(path, lane, pos)``
    and a stale record is silently dropped on install/sync (or worse,
    mis-targets whatever lands on the old coordinate).

    ``pos_map`` maps each surviving block's OLD key-derived position to its
    NEW position (identity-based, so pre-existing key gaps in a raw device
    export compact correctly). ``removed_pos`` (if given) is the deleted
    block's old position: records targeting it are DROPPED with a stderr
    warning naming the CC. Records at a position in neither set were already
    dangling and are left untouched.
    """
    recs = _midi_records(body)
    if not recs:
        return
    import sys
    kept: list = []
    for rec in recs:
        if (isinstance(rec, dict) and rec.get("path") == fi
                and rec.get("lane") == lane
                and isinstance(rec.get("pos"), int)
                and not isinstance(rec.get("pos"), bool)):
            old = rec["pos"]
            if removed_pos is not None and old == removed_pos:
                what = ("bypass" if rec.get("param") is None
                        else f"param {rec.get('param')!r}")
                print(
                    f"warning: removed block {rec.get('block')!r} carried a "
                    f"MIDI CC {rec.get('cc')} {what} binding; binding dropped.",
                    file=sys.stderr,
                )
                continue
            if old in pos_map:
                rec["pos"] = pos_map[old]
        kept.append(rec)
    recs[:] = kept


def add_block(
    body: dict[str, Any],
    model: str,
    library: Library,
    *,
    path: int = 0,
    after: str | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    """Insert a new block into `body.preset.flow[path]`'s main (lane-0)
    chain, in place, and return its new `bNN` key.

    `model` resolves via `Library.find_block` (display name, alias, or
    model_id). The `bNN` skeleton is synthesized by `generate._to_hsp_bnn`
    (correct `type`/wrapped param defaults/`@enabled`) — reused rather than
    duplicated, per the Task 1a slot-skeleton decision.

    `after=None` appends to the end of the chain; `after="<name>"` inserts
    immediately following that block (resolved the same way `resolve_slot`
    resolves any other block reference, narrowed to this `path`/lane 0).
    Every block at or after the insertion point is renumbered — both its
    `position` and its `bNN` key — so key order keeps matching chain order
    (device- and decompile-relied-upon; see `decompile._bnn_keys`).
    """
    flow = (body.get("preset") or {}).get("flow") or []
    if not (0 <= path < len(flow)) or not isinstance(flow[path], dict):
        raise MutateError(f"Path {path} not in body flow (flow has {len(flow)} path(s)).")
    path_dict = flow[path]
    if _is_parallel_routed(path_dict):
        raise MutateError(
            "add_block not supported on a parallel-routed path yet (path "
            f"{path} contains a split/join)."
        )

    block = _find_block(model, library)
    lane = 0

    existing_keys = sorted(
        (k for k in _bnn_keys(path_dict) if _lane_pos(k)[0] == lane),
        key=lambda k: path_dict[k].get("position", _lane_pos(k)[1]),
    )
    ordered = [path_dict[k] for k in existing_keys]

    if after is None:
        insert_at = len(ordered)
    else:
        after_fi, after_key, _si = resolve_slot(body, after, library, path=path, lane=lane)
        insert_at = existing_keys.index(after_key) + 1

    new_bnn = _to_hsp_bnn(block, params or {}, position=0, path_index=lane)
    ordered.insert(insert_at, new_bnn)

    # Old key-derived position -> new 1-based position for every pre-existing
    # block (index j in existing_keys lands at j+1, +1 more past the insert).
    pos_map = {
        _lane_pos(k)[1]: (j + 1 if j < insert_at else j + 2)
        for j, k in enumerate(existing_keys)
    }
    new_keys = _renumber_lane(path_dict, lane, ordered)
    _remap_midi_positions(body, path, lane, pos_map)
    return new_keys[insert_at]


def remove_block(
    body: dict[str, Any],
    block: str,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> None:
    """Delete a placed block from `body`, in place, and renumber the
    `position`/`bNN` keys of every block that followed it in the same lane
    so key order keeps matching chain order.
    """
    fi, key, _si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    path_dict = body["preset"]["flow"][fi]
    if _is_parallel_routed(path_dict):
        raise MutateError(
            "remove_block not supported on a parallel-routed path yet (path "
            f"{fi} contains a split/join)."
        )
    del_lane, del_pos = _lane_pos(key)

    remaining_keys = sorted(
        (k for k in _bnn_keys(path_dict) if _lane_pos(k)[0] == del_lane and k != key),
        key=lambda k: path_dict[k].get("position", _lane_pos(k)[1]),
    )
    # Old key-derived position -> new sequential position (identity-based, so
    # pre-existing key gaps compact correctly), for the MIDI-record remap.
    pos_map = {_lane_pos(k)[1]: i for i, k in enumerate(remaining_keys, start=1)}
    ordered = [path_dict[k] for k in remaining_keys]
    _renumber_lane(path_dict, del_lane, ordered)
    _remap_midi_positions(body, fi, del_lane, pos_map, removed_pos=del_pos)


# --- swap_model --------------------------------------------------------------

def swap_model(
    body: dict[str, Any],
    old: str,
    new: str,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> list[str]:
    """Replace a placed block's model with another of the same category, in
    place, and return a list of human-readable warnings for anything that
    could not be carried over.

    Ports `patch.swap_model`'s semantics (same-category-only; carry params
    the target shares by name; warn on any dropped) onto an `.hsp` slot dict
    instead of a spec-json block entry. Because slots are already wrapped
    (`{"value": x}` / `{"controller": {...}, "value": x}`), a carried param
    keeps its controller assignment (e.g. an EXP-driven "Drive") intact --
    spec-level `patch.swap_model` has no such wrapper to preserve.

    `old` resolves like any other `resolve_slot` reference (narrowed by
    `path`/`lane`/`pos`); `new` resolves via `Library.find_block` (display
    name, alias, or model_id) but need not be placed anywhere.

    If the resolved slot carries an `irhash` and the target is not an IR
    block (`HX2_ImpulseResponse*`), the `irhash` is dropped with a warning
    -- an IR reference on a non-IR block is meaningless.
    """
    fi, key, si = resolve_slot(body, old, library, path=path, lane=lane, pos=pos)
    slot = _slot_dict(body, fi, key, si)

    old_block = library.load_block(_translate_model_id(slot.get("model", "")))
    new_block = _find_block(new, library)

    if old_block.category != new_block.category:
        raise MutateError(
            f"Cannot swap {old!r} ({old_block.category}) for {new!r} "
            f"({new_block.category}): categories differ."
        )

    warnings: list[str] = []
    old_params: dict[str, Any] = slot.get("params") or {}
    new_keys = set(new_block.params.keys())
    dropped = sorted(set(old_params) - new_keys)
    if dropped:
        warnings.append(
            f"swap {old!r}→{new!r}: dropped param(s) {dropped} not on target."
        )

    new_params: dict[str, Any] = {
        k: {"value": v} for k, v in new_block.exemplar.items() if k in new_block.params
    }
    new_params.update({k: v for k, v in old_params.items() if k in new_keys})

    slot["model"] = translate_to_hsp(new_block.model_id)
    slot["params"] = new_params

    if "irhash" in slot and not new_block.model_id.startswith(IR_MODEL_PREFIX):
        del slot["irhash"]
        warnings.append(f"swap {old!r}→{new!r}: dropped IR (target is not an IR block).")

    # Reconcile MIDI records targeting this coordinate (they live outside the
    # block dicts, unlike FS/EXP controllers which ride inside the param
    # wrappers and were carried above): a binding whose param the new model
    # lacks is dropped with a warning (same style as the dropped-param warning);
    # survivors get their stored block name refreshed so the record stays
    # self-consistent with what now sits at the coordinate.
    swap_lane, swap_pos = _lane_pos(key)
    recs = _midi_records(body)
    if recs:
        kept: list = []
        for rec in recs:
            if (isinstance(rec, dict) and rec.get("path") == fi
                    and rec.get("lane") == swap_lane and rec.get("pos") == swap_pos):
                pname = rec.get("param")
                if pname is not None and pname not in new_block.params:
                    warnings.append(
                        f"swap {old!r}→{new!r}: dropped MIDI CC {rec.get('cc')} "
                        f"binding on param {pname!r} not on target."
                    )
                    continue
                rec["block"] = new_block.display_name or new_block.model_id
            kept.append(rec)
        recs[:] = kept

    return warnings


# --- set_ir --------------------------------------------------------------

def set_ir(
    body: dict[str, Any],
    block: str,
    ir: str,
    library: Library,
    irs: Any,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> None:
    """Bind a registered IR to a placed IR block's `irhash`, in place.

    Ports `generate._resolve_irhash`'s resolution: `ir` may be a wav
    basename (looked up in `irs`' mapping) or a 32-char hex hash (used
    directly, with a stderr warning if unregistered -- the device may
    already hold it). Raises `MutateError` if `block` does not resolve to
    an `HX2_ImpulseResponse*` block, or if `ir` cannot be resolved.
    """
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    slot = _slot_dict(body, fi, key, si)

    lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
    if not lib_block.model_id.startswith(IR_MODEL_PREFIX):
        raise MutateError(
            f"Block {block!r} ({lib_block.category}) is not an IR block; "
            f"set_ir only applies to HX2_ImpulseResponse* blocks."
        )

    try:
        irhash = _resolve_irhash(block_default=None, spec_ir=ir, irs=irs)
    except GenerateError as exc:
        raise MutateError(str(exc)) from exc

    slot["irhash"] = irhash


# --- set_trails ------------------------------------------------------------

def set_trails(
    body: dict[str, Any],
    block: str,
    trails: bool,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> None:
    """Set a delay/reverb block's harness `Trails` (spillover) flag, in place.

    Ports the trails branch of `generate._to_hsp_bnn`: the value lives at
    `bNN.harness.params.Trails`, not on the slot. If the bNN has no verbatim
    harness yet, one is synthesized with the device constants observed
    across real exports; an existing harness has only its `Trails` entry
    overwritten. Raises `MutateError` for any block whose category is not
    `delay` or `reverb`.
    """
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    bnn = body["preset"]["flow"][fi][key]
    slot = bnn["slot"][si]

    lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
    if not flowparams.trails_capable(lib_block.category, lib_block.model_id):
        raise MutateError(
            f"Block {block!r} has category {lib_block.category!r}; trails "
            f"(harness spillover) applies only to delay, reverb, and FX-Loop "
            f"blocks."
        )

    trails = bool(trails)
    harness = bnn.get("harness")
    if not isinstance(harness, dict):
        bnn["harness"] = {
            "@enabled": {"value": True},
            "params": {
                "EvtIdx": {"value": -1},
                "Trails": {"value": trails},
                "bypass": {"value": False},
                "upper": {"value": True},
            },
        }
    else:
        params = harness.get("params")
        if not isinstance(params, dict):
            params = {}
            harness["params"] = params
        params["Trails"] = {"value": trails}


# --- set_input ---------------------------------------------------------------

def set_input(body: dict[str, Any], path: int, jack: str) -> None:
    """Rewrite one path's input-endpoint (`b00`) jack routing, in place.

    `jack` is a logical mode ("inst1"/"inst2"/"both"/"none"), resolved to a
    Stadium input model via `controllers.resolve_input_model` (keyed by the
    chassis's `meta.device_id`, defaulting to Stadium XL). Ports
    `generate._rewrite_input_endpoint`, which also reshapes `b00`'s params
    between the mono and stereo wrapper shapes when the target's
    channel-count differs from the current model's.
    """
    flow = (body.get("preset") or {}).get("flow") or []
    if not (0 <= path < len(flow)) or not isinstance(flow[path], dict):
        raise MutateError(f"Path {path} not in body flow (flow has {len(flow)} path(s)).")

    device_id = _chassis_device_id(body)
    try:
        target_model = controllers.resolve_input_model(device_id, jack)
    except ControllerError as exc:
        raise MutateError(str(exc)) from exc

    try:
        _rewrite_input_endpoint(flow[path], target_model)
    except GenerateError as exc:
        raise MutateError(str(exc)) from exc


# --- controller wiring: wire_footswitch / wire_expression / wire_wah_toe ----

def _validate_fs_args(behavior, curve, color, param, min, max, block) -> None:
    """Validate footswitch-assignment arguments; raise ``MutateError`` on any
    bad ``behavior``/``curve``/``color`` or missing/misapplied ``min``/``max``
    (mirrors the spec-side validation)."""
    if behavior not in ("latching", "momentary"):
        raise MutateError(
            f"Unknown footswitch behavior {behavior!r}; must be 'latching' or 'momentary'."
        )
    if curve is not None and curve not in controllers.CURVES:
        raise MutateError(
            f"Unknown curve {curve!r}; must be one of {list(controllers.CURVES)}."
        )
    if color is not None and color not in controllers.FS_COLORS:
        raise MutateError(
            f"Unknown footswitch color {color!r}; "
            f"must be one of {sorted(controllers.FS_COLORS)}."
        )
    if param is not None and not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in (min, max)
    ):
        raise MutateError(
            f"FS param target {block!r}.{param!r} requires numeric min and max "
            f"(the two raw param values the switch toggles between)."
        )
    if param is None and (min is not None or max is not None):
        raise MutateError(
            "min/max apply only to param footswitch targets; a bypass "
            "assignment toggles the block on/off (mirrors spec validation)."
        )


def _write_fs_scribble(entry, switch, source_id, label, color) -> None:
    """Set a switch's scribble strip (``fs_label``/``fs_color``) in its
    ``sources`` entry, in place. Only the stomp banks (A ``0x010101NN`` / B
    ``0x010102NN``) have strips; a label/color on the toe switch or an EXP pedal
    would be silently invisible on the device — warn and keep the entry
    corpus-shaped (toe/EXP entries carry no ``fs_*`` keys)."""
    import sys
    if (source_id & 0xFFFFFF00) not in (0x01010100, 0x01010200):
        print(
            f"warning: switch {switch!r} has no scribble strip on the "
            f"device; its label/color will not be shown (only FS1–FS5 / "
            f"FS7–FS11 have strips).",
            file=sys.stderr,
        )
        return
    if label is not None and len(label) > controllers.FS_LABEL_MAX:
        print(
            f"warning: footswitch label {label!r} is "
            f"{len(label)} chars; the device shows at most "
            f"{controllers.FS_LABEL_MAX}.",
            file=sys.stderr,
        )
    # Full scribble-strip shape observed across real exports:
    # {bypass, fs_color, fs_label, fs_topidx}.
    entry.setdefault("fs_topidx", 0)
    entry["fs_label"] = label if label is not None else entry.get("fs_label", "")
    entry["fs_color"] = color if color is not None else entry.get("fs_color", "auto")


def wire_footswitch(
    body: dict[str, Any],
    switch: str,
    block: str,
    behavior: str,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
    param: str | None = None,
    min: float | None = None,
    max: float | None = None,
    curve: str | None = None,
    threshold: float | None = None,
    label: str | None = None,
    color: str | None = None,
) -> None:
    """Assign a physical footswitch to a placed block, in place.

    Without `param`, writes a `targetbypass` controller dict onto the block's
    bNN-level `@enabled` wrapper (the same wrapper `set_enabled` mutates).
    With `param` (plus numeric `min`/`max` in raw param units), writes a
    `param`-type controller onto that param's value wrapper instead — the
    switch then toggles the param between the two values (corpus-real; see
    `generate._build_fs_param_controller`). Either way the resolved source id
    is registered in `preset.sources`; `label`/`color` set that switch's
    scribble strip there (`fs_label` / `fs_color`).

    `switch` is a logical name — one of the assignable footswitches
    "FS1".."FS5" / "FS7".."FS11" (FS6 = MODE and FS12 = TAP/Tuner are reserved
    and rejected), or "EXP1Toe" for the expression-pedal toe/position switch
    (see `wire_wah_toe`) — resolved via `controllers.resolve_controller_source`
    against the chassis device_id. `curve` is a `controllers.CURVES` name;
    `threshold` sets the switch's flip point (both optional).

    Assignment is permissive (matches the device-validated original
    `generate._build_fs_assignments`): one switch may drive multiple blocks
    and params (a merge switch), and re-wiring a target to a different switch
    is last-wins. Only an invalid `behavior`/`curve`/`color` or an
    unresolvable `switch`/`block`/`param` raises `MutateError`.
    """
    _validate_fs_args(behavior, curve, color, param, min, max, block)
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    device_id = _chassis_device_id(body)
    try:
        source_id = controllers.resolve_controller_source(device_id, switch)
    except ControllerError as exc:
        raise MutateError(str(exc)) from exc

    bnn = body["preset"]["flow"][fi][key]
    if param is not None:
        # FS → param toggle: attach the controller to the param's value wrapper.
        slot = _slot_dict(body, fi, key, si)
        lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
        if param not in lib_block.params:
            raise MutateError(
                f"FS target {switch} → {block!r}.{param!r}: unknown param. "
                f"Known params: {sorted(lib_block.params.keys())}."
            )
        wrapped = (slot.get("params") or {}).get(param)
        if not isinstance(wrapped, dict):
            raise MutateError(
                f"Block {block!r} has no existing value for param {param!r}."
            )
        if _is_stereo_param(wrapped):
            raise MutateError(
                f"FS target {block!r}.{param!r}: stereo-shaped params are "
                f"not supported for footswitch assignment."
            )
        controller = _build_fs_param_controller(
            source_id, behavior, min, max, curve=curve, threshold=threshold)
    else:
        wrapped = bnn.get("@enabled")
        if not isinstance(wrapped, dict):
            wrapped = {"value": True}
            bnn["@enabled"] = wrapped
        controller = _build_fs_controller(
            source_id, behavior,
            position=controllers.is_position_switch(switch),
            curve=curve, threshold=threshold,
        )

    # Assignment is permissive, matching the device-validated behavior of the
    # original `generate._build_fs_assignments` (keyed by block; its source-id
    # set is deduped): the Stadium allows ONE switch to drive MULTIPLE targets
    # (a merge switch -- e.g. a wah and a volume both bound to `EXP1Toe`, or a
    # bypass plus a param), and re-wiring a target to a different switch is
    # last-wins. Real exports rely on both, so no conflict is raised here --
    # that would reject valid hardware configurations and break faithful
    # round-tripping.
    sources = body.setdefault("preset", {}).setdefault("sources", {})
    wrapped["controller"] = controller
    entry = sources.setdefault(str(source_id), {"bypass": False})
    if label is not None or color is not None:
        _write_fs_scribble(entry, switch, source_id, label, color)


def wire_expression(
    body: dict[str, Any],
    pedal: str,
    targets: list[dict[str, Any]],
    library: Library,
) -> None:
    """Sweep one or more block params with an expression pedal, in place.

    Ports `generate._build_exp_controller` + `_build_exp_assignments`: each
    target dict is `{"block", "param", "min"=0.0, "max"=1.0}` (plus optional
    `"path"`/`"lane"`/`"pos"` to disambiguate, matching `resolve_slot`, and
    optional `"curve"` — a `controllers.CURVES` name, default "linear").
    Writes a `param`-type controller dict onto the param's existing value
    wrapper and registers the pedal's source id in `preset.sources`.

    Raises `MutateError` for: an empty `targets` list or an unknown param.
    Assignment is last-wins (matches the device-validated
    original `generate._build_exp_assignments`): a repeated `(block, param)`
    within `targets`, or a param already driven by another pedal, is
    overwritten rather than rejected. Stereo-shaped params (`{"1": ..., "2":
    ...}`) are out of scope -- see `set_param`'s stereo handling, which this
    verb does not replicate.
    """
    if not targets:
        raise MutateError("wire_expression requires a non-empty targets list.")

    device_id = _chassis_device_id(body)
    try:
        source_id = controllers.resolve_controller_source(device_id, pedal)
    except ControllerError as exc:
        raise MutateError(str(exc)) from exc

    resolved: list[tuple] = []
    for target in targets:
        block = target["block"]
        param = target["param"]
        # `min > max` is a valid inverted sweep (heel = max effect, toe = min);
        # real exports carry it and the original `_build_exp_controller` passed
        # it through untouched, so it is NOT rejected here.
        min_val = target.get("min", 0.0)
        max_val = target.get("max", 1.0)
        curve = target.get("curve")
        if curve is not None and curve not in controllers.CURVES:
            raise MutateError(
                f"Unknown curve {curve!r}; must be one of {list(controllers.CURVES)}."
            )
        threshold = target.get("threshold")

        fi, key, si = resolve_slot(
            body, block, library,
            path=target.get("path"), lane=target.get("lane"), pos=target.get("pos"),
        )
        slot = _slot_dict(body, fi, key, si)
        lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
        if param not in lib_block.params:
            raise MutateError(
                f"EXP target {pedal} → {block!r}.{param!r}: unknown param. "
                f"Known params: {sorted(lib_block.params.keys())}."
            )
        wrapped = (slot.get("params") or {}).get(param)
        if not isinstance(wrapped, dict):
            raise MutateError(
                f"Block {block!r} has no existing value for param {param!r}."
            )
        if _is_stereo_param(wrapped):
            raise MutateError(
                f"EXP target {block!r}.{param!r}: stereo-shaped params are "
                f"not supported for expression assignment."
            )
        # Last-wins: a param already carrying a controller is overwritten,
        # matching the original `generate._build_exp_assignments` (its
        # `exp_map` is keyed by (block, param), so a later assignment replaces
        # an earlier one). A duplicate (block, param) within `targets`, or a
        # second pedal driving the same param, resolves to the last write --
        # real exports contain both, so raising would break round-tripping.
        resolved.append((wrapped, min_val, max_val, curve, threshold))

    # Commit only after every target validates, so a failure partway through
    # `targets` leaves the body untouched.
    for wrapped, min_val, max_val, curve, threshold in resolved:
        wrapped["controller"] = _build_exp_controller(
            source_id, min_val, max_val, curve=curve, threshold=threshold)

    sources = body.setdefault("preset", {}).setdefault("sources", {})
    sources.setdefault(str(source_id), {"bypass": False})


def wire_midi(
    body: dict[str, Any],
    cc: int,
    targets: list[dict[str, Any]],
    library: Library,
) -> None:
    """Bind an incoming MIDI Control Change (``cc`` 0..127) to one or more
    placed targets, in place (backlog #33).

    Each target dict is ``{"block", "param"|None, "bypass": bool, "min", "max",
    "path"/"lane"/"pos"}``: a param sweep (``param`` set) or a block-bypass
    toggle (``bypass=True``). CC-only — MIDI Note sources are out of scope.

    Unlike footswitch/expression assignments, the MIDI binding is NOT written
    as a device-native ``.hsp`` controller dict: the ``.hsp``'s ``midisource``
    controller-source encoding is 0 across the whole 211-export corpus (no
    factory preset uses MIDI) and the parity capture pinned only the DEVICE
    ``.sbe`` / wire encoding, not the ``.hsp`` JSON shape — so inventing a
    device-native ``.hsp`` encoding is out of scope. Instead the assignment is
    recorded in a helixgen-namespaced ``preset._helixgen_midi`` list that the
    transcoder reads to synthesize the device ``ctrl``/``ctm_`` records, and
    ``view`` lifts back into the recipe. See BACKLOG #33.
    """
    if not isinstance(cc, int) or isinstance(cc, bool) or not (0 <= cc <= 127):
        raise MutateError(f"MIDI cc must be an integer 0..127 (got {cc!r}).")
    if not targets:
        raise MutateError("wire_midi requires a non-empty targets list.")

    records: list[dict[str, Any]] = []
    for target in targets:
        block = target["block"]
        bypass = bool(target.get("bypass", False))
        param = None if bypass else target.get("param")
        fi, key, si = resolve_slot(
            body, block, library,
            path=target.get("path"), lane=target.get("lane"), pos=target.get("pos"),
        )
        num = int(key[1:])
        lane = 1 if num >= 14 else 0
        pos = num - 14 * lane
        rec: dict[str, Any] = {"cc": cc, "path": fi, "lane": lane, "pos": pos,
                               "block": block}
        if bypass:
            rec["param"] = None
        else:
            slot = _slot_dict(body, fi, key, si)
            lib_block = library.load_block(_translate_model_id(slot.get("model", "")))
            if param not in lib_block.params:
                raise MutateError(
                    f"MIDI CC {cc} → {block!r}.{param!r}: unknown param. "
                    f"Known params: {sorted(lib_block.params.keys())}."
                )
            wrapped = (slot.get("params") or {}).get(param)
            if not isinstance(wrapped, dict):
                raise MutateError(
                    f"Block {block!r} has no existing value for param {param!r}."
                )
            if _is_stereo_param(wrapped):
                raise MutateError(
                    f"MIDI target {block!r}.{param!r}: stereo-shaped params are "
                    f"not supported for MIDI assignment."
                )
            rec["param"] = param
            rec["min"] = target.get("min", 0.0)
            rec["max"] = target.get("max", 1.0)
        records.append(rec)

    # Commit only after every target validates.
    midi = body.setdefault("preset", {}).setdefault("_helixgen_midi", [])
    midi.extend(records)


# Command Center MIDI subtype -> the native ``Command`` param value that selects
# it (findings §5, confirmed by `Epic Lots of EQ.hsp`): PC=0, CC=1, MMC=2, Note=3.
_MIDI_SUBTYPE = {"midi_pc": 0, "midi_cc": 1, "midi_mmc": 2, "midi_note": 3}


def _command_native_record(command: str, fields: dict[str, Any], *,
                           behavior: str, toggle: bool, ordinal: int) -> dict[str, Any]:
    """Build one ``preset.commands`` record (the encoding real exports carry;
    see BACKLOG #16). ``PresetSnapshot`` and ``MIDI`` param sets mirror the
    corpus byte-for-byte (all keys always present)."""
    def wrap(d: dict[str, int]) -> dict[str, dict[str, int]]:
        return {k: {"value": v} for k, v in d.items()}

    if command == "snapshot":
        params = {"Action": 0, "Command": 0, "Preset": 0, "Setlist": 0,
                  "Snapshot": fields["snapshot"]}
        ctype = "PresetSnapshot"
    else:
        # MIDI: all 11 params present; ``Command`` selects the subtype.
        params = {"CC#": 0, "Command": _MIDI_SUBTYPE[command], "LSB": 0,
                  "MIDI Ch": fields["channel"], "MSB": 0, "Message": 0,
                  "Note": 0, "NoteOff": 0, "PC": 0, "Value": 0, "Velocity": 0}
        if command == "midi_cc":
            params["CC#"] = fields["cc"]
            params["Value"] = fields["value"]
        elif command == "midi_pc":
            params["PC"] = fields["program"]
            params["MSB"] = fields["bank_msb"]
            params["LSB"] = fields["bank_lsb"]
        elif command == "midi_note":
            params["Note"] = fields["note"]
            params["Velocity"] = fields["velocity"]
            params["NoteOff"] = 1 if fields["note_off"] else 0
        elif command == "midi_mmc":
            params["Message"] = fields["message"]
        ctype = "MIDI"

    return {"behavior": behavior, "curve": "linear", "delay": 0, "goid": 0,
            "ordinal": ordinal, "params": wrap(params), "threshold": 0.0,
            "toggle": toggle, "type": ctype}


def wire_command(
    body: dict[str, Any],
    switch: str,
    command: str,
    fields: dict[str, Any],
    *,
    behavior: str = "latching",
    toggle: bool = False,
    label: str | None = None,
    color: str | None = None,
) -> None:
    """Author a Command Center command onto a footswitch/Instant slot, in place
    (backlog #16). Writes NATIVELY into ``preset.commands`` (the encoding real
    exports carry — corpus-proven; unlike #33 MIDI-CC which needed a sidecar),
    keyed by the switch's ``.hsp`` source id, and registers the source in
    ``preset.sources``.

    ``switch`` is ``FS1``–``FS5`` / ``FS7``–``FS11`` or ``Instant1``–``Instant6``
    (resolved via :func:`controllers.resolve_command_source`; reserved
    ``FS6``/``FS12`` rejected). ``command`` is one of ``midi_cc``/``midi_pc``/
    ``midi_note``/``midi_mmc``/``snapshot``/``preset`` and ``fields`` its
    validated family params (see :mod:`spec`). Several commands may share a
    switch (a merged switch) — each gets the next ``ordinal`` in call order.

    ``label``/``color`` set the FS scribble strip (``preset.sources``); on an
    Instant slot (no strip) they warn and are ignored.
    """
    if color is not None and color not in controllers.FS_COLORS:
        raise MutateError(
            f"Unknown footswitch color {color!r}; "
            f"must be one of {sorted(controllers.FS_COLORS)}."
        )
    device_id = _chassis_device_id(body)
    source_id = controllers.resolve_command_source(device_id, switch)
    is_footswitch = switch.startswith("FS")

    commands = body.setdefault("preset", {}).setdefault("commands", {})
    key = str(source_id)
    records = commands.setdefault(key, [])
    records.append(_command_native_record(
        command, fields, behavior=behavior, toggle=toggle,
        ordinal=len(records)))

    sources = body["preset"].setdefault("sources", {})
    entry = sources.setdefault(key, {"bypass": False})
    entry.setdefault("bypass", False)
    if is_footswitch:
        entry.setdefault("fs_topidx", 0)
        entry["fs_label"] = label if label is not None else entry.get("fs_label", "")
        entry["fs_color"] = color if color is not None else entry.get("fs_color", "auto")
    elif label is not None or color is not None:
        import sys
        print(
            f"warning: {switch} has no scribble strip; label/color ignored.",
            file=sys.stderr,
        )


def wire_wah_toe(
    body: dict[str, Any],
    block: str,
    library: Library,
    *,
    path: int | None = None,
    lane: int | None = None,
    pos: int | None = None,
) -> None:
    """Wire a wah block's bypass to the onboard expression pedal's toe/
    position switch (`EXP1Toe`), in place -- the standard Helix wah
    auto-engage: pushing the pedal fully forward toggles the wah on/off
    while `EXP1` sweeps its `Pedal` param (see `wire_expression` for that
    half of the setup; this verb only wires the bypass side).

    `EXP1Toe`'s source id is `0x01010500` (hardware-validated in 0.5.1 --
    ~all real wah exports carry it). Thin wrapper over `wire_footswitch`
    with `switch="EXP1Toe"`, `behavior="latching"`: `is_position_switch`
    recognizes the "Toe" suffix and attaches the explicit min/max/threshold
    bounds a position switch needs (a digital FS's null bounds don't bind).
    """
    wire_footswitch(body, "EXP1Toe", block, "latching", library, path=path, lane=lane, pos=pos)


# --- batch operations (`helixgen patch`) -------------------------------------

# Each entry takes (body, library, op_dict) and mutates `body` in place,
# returning a list[str] of warnings (empty for ops that never warn).

def _op_set_param(body: dict, library: Library, o: dict) -> list[str]:
    set_param(
        body, o["block"], o["param"], o["value"], library,
        snapshot=o.get("snapshot"),
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )
    return []


def _op_set_enabled(body: dict, library: Library, o: dict) -> list[str]:
    set_enabled(
        body, o["block"], o["enabled"], library,
        snapshot=o.get("snapshot"),
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )
    return []


def _op_add_block(body: dict, library: Library, o: dict) -> list[str]:
    add_block(
        body, o["block"], library,
        path=o.get("path", 0), after=o.get("after"), params=o.get("params"),
    )
    return []


def _op_remove_block(body: dict, library: Library, o: dict) -> list[str]:
    remove_block(
        body, o["block"], library,
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )
    return []


def _op_swap_model(body: dict, library: Library, o: dict) -> list[str]:
    return swap_model(
        body, o["old"], o["new"], library,
        path=o.get("path"), lane=o.get("lane"), pos=o.get("pos"),
    )


PATCH_OPS = {
    "set_param": _op_set_param,
    "set_enabled": _op_set_enabled,
    "add_block": _op_add_block,
    "remove_block": _op_remove_block,
    "swap_model": _op_swap_model,
}

# Required fields per op, validated up front so a missing key reports itself
# as "op N (set_param): missing required field(s) ..." instead of a bare
# KeyError escaping from the op body.
_PATCH_OP_REQUIRED = {
    "set_param": ("block", "param", "value"),
    "set_enabled": ("block", "enabled"),
    "add_block": ("block",),
    "remove_block": ("block",),
    "swap_model": ("old", "new"),
}


def apply_operations(
    body: dict[str, Any], operations: list, library: Library
) -> list[str]:
    """Apply a sequence of `{"op": ..., ...}` dicts to a parsed `.hsp` body,
    in place, returning the accumulated warnings (`swap_model` messages about
    params/IRs/MIDI bindings that could not be carried over).

    The op vocabulary is :data:`PATCH_OPS` (`set_param`, `set_enabled`,
    `add_block`, `remove_block`, `swap_model`), each dispatching to the
    matching surgical verb in this module. An unknown op raises
    :class:`MutateError` — callers apply ops to an in-memory body and only
    write the file after ALL ops succeeded, so a bad op never half-patches
    the preset on disk. (This is the engine behind `helixgen patch`,
    formerly the MCP `patch_preset` tool.)
    """
    if not isinstance(operations, list):
        raise MutateError(
            f"operations must be a JSON list of op dicts, got {type(operations).__name__}")
    warnings: list[str] = []
    for i, o in enumerate(operations):
        if not isinstance(o, dict):
            raise MutateError(f"op {i}: each operation must be a dict, got {o!r}")
        op = o.get("op")
        if op not in PATCH_OPS:
            raise MutateError(
                f"op {i}: unknown patch op {op!r}; valid: {sorted(PATCH_OPS)}")
        missing = [k for k in _PATCH_OP_REQUIRED[op] if k not in o]
        if missing:
            raise MutateError(
                f"op {i} ({op}): missing required field(s) {missing}")
        try:
            warnings.extend(PATCH_OPS[op](body, library, o))
        except MutateError as e:
            raise MutateError(f"op {i} ({op}): {e}") from e
    return warnings
