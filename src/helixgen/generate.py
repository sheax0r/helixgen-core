"""Generate: turn a parsed Spec + Library into a .hlx or .hsp preset dict.

Dispatches on `chassis._helixgen_chassis_shape`:
- "hlx" (or absent) → legacy Helix .hlx shape (data.tone.dspN.blockN/cabN)
- "hsp"             → Stadium .hsp shape (preset.flow[i].bNN.slot[0])
"""
from __future__ import annotations

import copy
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

from helixgen import __version__
from helixgen import controllers
from helixgen.chassis import CHASSIS_SHAPE_KEY
from helixgen.hsp import HSP_MAGIC, translate_to_hsp
from helixgen.ingest import (
    DSP_BLOCK_KEY_PREFIX,
    DSP_CAB_KEY_PREFIX,
    PRESET_DSP_KEYS,
    RAW_BLOCK_CAB_LINK_KEY,
    RAW_BLOCK_MODEL_KEY,
    RAW_BLOCK_NON_PARAM_KEYS,
    RAW_BLOCK_SYSTEM_KEY_PREFIX,
)
from helixgen.ir import IR_MODEL_PREFIX
from helixgen.library import Block, Library
from helixgen.spec import Spec, parse_spec


_HASH_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def _resolve_irhash(block_default: str | None, spec_ir: str | None, irs: "IrMapping") -> str:
    """Decide which irhash to emit on an IR slot.

    Priority: spec_ir (resolved via IrMapping) > block_default > error.
    """
    from helixgen.ir import IrMappingError  # local import to avoid cycle

    if spec_ir is not None:
        if _HASH_RE.fullmatch(spec_ir):
            h = spec_ir.lower()
            if irs is not None:
                try:
                    irs.resolve_by_hash(h)
                except IrMappingError:
                    print(
                        f"warning: IR hash {h} is not registered; passing it "
                        f"through unchanged (the device must already hold this IR).",
                        file=sys.stderr,
                    )
            return h
        # basename form still requires a registered mapping
        if irs is None:
            raise GenerateError(
                f"cannot resolve IR basename {spec_ir!r}: no IR mapping available"
            )
        try:
            h, _ = irs.resolve_by_basename(spec_ir)
        except IrMappingError as e:
            raise GenerateError(str(e)) from e
        return h
    if block_default is not None:
        return block_default
    raise GenerateError(
        "IR block requires an `ir` field (no canonical irhash available); "
        "see `helixgen list-irs`"
    )


ResolvedPath = list[tuple[Block, dict[str, Any]]]


class ParamValidationError(ValueError):
    """User specified parameters that don't exist on the resolved block."""


class GenerateError(ValueError):
    """Generation failed for a structural reason (chassis, slots, etc.)."""


def resolve_blocks(spec: Spec, library: Library) -> list[ResolvedPath]:
    """Look up every block in the spec against the library."""
    resolved: list[ResolvedPath] = []
    for path in spec.paths:
        chain: ResolvedPath = []
        for entry in path.blocks:
            block = library.find_block(entry.block)
            chain.append((block, entry.params))
        resolved.append(chain)
    return resolved


def validate_params(block: Block, user_params: dict[str, Any]) -> None:
    """Hard-fail if any user_params key isn't in the block's schema."""
    known = set(block.params.keys())
    unknown = sorted(set(user_params.keys()) - known)
    if not unknown:
        return
    raise ParamValidationError(
        f"Unknown param(s) {unknown} for block {block.display_name!r}. "
        f"Known params: {sorted(known)}."
    )


