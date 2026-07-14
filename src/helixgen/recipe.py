"""Recipe: author a fresh `.hsp` (Stadium) body by replaying a spec-shaped
"recipe" onto a cloned chassis.

This is the authoring front-end of the `.hsp`-canonical redesign. Where the
legacy `generate._compose_preset_hsp` compiled a spec into a fresh body, this
module does the same job as a sequence of edits onto a cloned chassis body:

- Block placement, per-block params, base bypass, IR reference, verbatim
  `raw` state, delay/reverb `Trails`, and per-snapshot overrides are written
  through the shared low-level helpers `generate._to_hsp_bnn` /
  `_emit_splits` / `_emit_structural` / `_assign_positions` (the exact
  routines the golden pipeline uses, so output is byte-meaning identical).
- Input routing and controller wiring (footswitches, expression pedals) are
  applied as `mutate.*` verbs — the same verbs that mutate an existing body —
  so authoring and editing share one code path for those concerns.

`apply_recipe` REPLACES the `.hsp` branch of `generate.compose_preset`. The
legacy `.hlx` compose path stays in `generate.py` (out of scope here); an
`.hlx`-shaped chassis raises `GenerateError` rather than being reimplemented.
"""
from __future__ import annotations

import copy
from typing import Any

from helixgen import __version__  # noqa: F401  (re-exported provenance version lives in generate)
from helixgen import flowparams, mutate
from helixgen.chassis import CHASSIS_SHAPE_KEY
from helixgen.generate import (
    GenerateError,
    _assign_positions,
    _build_snapshot_metadata,
    _build_snapshot_overrides,
    _emit_splits,
    _emit_structural,
    _is_chassis_meta_key,
    _provenance,
    _resolve_irhash,
    _to_hsp_bnn,
    resolve_blocks,
    validate_params,
)
from helixgen.hsp import dumps_hsp
from helixgen.ir import IR_MODEL_PREFIX
from helixgen.spec import BlockEntry, InputSpec, Spec, parse_spec

_MAX_LANE_SLOTS = 12  # b01..b12 user-block slots per lane


def _effective_input(path_entry, path_index: int) -> tuple[str, InputSpec | None]:
    """(effective mode, InputSpec-or-None) for a path's `input` field."""
    inp = path_entry.input
    if isinstance(inp, InputSpec):
        return (inp.source or flowparams.default_input_mode(path_index)), inp
    return (inp or flowparams.default_input_mode(path_index)), None


_INPUT_FIELD_ATTRS = (
    ("pad", "pad"),
    ("trim", "trim"),
    ("gate", "gate_enabled"),
    ("threshold", "gate_threshold"),
    ("decay", "gate_decay"),
)


def _normalize_input_endpoint(path_dict: dict[str, Any], mode: str,
                              input_spec: InputSpec | None) -> None:
    """Write the full modeled param set onto a path's `b00` input endpoint:
    schema defaults overlaid with the recipe's input-object fields.

    Deterministic by design (spec §3.1): every real export carries a complete
    `b00` param set, and normalizing stops the chassis's gate/trim/pad state
    from leaking into authored presets. Mono models get `{"value": x}`
    wrappers; the stereo ("both") model gets per-channel `{"1","2"}` wrappers
    plus a scalar `StereoLink`.
    """
    b00 = path_dict.get("b00")
    if not (isinstance(b00, dict) and b00.get("slot")):
        return
    stereo = mode == "both"

    values: dict[str, Any] = dict(flowparams.INPUT_HSP_DEFAULTS)
    if mode == "none":
        values.pop("Pad", None)
    for field, attr in _INPUT_FIELD_ATTRS:
        v = getattr(input_spec, attr) if input_spec else None
        if v is None:
            continue
        hsp_name = flowparams.INPUT_FIELD_SPECS[field][0]
        if hsp_name not in values:
            continue  # e.g. Pad on a "none" input (parse already rejects it)
        if isinstance(v, dict):
            values[hsp_name] = {
                ch: flowparams.input_field_to_hsp(field, cv)[1]
                for ch, cv in v.items()
            }
        else:
            values[hsp_name] = flowparams.input_field_to_hsp(field, v)[1]

    params: dict[str, Any] = {}
    for name, v in values.items():
        if stereo:
            if isinstance(v, dict):
                params[name] = {ch: {"value": cv} for ch, cv in v.items()}
            else:
                params[name] = {"1": {"value": v}, "2": {"value": copy.deepcopy(v)}}
        else:
            if isinstance(v, dict):  # per-channel on mono can't happen post-parse
                v = v.get("1")
            params[name] = {"value": v}
    if stereo:
        link = input_spec.link if (input_spec and input_spec.link is not None) \
            else flowparams.STEREO_LINK_DEFAULT
        params["StereoLink"] = {"value": link}
    b00["slot"][0]["params"] = params


