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
class FootswitchAssignment:
    """A single FS-to-block bypass assignment.

    `switch` is a logical name (e.g. "FS3"); the chassis-specific source
    ID is resolved at generate time.
    """
    switch: str
    block: str
    behavior: str = "latching"


@dataclass
class ExpressionTarget:
    block: str
    param: str
    min: float = 0.0
    max: float = 1.0


@dataclass
class ExpressionAssignment:
    pedal: str
    targets: list[ExpressionTarget] = field(default_factory=list)


@dataclass
class Spec:
    name: str
    paths: list[PathEntry]
    author: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
    footswitches: list[FootswitchAssignment] = field(default_factory=list)
    expression: list[ExpressionAssignment] = field(default_factory=list)


SNAPSHOT_MAX = 8  # Stadium hardware cap
VALID_INPUT_MODES = ("inst1", "inst2", "both", "none")
VALID_FS_BEHAVIORS = ("latching", "momentary")


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
    footswitches = _parse_footswitches(data.get("footswitches"), source=source)
    expression = _parse_expression(data.get("expression"), source=source)
    return Spec(
        name=name, paths=paths, author=author,
        snapshots=snapshots, footswitches=footswitches, expression=expression,
    )


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


def _parse_footswitches(raw: Any, *, source: str) -> list[FootswitchAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"footswitches" must be a list.')
    out: list[FootswitchAssignment] = []
    seen_switches: set[str] = set()
    seen_blocks: set[str] = set()
    for i, entry in enumerate(raw):
        fs = _parse_footswitch(entry, source=f"{source} footswitches[{i}]")
        if fs.switch in seen_switches:
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate switch {fs.switch!r}; each switch may appear once.",
            )
        if fs.block in seen_blocks:
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate block {fs.block!r}; one block per footswitch.",
            )
        seen_switches.add(fs.switch)
        seen_blocks.add(fs.block)
        out.append(fs)
    return out


def _parse_footswitch(data: Any, *, source: str) -> FootswitchAssignment:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    switch = data.get("switch")
    if not isinstance(switch, str) or not switch:
        raise _err(source, '"switch" is required and must be a non-empty string.')
    block = data.get("block")
    if not isinstance(block, str) or not block:
        raise _err(source, '"block" is required and must be a non-empty string.')
    behavior = data.get("behavior", "latching")
    if behavior not in VALID_FS_BEHAVIORS:
        raise _err(
            source,
            f'"behavior" must be one of {list(VALID_FS_BEHAVIORS)} (got {behavior!r}).',
        )
    return FootswitchAssignment(switch=switch, block=block, behavior=behavior)


def _parse_expression(raw: Any, *, source: str) -> list[ExpressionAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"expression" must be a list.')
    out: list[ExpressionAssignment] = []
    seen_pedals: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for i, entry in enumerate(raw):
        assignment = _parse_expression_assignment(
            entry, source=f"{source} expression[{i}]"
        )
        if assignment.pedal in seen_pedals:
            raise _err(
                f"{source} expression[{i}]",
                f"duplicate pedal {assignment.pedal!r}; each pedal may appear once.",
            )
        seen_pedals.add(assignment.pedal)
        for j, t in enumerate(assignment.targets):
            key = (t.block, t.param)
            if key in seen_targets:
                raise _err(
                    f"{source} expression[{i}] targets[{j}]",
                    f"duplicate (block, param) {key!r}; one param per pedal across the spec.",
                )
            seen_targets.add(key)
        out.append(assignment)
    return out


def _parse_expression_assignment(data: Any, *, source: str) -> ExpressionAssignment:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    pedal = data.get("pedal")
    if not isinstance(pedal, str) or not pedal:
        raise _err(source, '"pedal" is required and must be a non-empty string.')
    targets_raw = data.get("targets")
    if not isinstance(targets_raw, list) or len(targets_raw) == 0:
        raise _err(source, '"targets" must be a non-empty list.')
    targets = [
        _parse_expression_target(t, source=f"{source} targets[{j}]")
        for j, t in enumerate(targets_raw)
    ]
    return ExpressionAssignment(pedal=pedal, targets=targets)


def _parse_expression_target(data: Any, *, source: str) -> ExpressionTarget:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    block = data.get("block")
    if not isinstance(block, str) or not block:
        raise _err(source, '"block" is required and must be a non-empty string.')
    param = data.get("param")
    if not isinstance(param, str) or not param:
        raise _err(source, '"param" is required and must be a non-empty string.')
    mn = data.get("min", 0.0)
    mx = data.get("max", 1.0)
    for label, val in (("min", mn), ("max", mx)):
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise _err(source, f'"{label}" must be a number.')
        if val < 0.0 or val > 1.0:
            raise _err(source, f'"{label}" must be in [0.0, 1.0] (got {val}).')
    if mn > mx:
        raise _err(source, f'"min" must be <= "max" (got min={mn}, max={mx}).')
    return ExpressionTarget(block=block, param=param, min=float(mn), max=float(mx))


def _parse_path(data: Any, *, source: str) -> PathEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    inp = data.get("input")
    if inp is not None:
        if not isinstance(inp, str):
            raise _err(source, '"input" must be a string if provided.')
        if inp not in VALID_INPUT_MODES:
            raise _err(
                source,
                f'"input" must be one of {list(VALID_INPUT_MODES)} '
                f'(got "{inp}").',
            )
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