def _coerce_param_value(block: Block, key: str, value: Any) -> Any:
    """Coerce a user-supplied param value to the block schema's declared type.

    The Stadium .hsp parser is type-sensitive: a numeric param declared `float`
    that is written as an int (e.g. a cab `HighCut`/`LowCut` passed as `6500`
    instead of `6500.0`) can corrupt the block on-device, rendering it silent.
    Library exemplars always store floats as floats; user values arrive with
    whatever JSON/Python type the caller used (JSON `6500` → int). Coerce to the
    declared type so generated params match the exemplar encoding.

    Only safe, lossless coercions are applied; non-numeric or unknown-schema
    values pass through untouched.
    """
    schema = block.params.get(key)
    if not schema:
        return value
    declared = schema.get("type")
    # Target is bool: the device stores bools as JSON true/false, and a numeric
    # 0/1 there corrupts the block the same way an int-for-float does. Convert
    # the unambiguous 0/1 a caller might have used for false/true (JSON specs
    # often write 1/0 for booleans). Leave bools and other numbers as-is.
    if declared == "bool":
        if not isinstance(value, bool) and isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        return value
    # bool is a subclass of int — never coerce bools into numbers.
    if isinstance(value, bool):
        return value
    if declared == "float" and isinstance(value, int):
        return float(value)
    if declared == "int" and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _is_amp(block: Block) -> bool:
    return block.category == "amp"


def _is_cab(block: Block) -> bool:
    return block.category == "cab"


def compose_preset(spec: Spec, library: Library, *, source: str, irs: "IrMapping | None" = None) -> dict[str, Any]:
    """Build a preset dict from spec + library. Shape-aware: dispatches by
    chassis shape so .hlx and .hsp libraries each produce native output.
    """
    if not library.has_chassis():
        raise GenerateError(
            "Library has no chassis. Run `helixgen ingest <real-export>` first."
        )

    chassis = library.load_chassis()
    shape = chassis.get(CHASSIS_SHAPE_KEY, "hlx")

    if shape == "hlx":
        return _compose_preset_hlx(spec, library, source=source, chassis=chassis)
    if shape == "hsp":
        return _compose_preset_hsp(spec, library, source=source, chassis=chassis, irs=irs)
    raise GenerateError(
        f"Unknown chassis shape {shape!r}. Re-ingest from a real export."
    )


