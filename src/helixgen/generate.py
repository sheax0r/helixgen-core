"""Generate: turn a parsed Spec + Library into a .hlx preset dict."""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
from typing import Any

from helixgen import __version__
from helixgen.ingest import PRESET_DSP_KEYS
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


def compose_preset(spec: Spec, library: Library, *, source: str) -> dict[str, Any]:
    """Build the final preset dict from a Spec + Library."""
    if not library.has_chassis():
        raise GenerateError(
            "Library has no chassis. Run `helixgen ingest <real-export.hlx>` first."
        )

    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    preset = copy.deepcopy(library.load_chassis())
    position_keys = preset.get("_helixgen", {}).get("position_keys", {"dsp0": [], "dsp1": []})

    for path_index, chain in enumerate(resolved):
        if path_index >= len(PRESET_DSP_KEYS):
            raise GenerateError(
                f"Spec has {len(resolved)} paths but only {len(PRESET_DSP_KEYS)} DSPs available."
            )
        dsp_key = PRESET_DSP_KEYS[path_index]
        slots = position_keys.get(dsp_key, [])
        if len(chain) > len(slots):
            raise GenerateError(
                f"Path {path_index} has more blocks ({len(chain)}) than chassis "
                f"slots on {dsp_key} ({len(slots)})."
            )

        spec_path = spec.paths[path_index]
        dsp = preset["data"]["tone"][dsp_key]
        if spec_path.input is not None:
            dsp["input"] = spec_path.input
        if spec_path.output is not None:
            dsp["output"] = spec_path.output

        dsp["blocks"] = {}
        for slot, (block, user_params) in zip(slots, chain):
            placed = copy.deepcopy(block.exemplar)
            for k, v in user_params.items():
                placed[k] = v
            dsp["blocks"][slot] = placed

    meta = preset["data"].setdefault("meta", {})
    meta["name"] = spec.name
    if spec.author is not None:
        meta["author"] = spec.author
    meta["helixgen"] = {
        "version": __version__,
        "spec_source": source,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    preset.pop("_helixgen", None)
    return preset


def generate_preset(spec_path: Path, output_path: Path, library: Library) -> Path:
    """Top-level: read spec from disk, compose, write output."""
    spec_path = Path(spec_path)
    output_path = Path(output_path)

    raw = json.loads(spec_path.read_text())
    spec = parse_spec(raw, source=str(spec_path))
    preset = compose_preset(spec, library, source=str(spec_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(preset, indent=2))
    return output_path
