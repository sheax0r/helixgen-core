"""Snapshot-aware `set_param` (loudness phase 2 prerequisite, backlog #62).

Pins the `.hsp` representation contract for per-snapshot PARAM overrides:
a param wrapper's 8-slot `snapshots` array is DENSE (null slots densify to
the base value — matching `generate._wrap_value_with_snapshots`), and the
wrapper's `value` mirrors the active snapshot (`preset.params.activesnapshot`)
so the block shows its active-snapshot state on load.
"""
from __future__ import annotations

import pytest

from helixgen import mutate, view
from helixgen.hsp import read_hsp
from tests.golden import harness


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("mutate-snap-test-library")
    return harness.build_corpus_library(root)


@pytest.fixture
def snapshots_body():
    return read_hsp(harness.CORPUS_DIR / "snapshots.hsp")


def _drive(body):
    return body["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]


def _bass(body):
    return body["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Bass"]


def _out_gain(body, path=0):
    return body["preset"]["flow"][path]["b13"]["slot"][0]["params"]["gain"]


# --- user-block params -------------------------------------------------------

def test_set_param_snapshot_by_name_updates_one_slot(snapshots_body, library):
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.6,
                     library, snapshot="Lead")
    w = _drive(snapshots_body)
    # only the Lead slot (index 1) changed; the rest of the dense array is kept
    assert w["snapshots"] == [0.5, 0.6, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5]
    # value mirrors the active snapshot (0 -> base 0.5), not the edited slot
    assert w["value"] == 0.5


def test_set_param_snapshot_by_index(snapshots_body, library):
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.9,
                     library, snapshot=2)
    assert _drive(snapshots_body)["snapshots"][2] == 0.9


def test_set_param_snapshot_digit_string_falls_back_to_index(
        snapshots_body, library):
    # the CLI passes strings; "2" matches no snapshot NAME -> index 2
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.9,
                     library, snapshot="2")
    assert _drive(snapshots_body)["snapshots"][2] == 0.9


def test_set_param_snapshot_name_wins_over_digit_fallback(
        snapshots_body, library):
    # a snapshot literally named "2" is matched by name, not read as index 2
    snapshots_body["preset"]["snapshots"][0]["name"] = "2"
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.9,
                     library, snapshot="2")
    assert _drive(snapshots_body)["snapshots"][0] == 0.9
    assert _drive(snapshots_body)["snapshots"][2] == 0.3


def test_set_param_snapshot_densifies_missing_array_to_base(
        snapshots_body, library):
    # Bass has no snapshots array yet; base 0.5 fills every untouched slot
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Bass", 0.8,
                     library, snapshot="Lead")
    w = _bass(snapshots_body)
    assert w["snapshots"] == [0.5, 0.8, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    assert w["value"] == 0.5  # active snapshot 0 keeps the base


def test_set_param_snapshot_resyncs_value_when_editing_active_snapshot(
        snapshots_body, library):
    snapshots_body["preset"].setdefault("params", {})["activesnapshot"] = 1
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.7,
                     library, snapshot="Lead")
    w = _drive(snapshots_body)
    assert w["snapshots"][1] == 0.7
    assert w["value"] == 0.7  # Lead IS the active snapshot


def test_set_param_snapshot_unknown_name_lists_known(snapshots_body, library):
    with pytest.raises(mutate.MutateError) as exc:
        mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.6,
                         library, snapshot="Nope")
    assert "Rhythm" in str(exc.value)


def test_set_param_snapshot_index_out_of_range(snapshots_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.6,
                         library, snapshot=8)


def test_set_param_snapshot_validates_param_name(snapshots_body, library):
    from helixgen.generate import ParamValidationError
    with pytest.raises(ParamValidationError):
        mutate.set_param(snapshots_body, "Brit 2204 Custom", "Dirve", 0.6,
                         library, snapshot="Lead")


def test_set_param_snapshot_requires_existing_value(snapshots_body, library):
    # a param with no wrapper yet has no base to densify against -> error
    del snapshots_body["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Bass"]
    with pytest.raises(mutate.MutateError) as exc:
        mutate.set_param(snapshots_body, "Brit 2204 Custom", "Bass", 0.8,
                         library, snapshot="Lead")
    assert "no existing value" in str(exc.value)