def _provenance(source: str) -> dict[str, str]:
    return {
        "version": __version__,
        "spec_source": source,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _compose_preset_hlx(
    spec: Spec, library: Library, *, source: str, chassis: dict[str, Any]
) -> dict[str, Any]:
    """Compose a .hlx-shape preset.

    For each path in the spec, place chain entries into the matching dsp:
    - Non-cab blocks go to sequential `block0`, `block1`, ... slots.
    - Cab blocks go to sequential `cab0`, `cab1`, ... slots.
    - When an amp is followed by a cab, the amp's `@cab` is set to the cab's
      slot key so Stadium/Helix renders the pairing correctly.
    """
    if any(p.input is not None for p in spec.paths) or spec.footswitches or spec.expression:
        print(
            "warning: input routing / footswitches / expression are Stadium-only; "
            "ignored for .hlx chassis output.",
            file=sys.stderr,
        )
    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    preset = copy.deepcopy(chassis)
    tone = preset.setdefault("data", {}).setdefault("tone", {})

    for path_index, chain in enumerate(resolved):
        if path_index >= len(PRESET_DSP_KEYS):
            raise GenerateError(
                f"Spec has {len(resolved)} paths but only {len(PRESET_DSP_KEYS)} DSPs available."
            )
        dsp_key = PRESET_DSP_KEYS[path_index]
        dsp = tone.setdefault(dsp_key, {})

        block_index = 0
        cab_index = 0
        last_amp_slot: str | None = None
        for (block, user_params), block_entry in zip(chain, spec.paths[path_index].blocks):
            placed = copy.deepcopy(block.exemplar)
            if block_entry.enabled is not None:
                placed["@enabled"] = block_entry.enabled
            for k, v in user_params.items():
                placed[k] = _coerce_param_value(block, k, v)

            if _is_cab(block):
                slot = f"{DSP_CAB_KEY_PREFIX}{cab_index}"
                cab_index += 1
                dsp[slot] = placed
                if last_amp_slot is not None:
                    dsp[last_amp_slot][RAW_BLOCK_CAB_LINK_KEY] = slot
                    last_amp_slot = None
            else:
                slot = f"{DSP_BLOCK_KEY_PREFIX}{block_index}"
                block_index += 1
                dsp[slot] = placed
                if _is_amp(block):
                    last_amp_slot = slot
                else:
                    last_amp_slot = None

    meta = preset.setdefault("data", {}).setdefault("meta", {})
    meta["name"] = spec.name
    if spec.author is not None:
        meta["author"] = spec.author
    meta["helixgen"] = _provenance(source)
    return preset


# ---------------------------------------------------------------------------
# .hsp (Stadium) composition.
#
# The .hsp wire format is structurally different: blocks live in
# preset.flow[path_index] keyed `b00..b13`. Each bNN has bNN-level metadata
# (`type`, `position`, `path`) and a `slot` array. Each slot has `model`,
# `@enabled` (wrapped as {"value": ...}), `version`, and a nested `params`
# dict where each value is wrapped as {"value": x}.
#
# The library stores blocks in a flattened, .hlx-normalized form (params
# unwrapped, model_id translated). To generate .hsp we have to un-flatten,
# re-wrap, and translate model ids back to the Stadium namespace.
# ---------------------------------------------------------------------------

_HSP_BNN_RANGE = range(1, 13)  # b01..b12 are user-block slots
HSP_SNAPSHOT_SLOTS = 8           # Stadium has 8 fixed snapshot slots per preset
DEFAULT_INPUT_MODES = ("both", "none")  # by path index


def _is_chassis_meta_key(key: str) -> bool:
    """True for top-level chassis annotations that must not appear in output."""
    return key.startswith("_helixgen_")


def _chassis_device_id(chassis: dict[str, Any]) -> str:
    """Return the chassis's device_id, or 'stadium_xl' if absent/unrecognized."""
    return (chassis.get("meta") or {}).get("device_id") or "stadium_xl"


def _build_exp_controller(source_id: int, min_val: float, max_val: float) -> dict[str, Any]:
    """Build the controller dict that wraps a param value for an EXP assignment.

    Shape derived from real Stadium XL exports (type=param, behavior=continuous,
    numeric delay/goid/threshold).
    """
    return {
        "behavior":   "continuous",
        "bypassed":   False,
        "curve":      "linear",
        "delay":      0,
        "goid":       0,
        "max":        max_val,
        "midisource": 0,
        "min":        min_val,
        "source":     source_id,
        "threshold":  0.0,
        "type":       "param",
    }


def _build_fs_controller(source_id: int, behavior: str) -> dict[str, Any]:
    """Build the controller dict that wraps @enabled for an FS assignment.

    Shape derived from real Stadium XL exports.
    """
    return {
        "behavior":   behavior,
        "bypassed":   False,
        "curve":      "linear",
        "delay":      None,
        "goid":       None,
        "max":        None,
        "midisource": 0,
        "min":        None,
        "source":     source_id,
        "threshold":  None,
        "type":       "targetbypass",
    }


def _wrap_value_with_snapshots(
    base: Any, snapshot_overrides: list[Any] | None
) -> dict[str, Any]:
    """Wrap a value in the Stadium `{"value": x}` envelope, optionally with a
    per-snapshot overrides array. The array is included only when at least
    one slot has a non-None override (else the wrapper stays plain).
    """
    wrapped: dict[str, Any] = {"value": base}
    if snapshot_overrides and any(o is not None for o in snapshot_overrides):
        wrapped["snapshots"] = list(snapshot_overrides)
    return wrapped


def _is_stereo_param(value: Any) -> bool:
    """True if a slot-param value uses the stereo `{"1": ..., "2": ...}` shape."""
    return (
        isinstance(value, dict)
        and "1" in value
        and isinstance(value["1"], dict)
        and "value" in value["1"]
    )


def _reshape_input_params(
    params: dict[str, Any], *, to_stereo: bool
) -> dict[str, Any]:
    """Convert input-endpoint params between mono and stereo shapes.

    Mono shape:   {"<name>": {"value": x}, ...}
    Stereo shape: {"<name>": {"1": {"value": x}, "2": {"value": y}}, ...,
                   "StereoLink": {"value": False}}

    Going mono → stereo wraps each scalar value into per-channel entries
    (both channels start equal) and adds `StereoLink: false`. Going stereo
    → mono takes channel 1 (channel 2 is discarded) and drops StereoLink.
    Identity transforms (already in target shape) return the input
    unchanged.
    """
    currently_stereo = any(_is_stereo_param(v) for v in params.values())
    if currently_stereo == to_stereo:
        return dict(params)

    if to_stereo:
        out: dict[str, Any] = {}
        for k, v in params.items():
            if k == "StereoLink":
                continue
            out[k] = {"1": copy.deepcopy(v), "2": copy.deepcopy(v)}
        out["StereoLink"] = {"value": False}
        return out

    # stereo → mono
    out = {}
    for k, v in params.items():
        if k == "StereoLink":
            continue
        if _is_stereo_param(v):
            out[k] = copy.deepcopy(v["1"])
        else:
            out[k] = copy.deepcopy(v)
    return out


def _rewrite_input_endpoint(path_dict: dict[str, Any], target_model: str) -> None:
    """Rewrite path_dict['b00'] to use `target_model`, reshaping params as needed.

    Mutates path_dict in place. Param values from the chassis are preserved
    across the swap; only the model and (where mono/stereo differs) the param
    wrapping shape change. Raises GenerateError if the path has no b00 slot.
    """
    b00 = path_dict.get("b00")
    if not isinstance(b00, dict) or not b00.get("slot"):
        raise GenerateError(
            "Chassis path has no b00 input slot; cannot apply spec input mode."
        )
    slot = b00["slot"][0]
    if slot.get("model") == target_model:
        return
    target_is_stereo = target_model.endswith("_2")
    slot["params"] = _reshape_input_params(
        slot.get("params") or {}, to_stereo=target_is_stereo
    )
    slot["model"] = target_model


def _to_hsp_bnn(
    block: Block,
    user_params: dict[str, Any],
    *,
    position: int,
    path_index: int,
    enabled_base: bool | None = None,
    enabled_overrides: list[bool | None] | None = None,
    param_overrides: dict[str, list[Any]] | None = None,
    fs_controller: dict[str, Any] | None = None,
    exp_controllers: dict[str, dict[str, Any]] | None = None,
    irhash: str | None = None,
) -> dict[str, Any]:
    """Build one Stadium bNN dict from a library Block and user param overrides.

    `enabled_overrides` is a per-snapshot list (length HSP_SNAPSHOT_SLOTS) of
    bool/None — None means "use the base @enabled value." `param_overrides`
    maps param-name → per-snapshot value list. `fs_controller`, when provided,
    attaches a footswitch bypass controller dict to the bNN-level `@enabled`
    wrapper. `exp_controllers`, when provided, attaches per-param expression-
    pedal controller dicts onto matching param values. For IR blocks, `irhash`
    is emitted directly onto the slot dict. Any may be None / empty when not
    relevant.
    """
    flat = copy.deepcopy(block.exemplar)
    for k, v in user_params.items():
        flat[k] = _coerce_param_value(block, k, v)

    slot_inner: dict[str, Any] = {
        "model": translate_to_hsp(flat.get(RAW_BLOCK_MODEL_KEY, block.model_id)),
    }
    # Slot-level @enabled: always plain (the bNN-level wraps snapshot variation).
    base_enabled = enabled_base if enabled_base is not None else flat.get("@enabled", True)
    slot_inner["@enabled"] = {"value": base_enabled}
    if "@version" in flat:
        slot_inner["version"] = flat["@version"]

    # IR blocks carry a slot-level irhash identifying the loaded impulse response.
    if irhash is not None:
        slot_inner["irhash"] = irhash

    params: dict[str, Any] = {}
    for k, v in flat.items():
        if not isinstance(k, str) or k.startswith(RAW_BLOCK_SYSTEM_KEY_PREFIX):
            continue
        if k in RAW_BLOCK_NON_PARAM_KEYS:
            continue
        snap_overrides = (param_overrides or {}).get(k)
        if snap_overrides is not None:
            snap_overrides = [
                o if o is None else _coerce_param_value(block, k, o)
                for o in snap_overrides
            ]
        wrapped = _wrap_value_with_snapshots(v, snap_overrides)
        if exp_controllers and k in exp_controllers:
            wrapped["controller"] = exp_controllers[k]
        params[k] = wrapped
    slot_inner["params"] = params

    enabled_wrapped = _wrap_value_with_snapshots(True, enabled_overrides)
    if fs_controller is not None:
        enabled_wrapped["controller"] = fs_controller

    bnn: dict[str, Any] = {
        "@enabled": enabled_wrapped,
        "type": flat.get("@type", _hsp_type_for_block(block)),
        "position": position,
        "path": path_index,
        "slot": [slot_inner],
    }
    return bnn


def _hsp_type_for_block(block: Block) -> str:
    """Fallback `type` field when the library exemplar lacks @type."""
    if block.category == "amp":
        return "amp"
    if block.category == "cab":
        return "cab"
    return "fx"


def _resolve_spec_block(
    name_or_id: str, resolved: list[ResolvedPath]
) -> tuple[int, int, Block]:
    """Locate a block in the resolved spec chains by display_name or model_id."""
    matches: list[tuple[int, int, Block]] = []
    for path_idx, chain in enumerate(resolved):
        for chain_idx, (block, _) in enumerate(chain):
            if block.model_id == name_or_id or block.display_name == name_or_id:
                matches.append((path_idx, chain_idx, block))
    if not matches:
        raise GenerateError(
            f"Spec references block {name_or_id!r} but no such block is "
            f"in the spec's paths. Add it to a path first."
        )
    if len(matches) > 1:
        raise GenerateError(
            f"Block {name_or_id!r} matches multiple placed blocks. "
            f"Use the model_id (in brackets in `list-blocks`) to disambiguate."
        )
    return matches[0]


def _build_snapshot_overrides(
    spec: Spec, resolved: list[ResolvedPath]
) -> tuple[
    dict[tuple[int, int], list[bool | None]],
    dict[tuple[int, int], dict[str, list[Any]]],
]:
    """Resolve spec snapshots into per-(path, block_in_chain) override maps.

    Returns:
      - enabled_map: {(path_idx, chain_idx): [snap0_bool_or_None, ..., snap7_...]}
      - param_map:   {(path_idx, chain_idx): {param_name: [snap0_val_or_None, ...]}}

    Snapshots beyond what the spec defines are filled with None (use base).
    Validates that referenced blocks exist and snapshot params are real
    (delegates the latter to validate_params).
    """
    enabled_map: dict[tuple[int, int], list[bool | None]] = {}
    param_map: dict[tuple[int, int], dict[str, list[Any]]] = {}

    for snap_idx, snap in enumerate(spec.snapshots):
        # disable: turn off the named block in this snapshot
        for name in snap.disable:
            path_idx, chain_idx, _ = _resolve_spec_block(name, resolved)
            key = (path_idx, chain_idx)
            enabled_map.setdefault(key, [None] * HSP_SNAPSHOT_SLOTS)
            enabled_map[key][snap_idx] = False

        # params: override values for named block in this snapshot
        for block_name, overrides in snap.params.items():
            path_idx, chain_idx, block = _resolve_spec_block(block_name, resolved)
            validate_params(block, overrides)
            key = (path_idx, chain_idx)
            block_params = param_map.setdefault(key, {})
            for pname, pval in overrides.items():
                arr = block_params.setdefault(pname, [None] * HSP_SNAPSHOT_SLOTS)
                arr[snap_idx] = pval

    return enabled_map, param_map


def _build_fs_assignments(
    spec: Spec, resolved: list[ResolvedPath], device_id: str
) -> tuple[dict[tuple[int, int], dict[str, Any]], set[int]]:
    """Resolve FS assignments to (path,chain) → controller-dict + source-id set."""
    fs_map: dict[tuple[int, int], dict[str, Any]] = {}
    source_ids: set[int] = set()
    for fs in spec.footswitches:
        path_idx, chain_idx, _ = _resolve_spec_block(fs.block, resolved)
        source_id = controllers.resolve_controller_source(device_id, fs.switch)
        fs_map[(path_idx, chain_idx)] = _build_fs_controller(source_id, fs.behavior)
        source_ids.add(source_id)
    return fs_map, source_ids


def _build_exp_assignments(
    spec: Spec, resolved: list[ResolvedPath], device_id: str
) -> tuple[dict[tuple[int, int, str], dict[str, Any]], set[int]]:
    """Resolve EXP assignments to (path, chain, param) → controller dict + source IDs."""
    exp_map: dict[tuple[int, int, str], dict[str, Any]] = {}
    source_ids: set[int] = set()
    for assignment in spec.expression:
        source_id = controllers.resolve_controller_source(device_id, assignment.pedal)
        for target in assignment.targets:
            path_idx, chain_idx, block = _resolve_spec_block(target.block, resolved)
            if target.param not in block.params:
                raise GenerateError(
                    f"EXP target {assignment.pedal} → "
                    f"{target.block!r}.{target.param!r}: unknown param. "
                    f"Known params: {sorted(block.params.keys())}."
                )
            exp_map[(path_idx, chain_idx, target.param)] = _build_exp_controller(
                source_id, target.min, target.max,
            )
            source_ids.add(source_id)
    return exp_map, source_ids


def _build_snapshot_metadata(spec: Spec) -> list[dict[str, Any]]:
    """Build the 8-entry preset.snapshots metadata list.

    First N entries take their names from the spec; remaining slots are
    filled with `Snap M` placeholders so the device sees all 8 as usable.
    """
    snaps: list[dict[str, Any]] = []
    for i in range(HSP_SNAPSHOT_SLOTS):
        if i < len(spec.snapshots):
            name = spec.snapshots[i].name
        else:
            name = f"Snap {i + 1}"
        snaps.append({
            "name": name,
            "color": "auto",
            "expsw": 1 if i == 0 else -1,  # first snapshot owns the expression pedal
            "source": 0,
            "tempo": 120.0,
            "valid": True,
        })
    return snaps


def _compose_preset_hsp(
    spec: Spec, library: Library, *, source: str, chassis: dict[str, Any], irs: "IrMapping | None" = None
) -> dict[str, Any]:
    """Compose a .hsp-shape preset. See module docstring for shape notes."""
    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    # Validate: reject `ir` field on non-IR blocks.
    for path_entry, chain in zip(spec.paths, resolved):
        for block_entry, (block, _) in zip(path_entry.blocks, chain):
            if block_entry.ir is not None and not block.model_id.startswith(IR_MODEL_PREFIX):
                raise GenerateError(
                    f"block {block.display_name!r} is not an IR block; "
                    f"remove the 'ir' field or change the block"
                )

    enabled_map, param_map = _build_snapshot_overrides(spec, resolved)
    device_id = _chassis_device_id(chassis)
    fs_map, fs_source_ids = _build_fs_assignments(spec, resolved, device_id)
    exp_map, exp_source_ids = _build_exp_assignments(spec, resolved, device_id)

    preset = copy.deepcopy(chassis)
    # Strip private library annotations — these are not part of the wire format.
    for k in [k for k in preset if isinstance(k, str) and _is_chassis_meta_key(k)]:
        del preset[k]

    flow = preset.setdefault("preset", {}).setdefault("flow", [])
    for path_index, path_entry in enumerate(spec.paths):
        if path_index >= len(flow):
            break  # block-placement loop below will raise the proper error
        path_dict = flow[path_index]
        if not isinstance(path_dict, dict):
            continue  # ditto
        mode = path_entry.input or DEFAULT_INPUT_MODES[path_index]
        target_model = controllers.resolve_input_model(device_id, mode)
        _rewrite_input_endpoint(path_dict, target_model)

    for path_index, chain in enumerate(resolved):
        if path_index >= len(flow):
            raise GenerateError(
                f"Spec has {len(resolved)} paths but chassis flow has only "
                f"{len(flow)} path(s)."
            )
        path_dict = flow[path_index]
        if not isinstance(path_dict, dict):
            raise GenerateError(
                f"Chassis flow path {path_index} is not an object; cannot place blocks."
            )
        if len(chain) > len(_HSP_BNN_RANGE):
            raise GenerateError(
                f"Path {path_index} has {len(chain)} blocks; only "
                f"{len(_HSP_BNN_RANGE)} user slots (b01..b12) available."
            )
        path_entry = spec.paths[path_index]
        for chain_idx, (block, user_params) in enumerate(chain):
            slot_index = chain_idx + 1
            key = f"b{slot_index:02d}"
            block_entry = path_entry.blocks[chain_idx]
            # Resolve irhash for IR blocks: spec.ir > canonical > error.
            resolved_irhash: str | None = None
            if block.model_id.startswith(IR_MODEL_PREFIX):
                resolved_irhash = _resolve_irhash(
                    block_default=block.default_irhash,
                    spec_ir=block_entry.ir,
                    irs=irs,
                )
            path_dict[key] = _to_hsp_bnn(
                block, user_params,
                position=slot_index,
                path_index=path_index,
                enabled_base=block_entry.enabled,
                enabled_overrides=enabled_map.get((path_index, chain_idx)),
                param_overrides=param_map.get((path_index, chain_idx)),
                fs_controller=fs_map.get((path_index, chain_idx)),
                exp_controllers={
                    pname: ctrl
                    for (pi, ci, pname), ctrl in exp_map.items()
                    if pi == path_index and ci == chain_idx
                } or None,
                irhash=resolved_irhash,
            )

    all_source_ids = fs_source_ids | exp_source_ids
    if all_source_ids:
        sources = preset["preset"].setdefault("sources", {})
        for sid in all_source_ids:
            sources.setdefault(str(sid), {"bypass": False})

    # Snapshot metadata + active-snapshot pointer. Always emitted (even when
    # the spec defines none) so the chassis-carried snapshot names from the
    # originating preset get replaced with something neutral.
    preset["preset"]["snapshots"] = _build_snapshot_metadata(spec)
    preset["preset"].setdefault("params", {})["activesnapshot"] = 0

    meta = preset.setdefault("meta", {})
    meta["name"] = spec.name
    if spec.author is not None:
        meta["author"] = spec.author
    meta["helixgen"] = _provenance(source)
    return preset


def generate_preset(
    spec_path: Path,
    output_path: Path,
    library: Library,
    irs: "IrMapping | None" = None,
) -> Path:
    """Top-level: read spec from disk, compose, write output.

    Output format follows the chassis shape: .hlx → pretty JSON; .hsp →
    8-byte magic header + compact JSON (so a Stadium can re-read it).
    """
    from helixgen.ir import IrMapping  # local import to avoid cycle

    spec_path = Path(spec_path)
    output_path = Path(output_path)

    if irs is None:
        irs = IrMapping.load()  # default location; returns empty mapping if no file

    raw = json.loads(spec_path.read_text())
    spec = parse_spec(raw, source=str(spec_path))
    preset = compose_preset(spec, library, source=str(spec_path), irs=irs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shape = library.load_chassis().get(CHASSIS_SHAPE_KEY, "hlx")
    if shape == "hsp":
        body = json.dumps(preset, separators=(",", ":")).encode("utf-8")
        output_path.write_bytes(HSP_MAGIC + body)
        # Sidecar spec beside the .hsp (source of truth for surgical edits).
        sidecar = output_path.with_name(output_path.stem + ".spec.json")
        sidecar.write_text(json.dumps(raw, indent=2))
    else:
        output_path.write_text(json.dumps(preset, indent=2))
    return output_path
