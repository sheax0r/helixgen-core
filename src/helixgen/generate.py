"""Generate: turn a parsed Spec + Library into a .hlx or .hsp preset dict.

Dispatches on `chassis._helixgen_chassis_shape`:
- "hlx" (or absent) → legacy Helix .hlx shape (data.tone.dspN.blockN/cabN)
- "hsp"             → Stadium .hsp shape (preset.flow[i].bNN.slot[0])
"""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
from typing import Any

from helixgen import __version__
from helixgen.chassis import CHASSIS_SHAPE_KEY
from helixgen.hsp import HSP_MAGIC, translate_to_hsp
from helixgen.ingest import (
    DSP_BLOCK_KEY_PREFIX,
    DSP_CAB_KEY_PREFIX,
    PRESET_DSP_KEYS,
    RAW_BLOCK_CAB_LINK_KEY,
    RAW_BLOCK_MODEL_KEY,
    RAW_BLOCK_SYSTEM_KEY_PREFIX,
)
from helixgen.library import Block, Library
from helixgen.spec import Spec, parse_spec


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


def _is_amp(block: Block) -> bool:
    return block.category == "amp"


def _is_cab(block: Block) -> bool:
    return block.category == "cab"


def compose_preset(spec: Spec, library: Library, *, source: str) -> dict[str, Any]:
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
        return _compose_preset_hsp(spec, library, source=source, chassis=chassis)
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
        for block, user_params in chain:
            placed = copy.deepcopy(block.exemplar)
            for k, v in user_params.items():
                placed[k] = v

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


def _is_chassis_meta_key(key: str) -> bool:
    """True for top-level chassis annotations that must not appear in output."""
    return key.startswith("_helixgen_")


def _to_hsp_bnn(
    block: Block, user_params: dict[str, Any], *, position: int, path_index: int
) -> dict[str, Any]:
    """Build one Stadium bNN dict from a library Block and user param overrides.

    Returns the bNN-level shape `{type, position, path, slot: [{...}]}`.
    """
    flat = copy.deepcopy(block.exemplar)
    for k, v in user_params.items():
        flat[k] = v

    slot_inner: dict[str, Any] = {
        "model": translate_to_hsp(flat.get(RAW_BLOCK_MODEL_KEY, block.model_id)),
    }
    # @enabled defaults to True if absent
    slot_inner["@enabled"] = {"value": flat.get("@enabled", True)}
    if "@version" in flat:
        slot_inner["version"] = flat["@version"]

    params: dict[str, Any] = {}
    for k, v in flat.items():
        if not isinstance(k, str) or k.startswith(RAW_BLOCK_SYSTEM_KEY_PREFIX):
            continue
        params[k] = {"value": v}
    slot_inner["params"] = params

    bnn: dict[str, Any] = {
        # bNN-level @enabled is the device's bypass switch. Real exports
        # always carry it (sometimes wrapped in a controller block for
        # footswitch assignments — we emit the plain form). Defaulting to
        # True here means every block the user places in a spec loads
        # enabled, which is what they almost always want.
        "@enabled": {"value": True},
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


def _compose_preset_hsp(
    spec: Spec, library: Library, *, source: str, chassis: dict[str, Any]
) -> dict[str, Any]:
    """Compose a .hsp-shape preset. See module docstring for shape notes."""
    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    preset = copy.deepcopy(chassis)
    # Strip private library annotations — these are not part of the wire format.
    for k in [k for k in preset if isinstance(k, str) and _is_chassis_meta_key(k)]:
        del preset[k]

    flow = preset.setdefault("preset", {}).setdefault("flow", [])

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
        for slot_index, (block, user_params) in enumerate(chain, start=1):
            key = f"b{slot_index:02d}"
            path_dict[key] = _to_hsp_bnn(
                block, user_params, position=slot_index, path_index=path_index
            )

    meta = preset.setdefault("meta", {})
    meta["name"] = spec.name
    if spec.author is not None:
        meta["author"] = spec.author
    meta["helixgen"] = _provenance(source)
    return preset


def generate_preset(spec_path: Path, output_path: Path, library: Library) -> Path:
    """Top-level: read spec from disk, compose, write output.

    Output format follows the chassis shape: .hlx → pretty JSON; .hsp →
    8-byte magic header + compact JSON (so a Stadium can re-read it).
    """
    spec_path = Path(spec_path)
    output_path = Path(output_path)

    raw = json.loads(spec_path.read_text())
    spec = parse_spec(raw, source=str(spec_path))
    preset = compose_preset(spec, library, source=str(spec_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    shape = library.load_chassis().get(CHASSIS_SHAPE_KEY, "hlx")
    if shape == "hsp":
        body = json.dumps(preset, separators=(",", ":")).encode("utf-8")
        output_path.write_bytes(HSP_MAGIC + body)
    else:
        output_path.write_text(json.dumps(preset, indent=2))
    return output_path
