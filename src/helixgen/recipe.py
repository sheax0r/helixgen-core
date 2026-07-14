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
from helixgen import mutate
from helixgen.chassis import CHASSIS_SHAPE_KEY
from helixgen.generate import (
    DEFAULT_INPUT_MODES,
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
from helixgen.spec import BlockEntry, Spec, parse_spec

_MAX_LANE_SLOTS = 12  # b01..b12 user-block slots per lane


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
            if block_entry.trails is not None and block.category not in ("delay", "reverb"):
                raise GenerateError(
                    f"Block {block_entry.block!r} sets \"trails\" but its "
                    f"category is {block.category!r}; trails (harness spillover) "
                    f"applies only to delay and reverb blocks."
                )

    enabled_map, param_map = _build_snapshot_overrides(spec, resolved)

    body = copy.deepcopy(chassis)
    # Strip private library annotations — not part of the wire format.
    for k in [k for k in body if isinstance(k, str) and _is_chassis_meta_key(k)]:
        del body[k]

    flow = body.setdefault("preset", {}).setdefault("flow", [])

    # --- input routing (mutate verb) ---------------------------------------
    for path_index, path_entry in enumerate(spec.paths):
        if path_index >= len(flow) or not isinstance(flow[path_index], dict):
            continue
        mode = path_entry.input or DEFAULT_INPUT_MODES[path_index]
        mutate.set_input(body, path_index, mode)

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
