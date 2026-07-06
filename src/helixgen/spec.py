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
    no_ir: bool = False
    enabled: bool | None = None
    lane: int = 0
    pos: int | None = None
    raw: dict[str, Any] | None = None


@dataclass
class SplitEntry:
    model: str
    params: dict[str, Any] = field(default_factory=dict)
    lane: int = 0
    pos: int | None = None


@dataclass
class JoinEntry:
    model: str = "P35_AppDSPJoin"
    params: dict[str, Any] = field(default_factory=dict)
    lane: int = 0
    pos: int | None = None


@dataclass
class StructuralEntry:
    """A routing-skeleton slot (endpoint or orphaned split/join) captured
    verbatim. `raw` is the exact bNN wire dict; generate re-emits it as-is at
    `b{14*lane+pos:02d}`. Never consulted against the block library."""
    raw: dict[str, Any]
    lane: int = 0
    pos: int | None = None


@dataclass
class PathEntry:
    blocks: list
    input: str | None = None
    output: str | None = None


@dataclass
class SnapshotBlockRef:
    """A reference to a placed block from within a snapshot. `block` is the
    display_name; optional `path`/`lane`/`pos` disambiguate when multiple
    placed blocks share the same display_name (common: humanized generic
    names like "Stereo"/"Mono").
    """
    block: str
    path: int | None = None
    lane: int | None = None
    pos: int | None = None


@dataclass
class SnapshotParamOverride:
    """One block's param overrides within a snapshot."""
    ref: SnapshotBlockRef
    params: dict[str, Any]


@dataclass
class Snapshot:
    """One named snapshot (Stadium scene). Each snapshot is a delta from the
    path's base block enabled-state and param values.
    """
    name: str
    disable: list[SnapshotBlockRef] = field(default_factory=list)
    params: list[SnapshotParamOverride] = field(default_factory=list)


@dataclass
class FootswitchAssignment:
    """A single FS-to-block bypass assignment.

    `switch` is a logical name (e.g. "FS3"); the chassis-specific source
    ID is resolved at generate time.  Optional `path`/`lane`/`pos` disambiguate
    when multiple placed blocks share the same display_name.
    """
    switch: str
    block: str
    behavior: str = "latching"
    path: int | None = None
    lane: int | None = None
    pos: int | None = None


@dataclass
class ExpressionTarget:
    block: str
    param: str
    min: float = 0.0
    max: float = 1.0
    path: int | None = None
    lane: int | None = None
    pos: int | None = None


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


