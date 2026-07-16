"""Closed-loop loudness normalization — planning + `.hsp` trim application.

Phase 2 of the loudness-feedback spec
(``docs/superpowers/specs/2026-07-14-loudness-feedback-normalization.md``,
backlog #62): compare per-target `device measure` results (the input-invariant
median chain ``gain_db``) against a target, compute dB trims, and write them
into the LOCAL ``.hsp`` — the source of truth; the device copy is rebuilt from
it by ``device sync`` / ``device install``.

The actuator is the path output block's ``level`` (``b13`` ``gain``), which is
dB-native so a correction is exact in one move: per-snapshot overrides for the
snapshot scope, a whole-preset shift (base + any existing per-snapshot array)
for the setlist scope. **Phase-0 hardware caveat:** every meter-grid tap sits
UPSTREAM of the output block's gain, so an output trim can never be confirmed
by re-measuring — the loop trusts the dB math by design (a naive
re-measure-to-confirm would falsely read "no change").

This module is pure local logic (unit-testable offline); the ``device
normalize`` CLI verb owns the device interaction (snapshot recall / preset
load + telemetry windows) and the interaction contract with the player.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from helixgen import mutate
from helixgen.view import _snapshot_names

# The device's output-block gain range in dB (`flowparams._OUTPUT_RANGES` /
# the vendored P35_OutputMatrix defs).
OUTPUT_LEVEL_MIN = -120.0
OUTPUT_LEVEL_MAX = 20.0


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


def compute_trim(
    measured_gain_db: float, target_gain_db: float, tolerance_db: float
) -> float:
    """The dB move that takes ``measured_gain_db`` to ``target_gain_db``,
    rounded to 0.1 dB — or 0.0 when the delta is inside the tolerance band
    (don't chase meter noise; the spec's ±1 dB default)."""
    delta = target_gain_db - measured_gain_db
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
    slots densify to the base — see ``mutate._write_snapshot_slot``).
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
