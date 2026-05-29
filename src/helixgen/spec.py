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
    ir: str | None = None


@dataclass
class PathEntry:
    blocks: list[BlockEntry]
    input: str | None = None
    output: str | None = None


@dataclass
class Snapshot:
    """One named snapshot (Stadium scene). Each snapshot is a delta from the
    path's base block enabled-state and param values.
    """
    name: str
    disable: list[str] = field(default_factory=list)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)


SNAPSHOT_MAX = 8  # Stadium hardware cap


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

    snapshots = _parse_snapshots(data.get("snapshots"), source=source)

    return Spec(name=name, paths=paths, author=author, snapshots=snapshots)


def _parse_snapshots(raw: Any, *, source: str) -> list[Snapshot]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"snapshots" must be a list.')
    if len(raw) > SNAPSHOT_MAX:
        raise _err(
            source,
            f'"snapshots" has {len(raw)} entries; Stadium supports at most {SNAPSHOT_MAX}.',
        )
    return [
        _parse_snapshot(entry, source=f"{source} snapshots[{i}]")
        for i, entry in enumerate(raw)
    ]


def _parse_snapshot(data: Any, *, source: str) -> Snapshot:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise _err(source, '"name" is required and must be a non-empty string.')

    disable_raw = data.get("disable", [])
    if not isinstance(disable_raw, list) or not all(isinstance(x, str) for x in disable_raw):
        raise _err(source, '"disable" must be a list of block-name strings.')

    params_raw = data.get("params", {})
    if not isinstance(params_raw, dict):
        raise _err(source, '"params" must be an object if provided.')
    params: dict[str, dict[str, Any]] = {}
    for block_name, overrides in params_raw.items():
        if not isinstance(overrides, dict):
            raise _err(
                f"{source} params[{block_name!r}]",
                "must be an object mapping param names to values.",
            )
        params[block_name] = dict(overrides)

    return Snapshot(name=name, disable=list(disable_raw), params=params)


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

    ir = data.get("ir")
    if ir is not None and not isinstance(ir, str):
        raise _err(source, '"ir" must be a string if provided.')

    return BlockEntry(block=name, params=dict(params), ir=ir)