def _resolve_jack_impedances(spec: Spec) -> dict[str, str]:
    """Per-jack impedance strings across all paths: the EXPLICIT recipe value
    when any path gives one, else the device-declared default for every jack
    a live source uses.

    Explicitness matters (review F1): a path that merely *uses* a jack
    without naming an impedance is not a "FirstEnabled" request — an explicit
    value from any path wins over other paths' omissions. Only two paths
    giving the same jack **different explicit** values conflict.
    """
    jack_z: dict[str, str] = {}
    explicit: set[str] = set()
    for path_index, path_entry in enumerate(spec.paths):
        mode, input_spec = _effective_input(path_entry, path_index)
        imp = input_spec.impedance if input_spec else None
        for jack in flowparams.jacks_for_mode(mode):
            if isinstance(imp, dict) and jack in imp:
                want = imp[jack]
            elif isinstance(imp, str):
                want = imp
            else:
                # jack used but impedance omitted: default, non-binding
                jack_z.setdefault(jack, flowparams.IMPEDANCE_DEFAULT)
                continue
            if jack in explicit and jack_z[jack] != want:
                raise GenerateError(
                    f"conflicting impedance for {jack}: paths request both "
                    f"{jack_z[jack]!r} and {want!r}."
                )
            jack_z[jack] = want
            explicit.add(jack)
    return jack_z


def _apply_output(path_dict: dict[str, Any], output_spec) -> None:
    """Normalize the path's primary (lane-0 `b13`) output endpoint level/pan:
    schema defaults overlaid with the recipe `output` object. Runs AFTER
    `_emit_structural`, so an explicit `output` wins over a stale structural
    copy; wrapper shape is preserved (only `value` is updated)."""
    b13 = path_dict.get("b13")
    is_output = (isinstance(b13, dict) and b13.get("type") == "output"
                 and b13.get("slot"))
    if not is_output:
        if output_spec is not None:
            raise GenerateError(
                "path has no lane-0 output endpoint (b13); cannot apply "
                "output level/pan."
            )
        return
    values = dict(flowparams.OUTPUT_HSP_DEFAULTS)
    if output_spec is not None:
        if output_spec.level is not None:
            values["gain"] = float(output_spec.level)
        if output_spec.pan is not None:
            values["pan"] = float(output_spec.pan)
    params = b13["slot"][0].setdefault("params", {})
    for name, v in values.items():
        wrapped = params.get(name)
        if isinstance(wrapped, dict) and not (
            "1" in wrapped and isinstance(wrapped.get("1"), dict)
        ):
            wrapped["value"] = v
        else:
            params[name] = {"value": v}


