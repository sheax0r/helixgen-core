"""Unit tests for `helixgen.device.normalize` — the pure planning/apply layer
of the phase-2 loudness closed loop (backlog #62). No device, no network."""
from __future__ import annotations

import pytest

from helixgen.device import normalize as NZ
from helixgen.hsp import read_hsp
from tests.golden import harness


@pytest.fixture
def snapshots_body():
    return read_hsp(harness.CORPUS_DIR / "snapshots.hsp")


def _gain(body, path=0):
    return body["preset"]["flow"][path]["b13"]["slot"][0]["params"]["gain"]


# --- planning ---------------------------------------------------------------

def test_snapshot_targets_trims_placeholders(snapshots_body):
    assert NZ.snapshot_targets(snapshots_body) == [
        (0, "Rhythm"), (1, "Lead"), (2, "Clean")]


def test_snapshot_targets_empty_without_named_snapshots(snapshots_body):
    for i, s in enumerate(snapshots_body["preset"]["snapshots"]):
        s["name"] = f"Snap {i + 1}"
    assert NZ.snapshot_targets(snapshots_body) == []


def test_compute_trim_rounds_and_respects_tolerance():
    assert NZ.compute_trim(33.98, 27.96, 1.0) == -6.0
    assert NZ.compute_trim(27.96, 33.98, 1.0) == 6.0
    assert NZ.compute_trim(28.3, 27.96, 1.0) == 0.0     # in band
    assert NZ.compute_trim(27.96, 27.96, 1.0) == 0.0


def test_effective_output_level_reads_snapshot_then_base(snapshots_body):
    assert NZ.effective_output_level(snapshots_body, 0) == 0.0
    _gain(snapshots_body)["snapshots"] = [0.0, -3.0] + [0.0] * 6
    assert NZ.effective_output_level(snapshots_body, 0, snap_idx=1) == -3.0
    assert NZ.effective_output_level(snapshots_body, 0, snap_idx=2) == 0.0


def test_total_loudness_adds_reference_output_level(snapshots_body):
    # total loudness = measured chain gain + the output level in force on
    # the FIRST output path (the meters tap upstream of output gain, so the
    # measured gain never includes the trim)
    assert NZ.total_loudness(snapshots_body, 30.0) == 30.0
    _gain(snapshots_body)["snapshots"] = [0.0, -3.0] + [0.0] * 6
    assert NZ.total_loudness(snapshots_body, 30.0, snap_idx=1) == 27.0
    assert NZ.total_loudness(snapshots_body, 30.0, snap_idx=2) == 30.0


# --- applying ---------------------------------------------------------------

def test_apply_snapshot_trim_writes_per_snapshot_override(snapshots_body):
    warnings = NZ.apply_snapshot_trim(snapshots_body, 1, -6.0)
    assert warnings == []
    w = _gain(snapshots_body)
    assert w["snapshots"][1] == -6.0
    assert w["snapshots"][0] == 0.0 and w["value"] == 0.0


def _plan_and_apply(body, gain_anchor, gain_target, tolerance=1.0):
    """One planning+apply pass the way the CLI runs it: trims are sized from
    TOTAL loudness (chain gain + effective output level), then applied as a
    relative move on every output path."""
    target = NZ.total_loudness(body, gain_anchor, snap_idx=0)
    trim = NZ.compute_trim(
        NZ.total_loudness(body, gain_target, snap_idx=1), target, tolerance)
    if trim:
        NZ.apply_snapshot_trim(body, 1, trim)
    return trim


def test_trim_loop_is_idempotent_on_second_run(snapshots_body):
    # C1 (2026-07-16 review): the meters tap upstream of output gain, so a
    # re-run sees the SAME measured gains — sizing trims from total loudness
    # makes the second pass a dead-band no-op instead of doubling the trim.
    assert _plan_and_apply(snapshots_body, 27.96, 33.98) == -6.0
    assert _gain(snapshots_body)["snapshots"][1] == -6.0
    assert _plan_and_apply(snapshots_body, 27.96, 33.98) == 0.0
    assert _gain(snapshots_body)["snapshots"][1] == -6.0  # NOT -12


def test_trim_loop_leaves_pre_balanced_overrides_alone(snapshots_body):
    # C1: a hand-balanced override that already equalizes total loudness
    # (Lead measures +6 dB hotter, its output is already -6 dB) is in band.
    _gain(snapshots_body)["snapshots"] = [0.0, -6.0] + [0.0] * 6
    assert _plan_and_apply(snapshots_body, 27.96, 33.98) == 0.0
    assert _gain(snapshots_body)["snapshots"][1] == -6.0


def test_apply_snapshot_trim_applies_to_every_output_path(snapshots_body):
    # the corpus chassis carries a second (empty) DSP path with its own b13
    assert NZ.output_paths(snapshots_body) == [0, 1]
    NZ.apply_snapshot_trim(snapshots_body, 1, -2.0)
    assert _gain(snapshots_body, path=1)["snapshots"][1] == -2.0


def test_apply_snapshot_trim_clamps_with_warning(snapshots_body):
    warnings = NZ.apply_snapshot_trim(snapshots_body, 1, 25.0)
    assert any("clamped" in w for w in warnings)
    assert _gain(snapshots_body)["snapshots"][1] == 20.0


def test_apply_base_trim_shifts_base_and_existing_array(snapshots_body):
    _gain(snapshots_body)["snapshots"] = [0.0, -3.0] + [0.0] * 6
    warnings = NZ.apply_base_trim(snapshots_body, -6.0)
    assert warnings == []
    w = _gain(snapshots_body)
    assert w["value"] == -6.0
    assert w["snapshots"] == [-6.0, -9.0, -6.0, -6.0, -6.0, -6.0, -6.0, -6.0]


def test_apply_base_trim_without_array_touches_base_only(snapshots_body):
    NZ.apply_base_trim(snapshots_body, -4.5)
    w = _gain(snapshots_body)
    assert w["value"] == -4.5
    assert "snapshots" not in w
