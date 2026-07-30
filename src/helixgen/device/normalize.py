"""Closed-loop loudness normalization — planning + `.hsp` trim application.

Phase 2 of the loudness-feedback spec
(``docs/superpowers/specs/2026-07-14-loudness-feedback-normalization.md``,
backlog #62): compare per-target TOTAL loudness against a target and write dB
trims into the LOCAL ``.hsp`` — the source of truth; the device copy is
rebuilt from it by ``device sync`` / ``device install``.

**The measured gain IS the total.** The meter taps sit DOWNSTREAM of the
output block's gain, so `device measure`'s median chain ``gain_db`` ALREADY
includes any output trim in force. Measured on live hardware (Stadium XL fw
1.3.2, 2026-07-30) — same preset, same stimulus, only the output gain moved::

    output gain   0 dB  ->  output_db -22.82,  gain_db   8.37
    output gain -20 dB  ->  output_db -42.86,  gain_db -11.11

A −20 dB write moved the meter −20.04 dB. So the loop equalizes ``gain_db``
directly (:func:`total_loudness`): ``trim = target − gain_target``.

This corrects a prior premise (``docs/helix-protocol.md``: "every tap sits
upstream of the output block's gain") that was an inference presented as a
measurement. Adding the output level on top of a gain that already contained
it DOUBLE-COUNTED it, so every trim was wrong by exactly the output level and
the loop OSCILLATED between two states instead of converging: a real 34-tone
library run trimmed −21.90 dB on one pass and +21.90 dB on the next, leaving
the library exactly where it started. See hc-daz.

Idempotency now comes from the trim being sized against an absolute target
each run: once a target measures at ``target_db``, the next run's delta is
zero and lands in the dead-band.

The actuator is the path output block's ``level`` (``b13`` ``gain``), which
is dB-native so a correction is exact in one move: per-snapshot overrides for
the snapshot scope, a whole-preset shift (base + any existing per-snapshot
array) for the setlist scope. Because the taps are downstream, a written trim
IS visible to a re-measure — re-measuring is a valid way to confirm one.

This module is pure local logic (unit-testable offline); the ``device
normalize`` CLI verb owns the device interaction (snapshot recall / preset
load + telemetry windows) and the interaction contract with the player.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from helixgen import flowparams, mutate
from helixgen.view import _snapshot_names

# The device's output-block gain range in dB — single source:
# `flowparams._OUTPUT_RANGES` (mirrors the vendored P35_OutputMatrix defs).
OUTPUT_LEVEL_MIN, OUTPUT_LEVEL_MAX = flowparams._OUTPUT_RANGES["level"]


def snapshot_targets(body: Dict[str, Any]) -> List[Tuple[int, str]]:
    """``(index, name)`` for every NAMED snapshot of a parsed ``.hsp`` body —
    trailing ``Snap N`` placeholders trimmed, matching ``view``'s notion of
    which snapshots the preset actually defines."""
    return list(enumerate(_snapshot_names(body)))


def output_paths(body: Dict[str, Any]) -> List[int]:
    """Indices of every DSP path carrying a lane-0 output endpoint (``b13``).
    Trims are applied to ALL of them — both DSPs feed the final mix, so a
    uniform move preserves the inter-path balance."""
    flow = (body.get("preset") or {}).get("flow") or []
    out: List[int] = []
    for pi, pd in enumerate(flow):
        if not isinstance(pd, dict):
            continue
        b13 = pd.get("b13")
        if isinstance(b13, dict) and b13.get("type") == "output" and b13.get("slot"):
            out.append(pi)
    return out


def _gain_wrapper(body: Dict[str, Any], pi: int) -> Optional[Dict[str, Any]]:
    slot = body["preset"]["flow"][pi]["b13"]["slot"][0]
    w = (slot.get("params") or {}).get("gain")
    return w if isinstance(w, dict) else None


def effective_output_level(
    body: Dict[str, Any], pi: int, snap_idx: Optional[int] = None
) -> float:
    """The output level (dB) currently in force on path ``pi`` — the
    per-snapshot override slot when ``snap_idx`` is given and the wrapper
    carries one, else the base value, else the device default 0.0."""
    w = _gain_wrapper(body, pi)
    if w is None:
        return 0.0
    if snap_idx is not None:
        snaps = w.get("snapshots")
        if (isinstance(snaps, list) and 0 <= snap_idx < len(snaps)
                and isinstance(snaps[snap_idx], (int, float))
                and not isinstance(snaps[snap_idx], bool)):
            return float(snaps[snap_idx])
    base = w.get("value")
    if isinstance(base, (int, float)) and not isinstance(base, bool):
        return float(base)
    return 0.0


def reference_output_level(
    body: Dict[str, Any], snap_idx: Optional[int] = None
) -> float:
    """The output level (dB) in force on the preset's FIRST output path —
    the reference the loop's total-loudness math uses (a trim moves every
    output path by the same delta, so path 0 stands in for the preset;
    0.0 when the body carries no output endpoint)."""
    paths = output_paths(body)
    return effective_output_level(body, paths[0], snap_idx) if paths else 0.0


def total_loudness(
    body: Dict[str, Any], gain_db: float, snap_idx: Optional[int] = None
) -> float:
    """What the listener hears, in dB — which is just the measured chain
    ``gain_db``.

    The meter taps sit DOWNSTREAM of the output block's gain (measured on
    Stadium XL fw 1.3.2: a −20 dB output-gain write moved the meter −20.04
    dB), so ``gain_db`` already includes the output level in force. Adding
    :func:`reference_output_level` on top of it double-counts, which made the
    loop oscillate instead of converge — see the module docstring and hc-daz.

    ``body`` and ``snap_idx`` are retained for API compatibility and for
    callers that still want the level itself via
    :func:`reference_output_level` (the trim is applied as a DELTA to that
    level, so it is still needed on the apply path)."""
    return gain_db


def compute_trim(
    measured_total_db: float, target_total_db: float, tolerance_db: float
) -> float:
    """The dB move that takes ``measured_total_db`` (a target's TOTAL
    loudness — see :func:`total_loudness`) to ``target_total_db``, rounded
    to 0.1 dB — or 0.0 when the delta is inside the tolerance band (don't
    chase meter noise; the spec's ±1 dB default)."""
    delta = target_total_db - measured_total_db
    if abs(delta) <= tolerance_db:
        return 0.0
    return round(delta, 1)


def _clamp(value: float, warnings: List[str], label: str) -> float:
    if value < OUTPUT_LEVEL_MIN or value > OUTPUT_LEVEL_MAX:
        clamped = min(OUTPUT_LEVEL_MAX, max(OUTPUT_LEVEL_MIN, value))
        warnings.append(
            f"{label}: output level {value:+.1f} dB clamped to "
            f"{clamped:+.1f} dB (device range {OUTPUT_LEVEL_MIN:g}.."
            f"{OUTPUT_LEVEL_MAX:g} dB).")
        return clamped
    return value


def apply_snapshot_trim(
    body: Dict[str, Any], snap_idx: int, trim_db: float
) -> List[str]:
    """Add ``trim_db`` to snapshot ``snap_idx``'s effective output level on
    EVERY output path, in place, as a per-snapshot override (the untouched
    slots densify to the base — see ``mutate._write_snapshot_slot``). The
    relative move preserves any per-path offsets; idempotency comes from the
    planner sizing ``trim_db`` from TOTAL loudness (:func:`total_loudness`).
    Returns clamp warnings."""
    warnings: List[str] = []
    for pi in output_paths(body):
        cur = effective_output_level(body, pi, snap_idx)
        new = _clamp(cur + trim_db, warnings, f"path {pi} snapshot {snap_idx}")
        mutate.set_flow_param(body, "output", "level", new,
                              path=pi, snapshot=snap_idx)
    return warnings


def apply_base_trim(body: Dict[str, Any], trim_db: float) -> List[str]:
    """Shift a whole preset by ``trim_db``: every output path's base level,
    plus every slot of any existing per-snapshot gain array (a uniform shift
    preserves the preset's own scene-to-scene deltas). Returns clamp
    warnings."""
    warnings: List[str] = []
    for pi in output_paths(body):
        w = _gain_wrapper(body, pi)
        snaps = list(w.get("snapshots")) if (
            isinstance(w, dict) and isinstance(w.get("snapshots"), list)) else None
        base = effective_output_level(body, pi)
        mutate.set_flow_param(
            body, "output", "level",
            _clamp(base + trim_db, warnings, f"path {pi}"), path=pi)
        if snaps is not None:
            for i, v in enumerate(snaps[:8]):
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                mutate.set_flow_param(
                    body, "output", "level",
                    _clamp(float(v) + trim_db, warnings,
                           f"path {pi} snapshot {i}"),
                    path=pi, snapshot=i)
    return warnings