def test_set_param_snapshot_rejects_stereo_params(snapshots_body, library):
    params = snapshots_body["preset"]["flow"][0]["b02"]["slot"][0]["params"]
    params["Bass"] = {"1": {"value": 0.5}, "2": {"value": 0.5}}
    with pytest.raises(mutate.MutateError) as exc:
        mutate.set_param(snapshots_body, "Brit 2204 Custom", "Bass", 0.8,
                         library, snapshot="Lead")
    assert "stereo" in str(exc.value)


def test_set_param_snapshot_keeps_controller(snapshots_body, library):
    w = _drive(snapshots_body)
    w["controller"] = {"type": "param", "source": 0x02000000}
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.6,
                     library, snapshot="Lead")
    assert w["controller"] == {"type": "param", "source": 0x02000000}
    assert w["snapshots"][1] == 0.6


def test_set_param_without_snapshot_is_unchanged_behavior(
        snapshots_body, library):
    # base edits keep their existing semantics: value only, array untouched
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Drive", 0.42, library)
    w = _drive(snapshots_body)
    assert w["value"] == 0.42
    assert w["snapshots"] == [0.5, 0.85, 0.3, 0.5, 0.5, 0.5, 0.5, 0.5]


# --- output pseudo-block -----------------------------------------------------

def test_set_param_output_level_snapshot(snapshots_body, library):
    mutate.set_param(snapshots_body, "output", "level", -3.0, library,
                     snapshot="Lead")
    w = _out_gain(snapshots_body)
    assert w["snapshots"] == [0.0, -3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert w["value"] == 0.0  # active snapshot 0 keeps the base


def test_set_flow_param_output_snapshot_creates_missing_wrapper(
        snapshots_body, library):
    # a chassis-fresh b13 may carry no gain wrapper at all; base = 0.0 dB
    del snapshots_body["preset"]["flow"][0]["b13"]["slot"][0]["params"]["gain"]
    mutate.set_flow_param(snapshots_body, "output", "level", -6.0, snapshot=1)
    w = _out_gain(snapshots_body)
    assert w["snapshots"] == [0.0, -6.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_set_flow_param_output_snapshot_validates_range(
        snapshots_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_flow_param(snapshots_body, "output", "level", -300.0,
                              snapshot=1)


def test_set_flow_param_snapshot_rejected_on_other_pseudo_blocks(
        snapshots_body, library):
    for kind, param, value in (("input", "trim", 1.0),
                               ("split", "BalanceA", 0.4),
                               ("join", "Level", 0.0)):
        with pytest.raises(mutate.MutateError) as exc:
            mutate.set_flow_param(snapshots_body, kind, param, value,
                                  snapshot=1)
        assert "output" in str(exc.value)


# --- patch op ----------------------------------------------------------------

def test_patch_op_set_param_accepts_snapshot(snapshots_body, library):
    mutate.apply_operations(snapshots_body, [
        {"op": "set_param", "block": "Brit 2204 Custom", "param": "Drive",
         "value": 0.66, "snapshot": "Lead"},
    ], library)
    assert _drive(snapshots_body)["snapshots"][1] == 0.66


# --- round-trip through view -------------------------------------------------

def test_snapshot_param_edit_round_trips_through_view(snapshots_body, library):
    mutate.set_param(snapshots_body, "Brit 2204 Custom", "Bass", 0.7,
                     library, snapshot="Lead")
    projection = view.view(snapshots_body, library)
    lead = projection["snapshots"][1]
    assert lead["name"] == "Lead"
    assert lead["params"]["Brit 2204 Custom"]["Bass"] == 0.7
    # the pre-existing Drive override is still there
    assert lead["params"]["Brit 2204 Custom"]["Drive"] == 0.85
    # non-edited snapshots gained no phantom Bass override
    assert "Bass" not in (projection["snapshots"][2].get("params") or {}).get(
        "Brit 2204 Custom", {})
