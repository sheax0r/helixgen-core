"""Pure spec-dict transforms for surgical preset edits.

Each verb deep-copies the spec, mutates it, and returns the new dict. Block
addressing is by display name, disambiguated by (path, index). Validation of
param names/ranges is deferred to generate.py at regeneration time.
"""
from __future__ import annotations

import copy
from typing import Any


class PatchError(ValueError):
    """A surgical edit could not be applied (bad address, etc.)."""


def resolve_block(spec: dict, name: str, path: int | None, index: int | None) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    for pi, p in enumerate(spec.get("paths", [])):
        for bi, b in enumerate(p.get("blocks", [])):
            if b.get("block") == name:
                matches.append((pi, bi))
    if path is not None and index is not None:
        if (path, index) in matches:
            return (path, index)
        raise PatchError(f"No block {name!r} at path {path} index {index}.")
    if not matches:
        raise PatchError(f"Block {name!r} is not in the spec. Placed blocks: "
                         f"{[b.get('block') for p in spec.get('paths', []) for b in p.get('blocks', [])]}.")
    if len(matches) > 1:
        raise PatchError(f"Block {name!r} matches {len(matches)} placements; "
                         f"disambiguate with --path/--index.")
    return matches[0]


def set_param(spec, block, param, value, *, path=None, index=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index)
    out["paths"][pi]["blocks"][bi].setdefault("params", {})[param] = value
    return out


def set_enabled(spec, block, enabled, *, path=None, index=None, snapshot=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index)
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


def add_block(spec, block, *, path=0, after=None, params=None) -> dict:
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


def remove_block(spec, block, *, path=None, index=None) -> dict:
    out = copy.deepcopy(spec)
    pi, bi = resolve_block(out, block, path, index)
    del out["paths"][pi]["blocks"][bi]
    return out
