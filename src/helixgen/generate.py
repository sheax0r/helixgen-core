"""Generate: turn a parsed Spec + Library into a .hlx preset dict."""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
from typing import Any

from helixgen import __version__
from helixgen.ingest import (
    DSP_BLOCK_KEY_PREFIX,
    DSP_CAB_KEY_PREFIX,
    PRESET_DSP_KEYS,
    RAW_BLOCK_CAB_LINK_KEY,
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
    """Build the final preset dict from a Spec + Library.

    For each path in the spec, place chain entries into the matching dsp:
    - Non-cab blocks go to sequential `block0`, `block1`, ... slots.
    - Cab blocks go to sequential `cab0`, `cab1`, ... slots.
    - When an amp is followed by a cab, the amp's `@cab` is set to the cab's
      slot key so Stadium/Helix renders the pairing correctly.
    """
    if not library.has_chassis():
        raise GenerateError(
            "Library has no chassis. Run `helixgen ingest <real-export.hlx>` first."
        )

    resolved = resolve_blocks(spec, library)
    for chain in resolved:
        for block, user_params in chain:
            validate_params(block, user_params)

    preset = copy.deepcopy(library.load_chassis())
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
    meta["helixgen"] = {
        "version": __version__,
        "spec_source": source,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

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
