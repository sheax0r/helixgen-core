"""Spec: parse + validate the JSON tone description that `generate` consumes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from helixgen import flowparams


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
    trails: bool | None = None


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
class InputSpec:
    """Object form of a path's ``input`` field (signal-flow param depth).

    Scalar fields may instead carry a per-channel ``{"1": x, "2": y}`` dict
    when the effective source is ``"both"`` (the stereo input model).
    ``gate_*`` fields come from the nested recipe ``gate`` object (or its
    bool shorthand). ``impedance`` is a string, or an ``{"inst1", "inst2"}``
    dict when the two jacks differ.
    """
    source: str | None = None
    impedance: Any = None
    pad: Any = None
    trim: Any = None
    gate_enabled: Any = None
    gate_threshold: Any = None
    gate_decay: Any = None
    link: bool | None = None


@dataclass
class OutputSpec:
    """Object form of a path's ``output`` field: primary (lane-0) output
    endpoint level (dB) and pan (0..1). Destination routing (the endpoint
    model) is deliberately not modeled — it round-trips verbatim via
    structural entries."""
    level: float | None = None
    pan: float | None = None


@dataclass
class PathEntry:
    blocks: list
    input: "str | InputSpec | None" = None
    output: OutputSpec | None = None


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
    """A single footswitch assignment.

    `switch` is a logical name (e.g. "FS3"); the chassis-specific source
    ID is resolved at generate time.  Optional `path`/`lane`/`pos` disambiguate
    when multiple placed blocks share the same display_name.

    Without `param` the switch toggles the block's **bypass**. With `param`
    (plus required numeric `min`/`max`, in raw param units) the switch toggles
    that param between the two values instead — the device's "assign a switch
    to a knob" behavior, corpus-real (77 instances across the 211 exports).
    Several assignments may share one `switch` (a merge switch). `label` /
    `color` set the switch's scribble strip; `curve` / `threshold` tune the
    controller response.
    """
    switch: str
    block: str
    behavior: str = "latching"
    path: int | None = None
    lane: int | None = None
    pos: int | None = None
    param: str | None = None
    min: float | None = None
    max: float | None = None
    curve: str | None = None
    threshold: float | None = None
    label: str | None = None
    color: str | None = None


@dataclass
class ExpressionTarget:
    block: str
    param: str
    min: float = 0.0
    max: float = 1.0
    path: int | None = None
    lane: int | None = None
    pos: int | None = None
    curve: str | None = None
    threshold: float | None = None


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
        paths.append(_parse_path(path_raw, source=f"{source} paths[{i}]",
                                 path_index=i))

    snapshots = _parse_snapshots(data.get("snapshots"), source=source)
    footswitches = _parse_footswitches(data.get("footswitches"), source=source)
    expression = _parse_expression(data.get("expression"), source=source)
    # A (block, param) may be driven by ONE controller: reject a param that
    # is both a footswitch toggle target and an expression sweep target.
    # Coordinate wildcards (None) alias explicit coordinates — see
    # _refs_may_alias — so `{"block": "X", "param": "P"}` collides with
    # `{"block": "X", "param": "P", "path": 0}`.
    fs_params = [(f.block, f.param, f.path, f.lane, f.pos)
                 for f in footswitches if f.param is not None]
    for a in expression:
        for t in a.targets:
            key = (t.block, t.param, t.path, t.lane, t.pos)
            if any(_refs_may_alias(key, fp) for fp in fs_params):
                raise _err(
                    source,
                    f"param {t.param!r} on block {t.block!r} is assigned to both "
                    f"a footswitch and pedal {a.pedal}; one controller per param.",
                )
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


def _refs_may_alias(a: tuple, b: tuple) -> bool:
    """True when two (block, param, path, lane, pos) references can resolve to
    the same placed target. block/param compare exactly; a coordinate that is
    None is a WILDCARD (an entry without coordinates targets the unique block
    of that name — the same block an explicitly-coordinated entry may name).
    Treating None as "different" would let a duplicate slip through as
    `{"block": "X"}` + `{"block": "X", "path": 0}`."""
    if a[0] != b[0] or a[1] != b[1]:
        return False
    return all(x is None or y is None or x == y for x, y in zip(a[2:], b[2:]))


def _parse_footswitches(raw: Any, *, source: str) -> list[FootswitchAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"footswitches" must be a list.')
    out: list[FootswitchAssignment] = []
    # One switch may drive many targets (a merge switch — corpus-real; dozens
    # of the 211 exports carry one); the duplicate guard therefore keys on the
    # TARGET (block + param + coordinates), not the switch.
    seen_targets: list[tuple] = []
    # label/color are per-SWITCH (one scribble strip); conflicting values
    # across a merge switch's entries are a spec error.
    strip: dict[str, tuple] = {}
    for i, entry in enumerate(raw):
        fs = _parse_footswitch(entry, source=f"{source} footswitches[{i}]")
        target_key = (fs.block, fs.param, fs.path, fs.lane, fs.pos)
        if any(_refs_may_alias(target_key, seen) for seen in seen_targets):
            what = f"param {fs.param!r} on block {fs.block!r}" if fs.param else f"block {fs.block!r}"
            raise _err(
                f"{source} footswitches[{i}]",
                f"duplicate footswitch target ({what}); "
                f"each block/param may be assigned once.",
            )
        seen_targets.append(target_key)
        if fs.label is not None or fs.color is not None:
            prev = strip.get(fs.switch)
            if prev is not None and prev != (fs.label, fs.color):
                raise _err(
                    f"{source} footswitches[{i}]",
                    f"conflicting label/color for switch {fs.switch!r}; a merge "
                    f"switch has ONE scribble strip — set label/color on one "
                    f"entry (or identically on all).",
                )
            strip[fs.switch] = (fs.label, fs.color)
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

    def _num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    param = data.get("param")
    mn, mx = data.get("min"), data.get("max")
    if param is not None:
        if not isinstance(param, str) or not param:
            raise _err(source, '"param" must be a non-empty string if provided.')
        if not _num(mn) or not _num(mx):
            raise _err(
                source,
                '"param" footswitch entries require numeric "min" and "max" '
                '(the two raw param values the switch toggles between).',
            )
        # Deliberately NOT coerced to float: the corpus-real use of FS param
        # toggles includes INT params (Interval 2→4, Transport 0→1), and the
        # device encodes their min/max as ints — float-coercing here breaks
        # exact round-trip and flips the device blob's msgpack type.
    elif mn is not None or mx is not None:
        raise _err(
            source,
            '"min"/"max" apply only to "param" footswitch entries; a bypass '
            'assignment toggles the block on/off.',
        )

    curve = data.get("curve")
    if curve is not None:
        from helixgen.controllers import CURVES
        if curve not in CURVES:
            raise _err(source, f'"curve" must be one of {list(CURVES)} (got {curve!r}).')

    threshold = data.get("threshold")
    if threshold is not None:
        if not _num(threshold):
            raise _err(source, '"threshold" must be a number if provided.')
        threshold = float(threshold)

    label = data.get("label")
    if label is not None and not isinstance(label, str):
        raise _err(source, '"label" must be a string if provided.')

    color = data.get("color")
    if color is not None:
        from helixgen.controllers import FS_COLORS
        if color not in FS_COLORS:
            raise _err(
                source,
                f'"color" must be one of {sorted(FS_COLORS)} (got {color!r}).',
            )

    return FootswitchAssignment(switch=switch, block=block, behavior=behavior,
                                path=path, lane=lane, pos=pos,
                                param=param, min=mn, max=mx,
                                curve=curve, threshold=threshold,
                                label=label, color=color)


def _parse_expression(raw: Any, *, source: str) -> list[ExpressionAssignment]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _err(source, '"expression" must be a list.')
    out: list[ExpressionAssignment] = []
    seen_pedals: set[str] = set()
    seen_targets: list[tuple] = []
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
            # positions can each carry an EXP target on the same param name.
            # None coordinates are WILDCARDS (see _refs_may_alias): a bare
            # reference and an explicitly-coordinated reference to the same
            # unique block are the same target — comparing them unequal would
            # let two pedals silently last-wins on one param.
            key = (t.block, t.param, t.path, t.lane, t.pos)
            if any(_refs_may_alias(key, seen) for seen in seen_targets):
                raise _err(
                    f"{source} expression[{i}] targets[{j}]",
                    f"duplicate (block, param, pos) {(t.block, t.param, t.pos)!r}; "
                    f"one param per pedal per block-coordinate across the spec.",
                )
            seen_targets.append(key)
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
    curve = data.get("curve")
    if curve is not None:
        from helixgen.controllers import CURVES
        if curve not in CURVES:
            raise _err(source, f'"curve" must be one of {list(CURVES)} (got {curve!r}).')
    threshold = data.get("threshold")
    if threshold is not None:
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            raise _err(source, '"threshold" must be a number if provided.')
        threshold = float(threshold)
    return ExpressionTarget(block=block, param=param, min=float(mn), max=float(mx),
                            path=path, lane=lane, pos=pos, curve=curve,
                            threshold=threshold)


def _parse_lane_pos(data: dict, *, source: str) -> tuple[int, int | None]:
    lane = data.get("lane", 0)
    if lane not in (0, 1):
        raise _err(source, f'"lane" must be 0 or 1 (got {lane!r}).')
    pos = data.get("pos")
    if pos is not None and (not isinstance(pos, int) or isinstance(pos, bool) or pos < 0):
        raise _err(source, f'"pos" must be a non-negative integer if provided (got {pos!r}).')
    return lane, pos


def _parse_path(data: Any, *, source: str, path_index: int = 0) -> PathEntry:
    if not isinstance(data, dict):
        raise _err(source, "must be an object.")

    inp = _parse_input(data.get("input"), source=source, path_index=path_index)
    out = _parse_output(data.get("output"), source=source)

    blocks_raw = data.get("blocks")
    if not isinstance(blocks_raw, list):
        raise _err(source, '"blocks" must be an array.')

    blocks = [_parse_path_entry(b, source=f"{source} blocks[{i}]")
              for i, b in enumerate(blocks_raw)]
    _validate_splits(blocks, source=source)
    return PathEntry(blocks=blocks, input=inp, output=out)


def _check_source_mode(mode: Any, *, source: str) -> None:
    if mode not in VALID_INPUT_MODES:
        raise _err(
            source,
            f'input "source" must be one of {list(VALID_INPUT_MODES)} '
            f'(got {mode!r}).',
        )


def _parse_channel_value(field: str, value: Any, *, stereo: bool,
                         source: str) -> Any:
    """Validate a scalar-or-per-channel input field value.

    A ``{"1": x, "2": y}`` dict is legal only when the effective source is
    ``"both"`` (the stereo input model); each channel value is validated
    individually. Scalars are validated directly.
    """
    if isinstance(value, dict):
        if not stereo:
            raise _err(source, f'per-channel "{field}" values require '
                               f'source "both" (the stereo input).')
        if set(value.keys()) != {"1", "2"}:
            raise _err(source, f'per-channel "{field}" must have exactly '
                               f'the keys "1" and "2".')
        for ch, v in value.items():
            try:
                flowparams.validate_input_field(field, v)
            except ValueError as e:
                raise _err(source, f'input channel {ch}: {e}') from e
        return {"1": value["1"], "2": value["2"]}
    try:
        flowparams.validate_input_field(field, value)
    except ValueError as e:
        raise _err(source, f"input: {e}") from e
    return value


_INPUT_OBJECT_KEYS = ("source", "impedance", "pad", "trim", "gate", "link")


def _parse_input(raw: Any, *, source: str, path_index: int) -> "str | InputSpec | None":
    """Parse a path's ``input`` field: the classic mode string, or the
    signal-flow object form (source + impedance/pad/trim/gate/link)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        _check_source_mode(raw, source=source)
        return raw
    if not isinstance(raw, dict):
        raise _err(source, '"input" must be a mode string or an object '
                           '(e.g. {"source": "inst1", "gate": true}).')

    unknown = sorted(set(raw) - set(_INPUT_OBJECT_KEYS))
    if unknown:
        raise _err(source, f'unknown input key(s) {unknown}; valid keys: '
                           f'{list(_INPUT_OBJECT_KEYS)}.')

    mode = raw.get("source")
    if mode is not None:
        _check_source_mode(mode, source=source)
    effective = mode or flowparams.default_input_mode(path_index)
    stereo = effective == "both"
    jacks = flowparams.jacks_for_mode(effective)

    spec = InputSpec(source=mode)

    imp = raw.get("impedance")
    if imp is not None:
        if not jacks:
            raise _err(source, f'input "impedance" is meaningless with '
                               f'source "{effective}" (no instrument jack in use).')
        if isinstance(imp, dict):
            bad = sorted(set(imp) - set(jacks))
            if bad:
                raise _err(source, f'impedance jack(s) {bad} not used by '
                                   f'source "{effective}"; valid jacks: {list(jacks)}.')
            for jack, v in imp.items():
                try:
                    flowparams.validate_impedance(v)
                except ValueError as e:
                    raise _err(source, f"impedance.{jack}: {e}") from e
            spec.impedance = dict(imp)
        else:
            try:
                flowparams.validate_impedance(imp)
            except ValueError as e:
                raise _err(source, f"impedance: {e}") from e
            spec.impedance = imp

    if raw.get("pad") is not None:
        if effective == "none":
            raise _err(source, 'input "pad" requires an instrument source '
                               '(inst1/inst2/both), not "none".')
        spec.pad = _parse_channel_value("pad", raw["pad"], stereo=stereo,
                                        source=source)
    if raw.get("trim") is not None:
        spec.trim = _parse_channel_value("trim", raw["trim"], stereo=stereo,
                                         source=source)

    gate = raw.get("gate")
    if gate is not None:
        if isinstance(gate, bool):
            spec.gate_enabled = gate
        elif isinstance(gate, dict):
            unknown = sorted(set(gate) - {"enabled", "threshold", "decay"})
            if unknown:
                raise _err(source, f'unknown gate key(s) {unknown}; valid '
                                   f'keys: [\'enabled\', \'threshold\', \'decay\'].')
            spec.gate_enabled = _parse_channel_value(
                "gate", gate.get("enabled", True), stereo=stereo, source=source)
            if gate.get("threshold") is not None:
                spec.gate_threshold = _parse_channel_value(
                    "threshold", gate["threshold"], stereo=stereo, source=source)
            if gate.get("decay") is not None:
                spec.gate_decay = _parse_channel_value(
                    "decay", gate["decay"], stereo=stereo, source=source)
        else:
            raise _err(source, '"gate" must be a boolean or an object '
                               '{"enabled", "threshold", "decay"}.')

    link = raw.get("link")
    if link is not None:
        if not stereo:
            raise _err(source, 'input "link" (StereoLink) requires source '
                               '"both" (the stereo input).')
        if not isinstance(link, bool):
            raise _err(source, '"link" must be a boolean.')
        spec.link = link

    return spec


