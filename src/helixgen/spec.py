"""Spec: parse + validate the JSON tone description that `generate` consumes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SpecError(ValueError):
    """Raised when a spec is structurally invalid."""


@dataclass
class BlockEntry:
    block: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class PathEntry:
    blocks: list[BlockEntry]
    input: str | None = None
    output: str | None = None


@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None


def _err(source: str, message: str) -> SpecError:
    return SpecError(f"Spec at {source}: {message}")


def parse_spec(data: Any, *, source: str = "<input>") -> Spec:
    """Parse and validate a spec dict. Raises SpecError on any structural problem."""
    if not isinstance(data, dict):
        raise _err(source, "top-level value must be an object.")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise _err(source, '"name" is required and must be a non-empty string.')

    author = data.get("author")
    if author is not None and not isinstance(author, str):
        raise _err(source, '"author" must be a string if provided.')

    paths_raw = data.get("paths")
    if not isinstance(paths_raw, list):
        raise _err(source, '"paths" must be an array.')
    if len(paths_raw) == 0:
        raise _err(source, '"paths" must contain at least one chain.')
    if len(paths_raw) > 2:
        raise _err(
            source,
            f'"paths" length {len(paths_raw)} not supported (max 2 — one per DSP).',
        )

    paths: list[PathEntry] = []
    for i, path_raw in enumerate(paths_raw):
        paths.append(_parse_path(path_raw, source=f"{source} paths[{i}]"))

    return Spec(name=name, paths=paths, author=author)


def _parse_path(data: Any, *, source: str) -> PathEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    inp = data.get("input")
    if inp is not None and not isinstance(inp, str):
        raise _err(source, '"input" must be a string if provided.')
    out = data.get("output")
    if out is not None and not isinstance(out, str):
        raise _err(source, '"output" must be a string if provided.')

    blocks_raw = data.get("blocks")
    if not isinstance(blocks_raw, list):
        raise _err(source, '"blocks" must be an array.')

    blocks: list[BlockEntry] = []
    for i, b in enumerate(blocks_raw):
        blocks.append(_parse_block_entry(b, source=f"{source} blocks[{i}]"))

    return PathEntry(blocks=blocks, input=inp, output=out)


def _parse_block_entry(data: Any, *, source: str) -> BlockEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    if "parallel" in data:
        raise _err(
            source,
            '"parallel" entries not supported in v1. '
            "See docs/features/parallel-paths.md.",
        )

    name = data.get("block")
    if not isinstance(name, str) or not name:
        raise _err(source, '"block" is required and must be a non-empty string.')

    params = data.get("params", {})
    if not isinstance(params, dict):
        raise _err(source, '"params" must be an object if provided.')

    return BlockEntry(block=name, params=dict(params))
