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

from helixgen import controllers
from helixgen.controllers import ControllerError
from helixgen.generate import (
    HSP_SNAPSHOT_SLOTS,
    GenerateError,
    ParamValidationError,
    _build_exp_controller,
    _build_fs_controller,
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

    new_keys = _renumber_lane(path_dict, lane, ordered)
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
    del_lane, _del_pos = _lane_pos(key)

    remaining_keys = sorted(
        (k for k in _bnn_keys(path_dict) if _lane_pos(k)[0] == del_lane and k != key),
        key=lambda k: path_dict[k].get("position", _lane_pos(k)[1]),
    )
    ordered = [path_dict[k] for k in remaining_keys]
    _renumber_lane(path_dict, del_lane, ordered)


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
    if lib_block.category not in ("delay", "reverb"):
        raise MutateError(
            f"Block {block!r} has category {lib_block.category!r}; trails "
            f"(harness spillover) applies only to delay and reverb blocks."
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
) -> None:
    """Assign a physical footswitch to a placed block's bypass, in place.

    Ports `generate._build_fs_controller` + `_build_fs_assignments`: writes
    a `targetbypass` controller dict onto the block's bNN-level `@enabled`
    wrapper (the same wrapper `set_enabled` mutates) and registers the
    resolved source id in `preset.sources`. `switch` is a logical name — one of
    the assignable footswitches "FS1".."FS5" / "FS7".."FS11" (FS6 = MODE and
    FS12 = TAP/Tuner are reserved and rejected), or "EXP1Toe" for the
    expression-pedal toe/position switch (see `wire_wah_toe`) — resolved via
    `controllers.resolve_controller_source` against the chassis device_id.

    Assignment is permissive (matches the device-validated original
    `generate._build_fs_assignments`): one switch may drive multiple blocks (a
    footswitch group), and re-wiring a block to a different switch is
    last-wins. Only an invalid `behavior` or an unresolvable `switch`/`block`
    raises `MutateError`.
    """
    if behavior not in ("latching", "momentary"):
        raise MutateError(
            f"Unknown footswitch behavior {behavior!r}; must be 'latching' or 'momentary'."
        )
    fi, key, si = resolve_slot(body, block, library, path=path, lane=lane, pos=pos)
    device_id = _chassis_device_id(body)
    try:
        source_id = controllers.resolve_controller_source(device_id, switch)
    except ControllerError as exc:
        raise MutateError(str(exc)) from exc

    bnn = body["preset"]["flow"][fi][key]
    wrapped = bnn.get("@enabled")
    if not isinstance(wrapped, dict):
        wrapped = {"value": True}
        bnn["@enabled"] = wrapped

    # Assignment is permissive, matching the device-validated behavior of the
    # original `generate._build_fs_assignments` (keyed by block; its source-id
    # set is deduped): the Stadium allows ONE switch to drive MULTIPLE blocks
    # (a footswitch group -- e.g. a wah and a volume both bound to `EXP1Toe`),
    # and re-wiring a block to a different switch is last-wins. Real exports
    # rely on both, so no conflict is raised here -- that would reject valid
    # hardware configurations and break faithful round-tripping.
    sources = body.setdefault("preset", {}).setdefault("sources", {})
    wrapped["controller"] = _build_fs_controller(
        source_id, behavior, position=controllers.is_position_switch(switch)
    )
    sources.setdefault(str(source_id), {"bypass": False})


def wire_expression(
    body: dict[str, Any],
    pedal: str,
    targets: list[dict[str, Any]],
    library: Library,
) -> None:
    """Sweep one or more block params with an expression pedal, in place.

    Ports `generate._build_exp_controller` + `_build_exp_assignments`: each
    target dict is `{"block", "param", "min"=0.0, "max"=1.0}` (plus optional
    `"path"`/`"lane"`/`"pos"` to disambiguate, matching `resolve_slot`).
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

    resolved: list[tuple[dict[str, Any], float, float]] = []
    for target in targets:
        block = target["block"]
        param = target["param"]
        # `min > max` is a valid inverted sweep (heel = max effect, toe = min);
        # real exports carry it and the original `_build_exp_controller` passed
        # it through untouched, so it is NOT rejected here.
        min_val = target.get("min", 0.0)
        max_val = target.get("max", 1.0)

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
        resolved.append((wrapped, min_val, max_val))

    # Commit only after every target validates, so a failure partway through
    # `targets` leaves the body untouched.
    for wrapped, min_val, max_val in resolved:
        wrapped["controller"] = _build_exp_controller(source_id, min_val, max_val)

    sources = body.setdefault("preset", {}).setdefault("sources", {})
    sources.setdefault(str(source_id), {"bypass": False})


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
