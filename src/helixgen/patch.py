"""Pure spec-dict transforms for surgical preset edits.

Each verb deep-copies the spec, mutates it, and returns the new dict. Block
addressing is by display name, disambiguated by (path, index). Validation of
param names/ranges is deferred to generate.py at regeneration time.
"""
from __future__ import annotations

import copy
from typing import Any

from helixgen.ir import IR_MODEL_PREFIX
from helixgen.library import Library


class PatchError(ValueError):
    """A surgical edit could not be applied (bad address, etc.)."""


def resolve_block(
    spec: dict,
    name: str,
    path: int | None,
    index: int | None,
    *,
    lane: int | None = None,
    pos: int | None = None,
) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    for pi, p in enumerate(spec.get("paths", [])):
        for bi, b in enumerate(p.get("blocks", [])):
            if b.get("block") != name:
                continue
            if lane is not None and b.get("lane", 0) != lane:
                continue
            if pos is not None and b.get("pos") != pos:
                continue
            matches.append((pi, bi))
    if path is not None and index is not None:
        if (path, index) in matches:
            return (path, index)
        raise PatchError(f"No block {name!r} at path {path} index {index}.")
    if not matches:
        raise PatchError(f"Block {name!r} is not in the spec (with the given lane/pos). Placed blocks: "
                         f"{[b.get('block') for p in spec.get('paths', []) for b in p.get('blocks', [])]}.")
    if len(matches) > 1:
        raise PatchError(f"Block {name!r} matches {len(matches)} placements; "
                         f"disambiguate with --lane/--path/--index.")
    return matches[0]


def set_param(spec, block, param, value, *, path=None, index=None, lane=None, pos=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index, lane=lane, pos=pos)
    out["paths"][pi]["blocks"][bi].setdefault("params", {})[param] = value
    return out


def set_enabled(spec, block, enabled, *, path=None, index=None, lane=None, pos=None, snapshot=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index, lane=lane, pos=pos)
    if snapshot is None:
        out["paths"][pi]["blocks"][bi]["enabled"] = enabled
        return out
    snaps = out.get("snapshots", [])
    target = next((s for s in snaps if s.get("name") == snapshot), None)
    if target is None:
        raise PatchError(f"No snapshot named {snapshot!r}.")
    disable = target.setdefault("disable", [])
    if enabled and block in disable:
        disable.remove(block)
    elif not enabled and block not in disable:
        disable.append(block)
    return out


def add_block(spec, block, *, path=0, after=None, params=None, lane=None, pos=None) -> dict:
    out = copy.deepcopy(spec)
    if path >= len(out.get("paths", [])):
        raise PatchError(f"No path {path} in spec.")
    blocks = out["paths"][path]["blocks"]
    entry: dict[str, Any] = {"block": block}
    if params:
        entry["params"] = dict(params)
    if after is None:
        blocks.append(entry)
    else:
        matches = [i for i, b in enumerate(blocks) if b.get("block") == after]
        if not matches:
            raise PatchError(f"Block {after!r} not found in path {path}.")
        if len(matches) > 1:
            raise PatchError(
                f"Block {after!r} appears multiple times in path {path}; "
                f"cannot choose an insertion point.")
        blocks.insert(matches[0] + 1, entry)
    return out


def remove_block(spec, block, *, path=None, index=None, lane=None, pos=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index, lane=lane, pos=pos)
    del out["paths"][pi]["blocks"][bi]
    return out


def swap_model(spec, old, new, library: Library, *, path=None, index=None, lane=None, pos=None):
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, old, path, index, lane=lane, pos=pos)
    entry = out["paths"][pi]["blocks"][bi]

    try:
        old_block = library.find_block(old)
        new_block = library.find_block(new)
    except (KeyError, LookupError) as e:
        raise PatchError(str(e)) from e

    if old_block.category != new_block.category:
        raise PatchError(
            f"Cannot swap {old!r} ({old_block.category}) for {new!r} "
            f"({new_block.category}): categories differ.")

    warnings: list[str] = []
    old_params = entry.get("params", {})
    new_keys = set(new_block.params.keys())
    carried = {k: v for k, v in old_params.items() if k in new_keys}
    dropped = sorted(set(old_params) - new_keys)
    if dropped:
        warnings.append(
            f"swap {old!r}→{new!r}: dropped param(s) {dropped} not on target.")

    entry["block"] = new_block.display_name
    if carried:
        entry["params"] = carried
    else:
        entry.pop("params", None)

    # Preserve IR ref only when the target is also an IR block.
    if entry.get("ir") is not None and not new_block.model_id.startswith(IR_MODEL_PREFIX):
        entry.pop("ir", None)
        warnings.append(f"swap {old!r}→{new!r}: dropped 'ir' (target is not an IR block).")

    return out, warnings