def _opt_int(v: Any, *, source: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise _err(source, "must be an integer.")
    return v


def _parse_snapshot_ref(entry: Any, *, source: str) -> "SnapshotBlockRef":
    if isinstance(entry, str):
        if not entry:
            raise _err(source, '"block" must be a non-empty string.')
        return SnapshotBlockRef(block=entry)
    if not isinstance(entry, dict):
        raise _err(source, "must be a string or a {block, lane, pos} object.")
    block = entry.get("block")
    if not isinstance(block, str) or not block:
        raise _err(source, '"block" is required and must be a non-empty string.')
    return SnapshotBlockRef(
        block=block,
        path=_opt_int(entry.get("path"), source=f"{source} path"),
        lane=_opt_int(entry.get("lane"), source=f"{source} lane"),
        pos=_opt_int(entry.get("pos"),  source=f"{source} pos"),
    )


def _parse_snapshot(data: Any, *, source: str) -> Snapshot:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise _err(source, '"name" is required and must be a non-empty string.')

    disable_raw = data.get("disable", [])
    if not isinstance(disable_raw, list):
        raise _err(source, '"disable" must be a list.')
    disable = [_parse_snapshot_ref(e, source=f"{source} disable[{i}]")
               for i, e in enumerate(disable_raw)]

    params_raw = data.get("params", {})
    params: list[SnapshotParamOverride] = []
    if isinstance(params_raw, dict):
        for block_name, ov in params_raw.items():
            if not isinstance(ov, dict):
                raise _err(source, f'params[{block_name!r}] must be an object.')
            params.append(SnapshotParamOverride(
                ref=SnapshotBlockRef(block=block_name), params=ov))
    elif isinstance(params_raw, list):
        for i, e in enumerate(params_raw):
            if not isinstance(e, dict):
                raise _err(source, f'params[{i}] must be an object.')
            pov = e.get("params")
            if not isinstance(pov, dict):
                raise _err(source, f'params[{i}]: "params" must be an object.')
            ref = _parse_snapshot_ref(
                {k: v for k, v in e.items() if k != "params"},
                source=f"{source} params[{i}]")
            params.append(SnapshotParamOverride(ref=ref, params=pov))
    else:
        raise _err(source, '"params" must be an object or a list.')

    return Snapshot(name=name, disable=disable, params=params)


def _parse_footswitches(raw: Any, *, source: str) -> list[FootswitchAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"footswitches" must be a list.')
    out: list[FootswitchAssignment] = []
    seen_blocks: set[tuple] = set()
    for i, entry in enumerate(raw):
        fs = _parse_footswitch(entry, source=f"{source} footswitches[{i}]")
        block_key = (fs.block, fs.path, fs.lane, fs.pos)
        if block_key in seen_blocks:
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate block {fs.block!r}; one block per footswitch.",
            )
        seen_blocks.add(block_key)
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
    path = data.get("path")
    if path is not None and (not isinstance(path, int) or isinstance(path, bool) or path < 0):
        raise _err(source, '"path" must be a non-negative integer if provided.')
    lane = data.get("lane")
    if lane is not None and lane not in (0, 1):
        raise _err(source, '"lane" must be 0 or 1 if provided.')
    pos = data.get("pos")
    if pos is not None and (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0):
        raise _err(source, '"pos" must be a non-negative integer if provided.')
    return FootswitchAssignment(switch=switch, block=block, behavior=behavior,
                                path=path, lane=lane, pos=pos)


def _parse_expression(raw: Any, *, source: str) -> list[ExpressionAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"expression" must be a list.')
    out: list[ExpressionAssignment] = []
    seen_pedals: set[str] = set()
    seen_targets: set[tuple] = set()
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
            # Include coordinate fields so two same-name blocks at different
            # positions can each carry an EXP target on the same param name
            # (mirrors the coordinate-aware FS duplicate check from task 9).
            key = (t.block, t.param, t.path, t.lane, t.pos)
            if key in seen_targets:
                raise _err(
                    f"{source} expression[{i}] targets[{j}]",
                    f"duplicate (block, param, pos) {(t.block, t.param, t.pos)!r}; "
                    f"one param per pedal per block-coordinate across the spec.",
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
    # Inverted ranges (min > max) are valid: real presets use them for reverse
    # heel-to-toe sweeps (e.g. min=0.85, max=0.67).
    path = data.get("path")
    if path is not None and (not isinstance(path, int) or isinstance(path, bool) or path < 0):
        raise _err(source, '"path" must be a non-negative integer if provided.')
    lane = data.get("lane")
    if lane is not None and lane not in (0, 1):
        raise _err(source, '"lane" must be 0 or 1 if provided.')
    pos = data.get("pos")
    if pos is not None and (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0):
        raise _err(source, '"pos" must be a non-negative integer if provided.')
    return ExpressionTarget(block=block, param=param, min=float(mn), max=float(mx),
                            path=path, lane=lane, pos=pos)


def _parse_lane_pos(data: dict, *, source: str) -> tuple[int, int | None]:
    lane = data.get("lane", 0)
    if lane not in (0, 1):
        raise _err(source, f'"lane" must be 0 or 1 (got {lane!r}).')
    pos = data.get("pos")
    if pos is not None and (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0):
        raise _err(source, f'"pos" must be a non-negative integer if provided (got {pos!r}).')
    return lane, pos


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

    blocks = [_parse_path_entry(b, source=f"{source} blocks[{i}]")
              for i, b in enumerate(blocks_raw)]
    _validate_splits(blocks, source=source)
    return PathEntry(blocks=blocks, input=inp, output=out)


def _parse_path_entry(data: Any, *, source: str):
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")
    if "structural" in data:
        raw = data["structural"]
        if not isinstance(raw, dict):
            raise _err(source, '"structural" must be an object (verbatim bNN dict).')
        lane, pos = _parse_lane_pos(data, source=source)
        if pos is None:
            raise _err(source, '"structural" entries require an explicit integer "pos".')
        return StructuralEntry(raw=raw, lane=lane, pos=pos)
    if "split" in data:
        sd = data["split"]
        if not isinstance(sd, dict) or not isinstance(sd.get("model"), str):
            raise _err(source, '"split" must be an object with a "model" string.')
        lane, pos = _parse_lane_pos(data, source=source)
        return SplitEntry(model=sd["model"], params=dict(sd.get("params", {})), lane=lane, pos=pos)
    if "join" in data:
        jd = data["join"] or {}
        if not isinstance(jd, dict):
            raise _err(source, '"join" must be an object if provided.')
        lane, pos = _parse_lane_pos(data, source=source)
        return JoinEntry(model=jd.get("model", "P35_AppDSPJoin"),
                         params=dict(jd.get("params", {})), lane=lane, pos=pos)
    # plain block (existing logic) + lane/pos
    if "parallel" in data:
        raise _err(source, '"parallel" entries not supported; use split/join.')
    name = data.get("block")
    if not isinstance(name, str) or not name:
        raise _err(source, '"block" is required and must be a non-empty string.')
    params = data.get("params", {})
    if not isinstance(params, dict):
        raise _err(source, '"params" must be an object if provided.')
    ir = data.get("ir")
    if ir is not None and not isinstance(ir, str):
        raise _err(source, '"ir" must be a string if provided.')
    no_ir = data.get("no_ir", False)
    if not isinstance(no_ir, bool):
        raise _err(source, '"no_ir" must be a boolean.')
    if ir is not None and no_ir:
        raise _err(source, 'set at most one of "ir" / "no_ir".')
    enabled = data.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise _err(source, '"enabled" must be a boolean if provided.')
    raw = data.get("raw")
    if raw is not None:
        if not isinstance(raw, dict):
            raise _err(source, '"raw" must be an object if provided.')
        if "harness" in raw and not isinstance(raw["harness"], dict):
            raise _err(source, '"raw.harness" must be an object if provided.')
        if "slots" in raw and not (
            isinstance(raw["slots"], list)
            and all(isinstance(s, dict) for s in raw["slots"])
        ):
            raise _err(source, '"raw.slots" must be a list of objects if provided.')
    lane, pos = _parse_lane_pos(data, source=source)
    return BlockEntry(block=name, params=dict(params), ir=ir, no_ir=no_ir,
                       enabled=enabled, lane=lane, pos=pos, raw=raw)


def _validate_splits(entries: list, *, source: str) -> None:
    n_split = sum(1 for e in entries if isinstance(e, SplitEntry))
    n_join = sum(1 for e in entries if isinstance(e, JoinEntry))
    if n_split > 2:
        raise _err(source, f"at most 2 split regions per path (got {n_split}).")
    if n_split != n_join:
        raise _err(source, f"unbalanced split/join ({n_split} split, {n_join} join).")
    # each split must precede a join in list order
    depth = 0
    for e in entries:
        if isinstance(e, SplitEntry):
            depth += 1
        elif isinstance(e, JoinEntry):
            depth -= 1
            if depth < 0:
                raise _err(source, "join without a matching open split.")
    if depth != 0:
        raise _err(source, "split without a matching join.")