def apply_recipe(
    recipe: dict[str, Any] | Spec,
    library,
    *,
    chassis: dict[str, Any],
    irs: Any = None,
    source: str = "recipe",
) -> dict[str, Any]:
    """Clone `chassis` and replay `recipe` onto it, returning an `.hsp` body dict.

    `recipe` is a spec-shaped dict (parsed via `spec.parse_spec`) or an
    already-parsed `Spec`. `chassis` is a Stadium `.hsp` chassis dict (e.g.
    from `library.load_chassis()`); a non-`hsp` chassis raises `GenerateError`
    (the legacy `.hlx` compose path in `generate.py` owns that shape).
    """
    shape = chassis.get(CHASSIS_SHAPE_KEY, "hlx")
    if shape != "hsp":
        raise GenerateError(
            f"recipe authoring supports only .hsp (Stadium) chassis; got "
            f"shape {shape!r}. Use generate.compose_preset for .hlx output."
        )

    spec = recipe if isinstance(recipe, Spec) else parse_spec(recipe, source=source)

    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    # Reject `ir`/`no_ir`/`trails` on blocks that cannot carry them, matching
    # the guards `generate._compose_preset_hsp` enforces before placement.
    for path_entry, chain in zip(spec.paths, resolved):
        block_entries = [e for e in path_entry.blocks if isinstance(e, BlockEntry)]
        for block_entry, (block, _) in zip(block_entries, chain):
            is_ir = block.model_id.startswith(IR_MODEL_PREFIX)
            if block_entry.ir is not None and not is_ir:
                raise GenerateError(
                    f"block {block.display_name!r} is not an IR block; "
                    f"remove the 'ir' field or change the block"
                )
            if block_entry.no_ir and not is_ir:
                raise GenerateError(
                    f"block {block.display_name!r} is not an IR block; "
                    f"remove the 'no_ir' field or change the block"
                )
            if block_entry.trails is not None and not flowparams.trails_capable(
                block.category, block.model_id
            ):
                raise GenerateError(
                    f"Block {block_entry.block!r} sets \"trails\" but its "
                    f"category is {block.category!r}; trails (harness spillover) "
                    f"applies only to delay, reverb, and FX-Loop blocks."
                )

    enabled_map, param_map = _build_snapshot_overrides(spec, resolved)

    body = copy.deepcopy(chassis)
    # Strip private library annotations — not part of the wire format.
    for k in [k for k in body if isinstance(k, str) and _is_chassis_meta_key(k)]:
        del body[k]

    flow = body.setdefault("preset", {}).setdefault("flow", [])

    # --- input routing + endpoint params (spec §3.1: deterministic) --------
    for path_index, path_entry in enumerate(spec.paths):
        if path_index >= len(flow) or not isinstance(flow[path_index], dict):
            continue
        mode, input_spec = _effective_input(path_entry, path_index)
        mutate.set_input(body, path_index, mode)
        _normalize_input_endpoint(flow[path_index], mode, input_spec)

    # Chassis flows beyond the spec's paths keep their input model but get the
    # same deterministic endpoint-param normalization (a spec with 1 path on a
    # 2-flow chassis must produce the same flow[1] endpoints as regenerating
    # its own `view` projection, which lists every flow).
    device_id = (body.get("meta") or {}).get("device_id") or "stadium_xl"
    from helixgen import controllers as _controllers
    for path_index in range(len(spec.paths), len(flow)):
        path_dict = flow[path_index]
        if not isinstance(path_dict, dict):
            continue
        b00 = path_dict.get("b00")
        model = (b00.get("slot") or [{}])[0].get("model", "") if isinstance(b00, dict) else ""
        mode = _controllers.input_mode_for_model(device_id, model)
        if mode is not None:
            _normalize_input_endpoint(path_dict, mode, None)
        _apply_output(path_dict, None)

    # --- preset-level input impedance (used jacks only) ---------------------
    for jack, z in _resolve_jack_impedances(spec).items():
        body["preset"].setdefault("params", {})[f"{jack}Z"] = z

    # --- block placement (shared core helpers) -----------------------------
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
        path_entry = spec.paths[path_index]
        eff = _assign_positions(path_entry)
        block_entries = [e for e in path_entry.blocks if isinstance(e, BlockEntry)]
        # Per-lane capacity guard: a lane has only 12 user-block slots
        # (b01..b12); a 13th block would otherwise silently overwrite the
        # endpoint slot (matches the legacy `_compose_preset_hsp` guard).
        for lane in (0, 1):
            n = sum(1 for e in block_entries if getattr(e, "lane", 0) == lane)
            if n > _MAX_LANE_SLOTS:
                raise GenerateError(
                    f"Path {path_index} lane {lane} has {n} blocks; only "
                    f"{_MAX_LANE_SLOTS} user slots (b01..b{_MAX_LANE_SLOTS:02d}) "
                    f"per lane available."
                )
        for chain_idx, (block, user_params) in enumerate(chain):
            block_entry = block_entries[chain_idx]
            lane, pos, key = eff[id(block_entry)]

            resolved_irhash: str | None = None
            if block.model_id.startswith(IR_MODEL_PREFIX) and not block_entry.no_ir:
                resolved_irhash = _resolve_irhash(
                    block_default=block.default_irhash,
                    spec_ir=block_entry.ir,
                    irs=irs,
                )

            path_dict[key] = _to_hsp_bnn(
                block, user_params,
                position=pos,
                path_index=lane,
                enabled_base=block_entry.enabled,
                enabled_overrides=enabled_map.get((path_index, chain_idx)),
                param_overrides=param_map.get((path_index, chain_idx)),
                irhash=resolved_irhash,
                raw=block_entry.raw,
                trails=block_entry.trails,
            )
        _emit_splits(path_dict, path_entry, eff)
        _emit_structural(path_dict, path_entry)
        # Output level/pan LAST, so an explicit recipe `output` wins over a
        # verbatim structural endpoint carried from `view`.
        _apply_output(path_dict, path_entry.output)

    # --- controller wiring (mutate verbs) ----------------------------------
    # Reset chassis-carryover scribble strips: the cloned chassis carries the
    # ORIGINATING preset's fs_label/fs_color values, which would silently leak
    # stale labels onto an authored tone (and read back out of `view` as if
    # the recipe had set them). Authored output carries only what the recipe
    # declares; bypass/fs_topidx keep the chassis shape.
    for entry in (body.get("preset", {}).get("sources") or {}).values():
        if isinstance(entry, dict):
            if "fs_label" in entry:
                entry["fs_label"] = ""
            if "fs_color" in entry:
                entry["fs_color"] = "auto"

    for fs in spec.footswitches:
        mutate.wire_footswitch(
            body, fs.switch, fs.block, fs.behavior, library,
            path=fs.path, lane=fs.lane, pos=fs.pos,
            param=fs.param, min=fs.min, max=fs.max,
            curve=fs.curve, threshold=fs.threshold,
            label=fs.label, color=fs.color,
        )
    for assignment in spec.expression:
        targets = [
            {
                "block": t.block, "param": t.param, "min": t.min, "max": t.max,
                "path": t.path, "lane": t.lane, "pos": t.pos, "curve": t.curve,
                "threshold": t.threshold,
            }
            for t in assignment.targets
        ]
        mutate.wire_expression(body, assignment.pedal, targets, library)
    for assignment in spec.midi:
        midi_targets = [
            {
                "block": t.block, "param": t.param, "bypass": t.bypass,
                "min": t.min, "max": t.max,
                "path": t.path, "lane": t.lane, "pos": t.pos,
            }
            for t in assignment.targets
        ]
        mutate.wire_midi(body, assignment.cc, midi_targets, library)

    # --- snapshot metadata + active-snapshot pointer -----------------------
    body["preset"]["snapshots"] = _build_snapshot_metadata(spec)
    body["preset"].setdefault("params", {})["activesnapshot"] = 0

    # --- meta + provenance --------------------------------------------------
    meta = body.setdefault("meta", {})
    meta["name"] = spec.name
    if spec.author is not None:
        meta["author"] = spec.author
    meta["helixgen"] = _provenance(source)
    return body


def generate_from_recipe(
    recipe: dict[str, Any] | Spec,
    library,
    *,
    irs: Any = None,
    chassis: dict[str, Any],
    source: str = "recipe",
) -> bytes:
    """Author a `.hsp` body from `recipe` and serialize it to Stadium bytes."""
    return dumps_hsp(apply_recipe(recipe, library, chassis=chassis, irs=irs, source=source))