def _parse_output(raw: Any, *, source: str) -> OutputSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _err(source, '"output" must be an object like '
                           '{"level": -3.0, "pan": 0.5} (destination routing '
                           'is carried verbatim via structural entries, not '
                           'authored here).')
    unknown = sorted(set(raw) - {"level", "pan"})
    if unknown:
        raise _err(source, f'unknown output key(s) {unknown}; valid keys: '
                           f"['level', 'pan'].")
    out = OutputSpec()
    for fieldname in ("level", "pan"):
        v = raw.get(fieldname)
        if v is None:
            continue
        try:
            flowparams.validate_output_field(fieldname, v)
        except ValueError as e:
            raise _err(source, f"output: {e}") from e
        setattr(out, fieldname, float(v))
    return out


def _resolve_split_model(sd: dict, *, source: str) -> str:
    """Resolve a split entry's model from its friendly ``type`` and/or raw
    ``model``. One of the two is required; when both are given they must
    agree."""
    typ = sd.get("type")
    model = sd.get("model")
    if typ is not None:
        if typ not in flowparams.SPLIT_TYPES:
            raise _err(source, f'unknown split type {typ!r}; valid types: '
                               f'{sorted(flowparams.SPLIT_TYPES)}.')
        resolved = flowparams.SPLIT_TYPES[typ]
        if model is not None and model != resolved:
            raise _err(source, f'split "type" {typ!r} and "model" {model!r} '
                               f'do not agree ({typ!r} is {resolved}).')
        return resolved
    if not isinstance(model, str):
        raise _err(source, '"split" requires a "type" '
                           f'({sorted(flowparams.SPLIT_TYPES)}) or a '
                           '"model" string.')
    return model


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
        if not isinstance(sd, dict):
            raise _err(source, '"split" must be an object with a "type" '
                               '(y/ab/crossover/dynamic) or "model" string.')
        model = _resolve_split_model(sd, source=source)
        params_raw = sd.get("params", {})
        if not isinstance(params_raw, dict):
            raise _err(source, '"split.params" must be an object.')
        params = dict(params_raw)
        try:
            flowparams.validate_wire_params(model, params)
        except ValueError as e:
            raise _err(source, f"split: {e}") from e
        params = flowparams.coerce_wire_params(model, params)
        lane, pos = _parse_lane_pos(data, source=source)
        return SplitEntry(model=model, params=params, lane=lane, pos=pos)
    if "join" in data:
        jd = data["join"] or {}
        if not isinstance(jd, dict):
            raise _err(source, '"join" must be an object if provided.')
        model = jd.get("model", "P35_AppDSPJoin")
        params_raw = jd.get("params", {})
        if not isinstance(params_raw, dict):
            raise _err(source, '"join.params" must be an object.')
        params = dict(params_raw)
        try:
            flowparams.validate_wire_params(model, params)
        except ValueError as e:
            raise _err(source, f"join: {e}") from e
        params = flowparams.coerce_wire_params(model, params)
        lane, pos = _parse_lane_pos(data, source=source)
        return JoinEntry(model=model, params=params, lane=lane, pos=pos)
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
    trails = data.get("trails")
    if trails is not None and not isinstance(trails, bool):
        raise _err(source, '"trails" must be a boolean if provided.')
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
                       enabled=enabled, lane=lane, pos=pos, raw=raw,
                       trails=trails)


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
