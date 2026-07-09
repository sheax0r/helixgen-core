"""Tests for helixgen.mutate — the .hsp-canonical body-mutation verbs.

These operate directly on a parsed `.hsp` body dict (`preset.flow[*].bNN`),
not on a spec.json. See docs/superpowers/plans/2026-07-08-hsp-canonical-redesign.md
Tasks 1b/1c/1d/1e.
"""
from __future__ import annotations

import copy

import pytest

from helixgen import mutate
from helixgen.controllers import CONTROLLER_SOURCE_IDS
from helixgen.generate import ParamValidationError
from helixgen.hsp import read_hsp, write_hsp
from helixgen.ir import IrMapping
from tests.golden import harness


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("mutate-test-library")
    return harness.build_corpus_library(root)


@pytest.fixture
def goldfinger_body():
    return read_hsp(harness.CORPUS_DIR / "goldfinger.hsp")


@pytest.fixture
def expression_body():
    return read_hsp(harness.CORPUS_DIR / "expression.hsp")


@pytest.fixture
def snapshots_body():
    return read_hsp(harness.CORPUS_DIR / "snapshots.hsp")


@pytest.fixture
def split_join_body():
    return read_hsp(harness.CORPUS_DIR / "split_join.hsp")


@pytest.fixture
def dual_cab_raw_body():
    return read_hsp(harness.CORPUS_DIR / "dual_cab_raw.hsp")


# --- resolve_slot ------------------------------------------------------

def test_resolve_slot_unique_match(goldfinger_body, library):
    assert mutate.resolve_slot(goldfinger_body, "Brit 2204 Custom", library) == (0, "b02", 0)


def test_resolve_slot_finds_each_placed_block(goldfinger_body, library):
    # Sanity: every block placed by the goldfinger recipe resolves to a
    # distinct (flow_index, bnn_key, slot_index).
    assert mutate.resolve_slot(goldfinger_body, "Scream 808", library) == (0, "b01", 0)
    assert mutate.resolve_slot(goldfinger_body, "4x12 Greenback 25", library) == (0, "b03", 0)
    assert mutate.resolve_slot(goldfinger_body, "Digital", library) == (0, "b04", 0)
    assert mutate.resolve_slot(goldfinger_body, "Plate", library) == (0, "b05", 0)


def test_resolve_slot_missing_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError) as exc:
        mutate.resolve_slot(goldfinger_body, "Nope Amp", library)
    msg = str(exc.value)
    assert "Nope Amp" in msg
    # Helpful message lists the blocks that ARE placed.
    assert "Scream 808" in msg
    assert "Brit 2204 Custom" in msg


def test_resolve_slot_ambiguous_raises(library):
    body = {
        "preset": {
            "flow": [
                {
                    "b01": {
                        "type": "amp", "position": 1, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{"model": "HD2_AmpBrit2204Custom", "@enabled": {"value": True}, "params": {}}],
                    },
                    "b02": {
                        "type": "amp", "position": 2, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{"model": "HD2_AmpBrit2204Custom", "@enabled": {"value": True}, "params": {}}],
                    },
                }
            ]
        }
    }
    with pytest.raises(mutate.MutateError) as exc:
        mutate.resolve_slot(body, "Brit 2204 Custom", library)
    assert "Brit 2204 Custom" in str(exc.value)
    # Disambiguated by pos resolves cleanly.
    assert mutate.resolve_slot(body, "Brit 2204 Custom", library, pos=1) == (0, "b01", 0)
    assert mutate.resolve_slot(body, "Brit 2204 Custom", library, pos=2) == (0, "b02", 0)


def test_resolve_slot_by_model_id(goldfinger_body, library):
    assert mutate.resolve_slot(goldfinger_body, "HD2_AmpBrit2204Custom", library) == (0, "b02", 0)


def test_resolve_slot_skips_endpoints(goldfinger_body, library):
    # b00/b13 endpoints (P35_ models) never match a library block name.
    with pytest.raises(mutate.MutateError):
        mutate.resolve_slot(goldfinger_body, "P35_InputInst1_2", library)


# --- set_param -----------------------------------------------------------

def test_set_param_mono_float_updates_value_only(goldfinger_body, library):
    before = copy.deepcopy(goldfinger_body)
    mutate.set_param(goldfinger_body, "Brit 2204 Custom", "Drive", 0.75, library)

    slot = goldfinger_body["preset"]["flow"][0]["b02"]["slot"][0]
    assert slot["params"]["Drive"]["value"] == 0.75

    # Nothing else in the body changed.
    before["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]["value"] = 0.75
    assert goldfinger_body == before


def test_set_param_unknown_param_raises(goldfinger_body, library):
    with pytest.raises((mutate.MutateError, ParamValidationError)):
        mutate.set_param(goldfinger_body, "Brit 2204 Custom", "NotAParam", 1.0, library)


def test_set_param_missing_block_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_param(goldfinger_body, "Nope Amp", "Drive", 0.5, library)


def test_set_param_preserves_controller(expression_body, library):
    # "Drive" on the Brit 2204 Custom in expression.hsp carries an EXP
    # controller dict. Setting the value must not disturb it.
    slot_before = copy.deepcopy(
        expression_body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Drive"]
    )
    assert "controller" in slot_before

    mutate.set_param(expression_body, "Brit 2204 Custom", "Drive", 0.42, library)

    wrapped = expression_body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Drive"]
    assert wrapped["value"] == 0.42
    assert wrapped["controller"] == slot_before["controller"]


def test_set_param_stereo_updates_both_channels(library):
    body = {
        "preset": {
            "flow": [
                {
                    "b01": {
                        "type": "fx", "position": 1, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{
                            "model": "HD2_DlyDigital",
                            "@enabled": {"value": True},
                            "params": {
                                "Mix": {"1": {"value": 0.2}, "2": {"value": 0.2}},
                                "Time": {"value": 0.4},
                                "Feedback": {"value": 0.4},
                            },
                        }],
                    },
                }
            ]
        }
    }
    mutate.set_param(body, "Digital", "Mix", 0.55, library)
    wrapped = body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Mix"]
    assert wrapped == {"1": {"value": 0.55}, "2": {"value": 0.55}}


def test_set_param_coerces_int_to_float(library):
    body = {
        "preset": {
            "flow": [
                {
                    "b01": {
                        "type": "cab", "position": 1, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{
                            "model": "HD2_Cab4x12Greenback25",
                            "@enabled": {"value": True},
                            "params": {
                                "Distance": {"value": 3},
                                "HighCut": {"value": 8000.0},
                                "LowCut": {"value": 80.0},
                            },
                        }],
                    },
                }
            ]
        }
    }
    mutate.set_param(body, "4x12 Greenback 25", "HighCut", 6500, library)
    wrapped = body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["HighCut"]
    assert wrapped["value"] == 6500.0
    assert isinstance(wrapped["value"], float)


def test_set_param_disambiguates_with_path_lane_pos(library):
    body = {
        "preset": {
            "flow": [
                {
                    "b01": {
                        "type": "amp", "position": 1, "path": 0,
                        "@enabled": {"value": True},
                        "slot": [{"model": "HD2_AmpBrit2204Custom", "@enabled": {"value": True},
                                  "params": {"Drive": {"value": 0.5}}}],
                    },
                },
                {
                    "b01": {
                        "type": "amp", "position": 1, "path": 1,
                        "@enabled": {"value": True},
                        "slot": [{"model": "HD2_AmpBrit2204Custom", "@enabled": {"value": True},
                                  "params": {"Drive": {"value": 0.5}}}],
                    },
                },
            ]
        }
    }
    with pytest.raises(mutate.MutateError):
        mutate.set_param(body, "Brit 2204 Custom", "Drive", 0.9, library)
    mutate.set_param(body, "Brit 2204 Custom", "Drive", 0.9, library, path=1)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Drive"]["value"] == 0.5
    assert body["preset"]["flow"][1]["b01"]["slot"][0]["params"]["Drive"]["value"] == 0.9


# --- set_enabled ----------------------------------------------------------

def test_set_enabled_base_disable_flips_bnn_value(goldfinger_body, library):
    bnn = goldfinger_body["preset"]["flow"][0]["b02"]
    slot_enabled_before = copy.deepcopy(bnn["slot"][0]["@enabled"])

    mutate.set_enabled(goldfinger_body, "Brit 2204 Custom", False, library)

    assert bnn["@enabled"]["value"] is False
    # Base bypass lives at the bNN level, NOT inside slot (device-validated;
    # slot-level @enabled is inert on Stadium and must stay untouched).
    assert bnn["slot"][0]["@enabled"] == slot_enabled_before


def test_set_enabled_base_enable_flips_bnn_value(goldfinger_body, library):
    mutate.set_enabled(goldfinger_body, "Brit 2204 Custom", False, library)
    mutate.set_enabled(goldfinger_body, "Brit 2204 Custom", True, library)
    assert goldfinger_body["preset"]["flow"][0]["b02"]["@enabled"]["value"] is True


def test_set_enabled_base_edit_syncs_active_snapshot_slot(snapshots_body, library):
    # Scream 808 (b01) already carries a dense snapshots array in the golden
    # fixture ([T, T, F, T, T, T, T, T], activesnapshot=0). A base-level edit
    # (snapshot=None) must keep value == snapshots[activesnapshot] in sync --
    # otherwise the block shows its old value on load until snapshots are
    # toggled once (the exact stale-on-load bug class fixed in 0.5.1).
    bnn = snapshots_body["preset"]["flow"][0]["b01"]
    assert bnn["@enabled"]["snapshots"][0] is True

    mutate.set_enabled(snapshots_body, "Scream 808", False, library)

    wrapped = bnn["@enabled"]
    assert wrapped["value"] is False
    assert wrapped["snapshots"][0] is False
    assert wrapped["value"] == wrapped["snapshots"][0]
    # Other snapshot slots are untouched by a base edit.
    assert wrapped["snapshots"] == [False, True, False, True, True, True, True, True]


def test_set_enabled_missing_block_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_enabled(goldfinger_body, "Nope Amp", False, library)


def test_set_enabled_snapshot_densifies_nulls_and_sets_index(snapshots_body, library):
    # b02 (Brit 2204 Custom) starts with a plain @enabled (no snapshots array
    # yet) -- disabling it in "Clean" (snapshot index 2) must synthesize a
    # dense 8-element array, not a sparse one (the 0.5.1 sparse-snapshot bug).
    bnn = snapshots_body["preset"]["flow"][0]["b02"]
    assert "snapshots" not in bnn["@enabled"]
    assert bnn["@enabled"]["value"] is True

    mutate.set_enabled(snapshots_body, "Brit 2204 Custom", False, library, snapshot="Clean")

    wrapped = bnn["@enabled"]
    assert len(wrapped["snapshots"]) == 8
    assert None not in wrapped["snapshots"]
    assert wrapped["snapshots"] == [True, True, False, True, True, True, True, True]


def test_set_enabled_snapshot_preserves_existing_overrides(snapshots_body, library):
    # b01 (Scream 808) already carries a real snapshots array in the golden
    # fixture: [T, T, F, T, T, T, T, T] (disabled in "Clean"). Editing a
    # different snapshot must not clobber that pre-existing override.
    bnn = snapshots_body["preset"]["flow"][0]["b01"]
    assert bnn["@enabled"]["snapshots"][2] is False

    mutate.set_enabled(snapshots_body, "Scream 808", False, library, snapshot="Lead")

    assert bnn["@enabled"]["snapshots"] == [True, False, False, True, True, True, True, True]


def test_set_enabled_value_mirrors_active_snapshot_after_edit(snapshots_body, library):
    # activesnapshot is 0 ("Rhythm"). Editing a non-active snapshot must not
    # perturb the on-load `value`.
    mutate.set_enabled(snapshots_body, "Brit 2204 Custom", False, library, snapshot="Clean")
    wrapped = snapshots_body["preset"]["flow"][0]["b02"]["@enabled"]
    assert wrapped["value"] is True  # unchanged: mirrors snapshots[0] == True

    # Editing the ACTIVE snapshot flips `value` too -- the invariant
    # value == snapshots[activesnapshot] must hold after any snapshot edit.
    mutate.set_enabled(snapshots_body, "Brit 2204 Custom", False, library, snapshot="Rhythm")
    wrapped = snapshots_body["preset"]["flow"][0]["b02"]["@enabled"]
    assert wrapped["value"] is False
    assert wrapped["snapshots"][0] is False
    assert wrapped["value"] == wrapped["snapshots"][0]


def test_set_enabled_snapshot_accepts_int_index(goldfinger_body, library):
    mutate.set_enabled(goldfinger_body, "Brit 2204 Custom", False, library, snapshot=1)
    wrapped = goldfinger_body["preset"]["flow"][0]["b02"]["@enabled"]
    assert wrapped["snapshots"][1] is False
    assert wrapped["snapshots"][0] is True  # densified from the pre-edit base


def test_set_enabled_clamps_out_of_range_active_snapshot_index(goldfinger_body, library):
    # A malformed `activesnapshot` pointing past the (8-slot) snapshots array
    # must not IndexError -- clamp to the last valid slot instead.
    goldfinger_body["preset"]["params"]["activesnapshot"] = 99
    mutate.set_enabled(goldfinger_body, "Brit 2204 Custom", False, library, snapshot=0)
    wrapped = goldfinger_body["preset"]["flow"][0]["b02"]["@enabled"]
    assert wrapped["value"] == wrapped["snapshots"][-1]


def test_set_enabled_unknown_snapshot_name_raises(snapshots_body, library):
    with pytest.raises(mutate.MutateError) as exc:
        mutate.set_enabled(snapshots_body, "Brit 2204 Custom", False, library, snapshot="Nope")
    assert "Nope" in str(exc.value)


# --- add_block --------------------------------------------------------------

def test_add_block_appends_new_bnn_with_sequential_position(goldfinger_body, library):
    key = mutate.add_block(goldfinger_body, "With Pan", library)

    assert key == "b06"
    bnn = goldfinger_body["preset"]["flow"][0][key]
    assert bnn["slot"][0]["model"] == "HX2_ImpulseResponseWithPan"
    assert bnn["type"] == "cab"
    assert bnn["position"] == 6
    assert bnn["path"] == 0
    assert bnn["@enabled"]["value"] is True
    # Existing blocks' positions/keys are untouched by an append.
    assert goldfinger_body["preset"]["flow"][0]["b01"]["position"] == 1
    assert goldfinger_body["preset"]["flow"][0]["b05"]["position"] == 5


def test_add_block_applies_params(goldfinger_body, library):
    key = mutate.add_block(goldfinger_body, "With Pan", library, params={"Mix": 0.5})
    slot = goldfinger_body["preset"]["flow"][0][key]["slot"][0]
    assert slot["params"]["Mix"]["value"] == 0.5


def test_add_block_after_inserts_and_renumbers(goldfinger_body, library):
    key = mutate.add_block(goldfinger_body, "With Pan", library, after="Scream 808")
    flow0 = goldfinger_body["preset"]["flow"][0]

    assert key == "b02"
    assert flow0["b02"]["slot"][0]["model"] == "HX2_ImpulseResponseWithPan"
    assert flow0["b02"]["position"] == 2
    # Everything that followed shifted up by one position AND bNN key so key
    # order keeps matching chain order (decompile relies on sorted-key order).
    assert flow0["b03"]["slot"][0]["model"] == "HD2_AmpBrit2204Custom"  # was b02
    assert flow0["b03"]["position"] == 3
    assert flow0["b04"]["slot"][0]["model"] == "HD2_Cab4x12Greenback25"  # was b03
    assert flow0["b04"]["position"] == 4
    assert flow0["b05"]["slot"][0]["model"] == "HD2_DlyDigital"  # was b04
    assert flow0["b05"]["position"] == 5
    assert flow0["b06"]["slot"][0]["model"] == "HD2_RvbPlate"  # was b05
    assert flow0["b06"]["position"] == 6
    assert "b07" not in flow0


def test_add_block_unknown_model_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.add_block(goldfinger_body, "Nope Block", library)


def test_add_block_invalid_path_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.add_block(goldfinger_body, "With Pan", library, path=5)


def test_add_block_raises_when_lane_full(library):
    path_dict = {}
    for i in range(1, 13):
        path_dict[f"b{i:02d}"] = {
            "type": "fx", "position": i, "path": 0,
            "@enabled": {"value": True},
            "slot": [{"model": "HD2_DlyDigital", "@enabled": {"value": True}, "params": {}}],
        }
    body = {"preset": {"flow": [path_dict]}}
    with pytest.raises(mutate.MutateError):
        mutate.add_block(body, "Digital", library)


def test_add_block_rejects_parallel_routed_path(split_join_body, library):
    # split_join.hsp's path 0 has a split/join pair (b02/b03) whose
    # branch/endpoint cross-references point at specific bNN keys --
    # renumbering lane 0 would rewrite those keys without updating the
    # pointers, and desync lane 1's positions. Not supported yet.
    before = copy.deepcopy(split_join_body)
    with pytest.raises(mutate.MutateError, match="parallel-routed"):
        mutate.add_block(split_join_body, "With Pan", library)
    assert split_join_body == before


def test_add_block_round_trips_through_write_and_read_hsp(goldfinger_body, library, tmp_path):
    mutate.add_block(goldfinger_body, "With Pan", library, after="Scream 808")
    out = tmp_path / "roundtrip_add.hsp"
    write_hsp(out, goldfinger_body)
    reloaded = read_hsp(out)
    assert reloaded == goldfinger_body


# --- remove_block ------------------------------------------------------------

def test_remove_block_deletes_and_renumbers(goldfinger_body, library):
    mutate.remove_block(goldfinger_body, "Brit 2204 Custom", library)
    flow0 = goldfinger_body["preset"]["flow"][0]

    assert flow0["b02"]["slot"][0]["model"] == "HD2_Cab4x12Greenback25"  # was b03
    assert flow0["b02"]["position"] == 2
    assert flow0["b03"]["slot"][0]["model"] == "HD2_DlyDigital"  # was b04
    assert flow0["b03"]["position"] == 3
    assert flow0["b04"]["slot"][0]["model"] == "HD2_RvbPlate"  # was b05
    assert flow0["b04"]["position"] == 4
    assert "b05" not in flow0
    assert not any(
        isinstance(v, dict) and v.get("slot", [{}])[0].get("model") == "HD2_AmpBrit2204Custom"
        for k, v in flow0.items() if k not in ("b00", "b13")
    )


def test_remove_block_rejects_parallel_routed_path(split_join_body, library):
    before = copy.deepcopy(split_join_body)
    with pytest.raises(mutate.MutateError, match="parallel-routed"):
        mutate.remove_block(split_join_body, "Scream 808", library)
    assert split_join_body == before


def test_remove_block_missing_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.remove_block(goldfinger_body, "Nope Amp", library)


def test_remove_block_round_trips_through_write_and_read_hsp(goldfinger_body, library, tmp_path):
    mutate.remove_block(goldfinger_body, "Brit 2204 Custom", library)
    out = tmp_path / "roundtrip_remove.hsp"
    write_hsp(out, goldfinger_body)
    reloaded = read_hsp(out)
    assert reloaded == goldfinger_body


# --- swap_model (Task 1f) --------------------------------------------------

def test_swap_model_carries_shared_params_and_warns_on_dropped(goldfinger_body, library):
    # "4x12 Greenback 25" (Distance/HighCut/LowCut) -> "With Pan"
    # (HighCut/LowCut/Mix/Pan/Level/Delay/IrData/Polarity): HighCut/LowCut are
    # shared and must carry over; Distance has no home on the target and must
    # be dropped with a warning.
    warnings = mutate.swap_model(goldfinger_body, "4x12 Greenback 25", "With Pan", library)

    slot = goldfinger_body["preset"]["flow"][0]["b03"]["slot"][0]
    assert slot["model"] == "HX2_ImpulseResponseWithPan"
    assert slot["params"]["HighCut"]["value"] == 8000.0
    assert slot["params"]["LowCut"]["value"] == 80.0
    assert "Distance" not in slot["params"]
    assert any("Distance" in w and "dropped" in w for w in warnings)


def test_swap_model_no_ir_warning_when_target_is_ir_block(goldfinger_body, library):
    # The source block ("4x12 Greenback 25") carries no irhash to begin with,
    # and the target ("With Pan") IS an IR block, so there's nothing to drop
    # and no warning about it (default_irhash injection is set_ir's job, not
    # swap_model's).
    warnings = mutate.swap_model(goldfinger_body, "4x12 Greenback 25", "With Pan", library)
    slot = goldfinger_body["preset"]["flow"][0]["b03"]["slot"][0]
    assert "irhash" not in slot
    assert not any("IR" in w for w in warnings)


def test_swap_model_drops_ir_when_target_is_not_ir_block(goldfinger_body, library):
    # Give the placed "With Pan" an irhash first (as if set_ir had run).
    mutate.add_block(goldfinger_body, "With Pan", library)
    key = "b06"
    goldfinger_body["preset"]["flow"][0][key]["slot"][0]["irhash"] = "ad8182e1ebe9fd95dffde5dd54b6d89c"

    warnings = mutate.swap_model(goldfinger_body, "With Pan", "4x12 Greenback 25", library)

    slot = goldfinger_body["preset"]["flow"][0][key]["slot"][0]
    assert slot["model"] == "HD2_Cab4x12Greenback25"
    assert "irhash" not in slot
    assert any("IR" in w for w in warnings)


def test_swap_model_rejects_cross_category(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.swap_model(goldfinger_body, "Digital", "Plate", library)


def test_swap_model_preserves_controller_on_carried_param(expression_body, library):
    # "Drive" on "Brit 2204 Custom" (b01 in expression.hsp) carries an EXP
    # controller. Swapping to another amp with a "Drive" param must not lose it.
    controller_before = copy.deepcopy(
        expression_body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Drive"]["controller"]
    )
    mutate.swap_model(expression_body, "Brit 2204 Custom", "Brit 2204 Custom", library)
    wrapped = expression_body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Drive"]
    assert wrapped["controller"] == controller_before


def test_swap_model_missing_block_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.swap_model(goldfinger_body, "Nope Amp", "Digital", library)


def test_swap_model_unknown_target_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.swap_model(goldfinger_body, "Digital", "Nope Delay", library)


# --- set_ir (Task 1f) -------------------------------------------------------

@pytest.fixture
def ir_block_body():
    return read_hsp(harness.CORPUS_DIR / "ir_block.hsp")


@pytest.fixture
def irs(tmp_path):
    return IrMapping(
        irs_dir=tmp_path,
        entries={"1234567890abcdef1234567890abcdef": "West.wav"},
    )


def test_set_ir_by_basename(ir_block_body, library, irs):
    mutate.set_ir(ir_block_body, "With Pan", "West.wav", library, irs)
    slot = ir_block_body["preset"]["flow"][0]["b02"]["slot"][0]
    assert slot["irhash"] == "1234567890abcdef1234567890abcdef"


def test_set_ir_by_hash(ir_block_body, library, irs):
    mutate.set_ir(ir_block_body, "With Pan", "1234567890abcdef1234567890abcdef", library, irs)
    slot = ir_block_body["preset"]["flow"][0]["b02"]["slot"][0]
    assert slot["irhash"] == "1234567890abcdef1234567890abcdef"


def test_set_ir_unknown_basename_raises(ir_block_body, library, irs):
    with pytest.raises(mutate.MutateError):
        mutate.set_ir(ir_block_body, "With Pan", "Nope.wav", library, irs)


def test_set_ir_non_ir_block_raises(goldfinger_body, library, irs):
    with pytest.raises(mutate.MutateError):
        mutate.set_ir(goldfinger_body, "Brit 2204 Custom", "West.wav", library, irs)


# --- set_trails (Task 1f) ----------------------------------------------------

def test_set_trails_true_sets_harness_param(goldfinger_body, library):
    mutate.set_trails(goldfinger_body, "Digital", True, library)
    harness_dict = goldfinger_body["preset"]["flow"][0]["b04"]["harness"]
    assert harness_dict["params"]["Trails"]["value"] is True


def test_set_trails_false_on_reverb(goldfinger_body, library):
    mutate.set_trails(goldfinger_body, "Plate", False, library)
    harness_dict = goldfinger_body["preset"]["flow"][0]["b05"]["harness"]
    assert harness_dict["params"]["Trails"]["value"] is False


def test_set_trails_preserves_existing_harness(goldfinger_body, library):
    bnn = goldfinger_body["preset"]["flow"][0]["b04"]
    bnn["harness"] = {
        "@enabled": {"value": True},
        "dual": True,
        "params": {"EvtIdx": {"value": -1}, "Trails": {"value": False},
                   "bypass": {"value": False}, "upper": {"value": True}},
    }
    mutate.set_trails(goldfinger_body, "Digital", True, library)
    assert bnn["harness"]["dual"] is True
    assert bnn["harness"]["params"]["Trails"]["value"] is True


def test_set_trails_rejects_non_delay_reverb(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.set_trails(goldfinger_body, "Brit 2204 Custom", True, library)


# --- set_input (Task 1f) -----------------------------------------------------

def test_set_input_rewrites_endpoint_model(goldfinger_body):
    mutate.set_input(goldfinger_body, 0, "inst1")
    slot = goldfinger_body["preset"]["flow"][0]["b00"]["slot"][0]
    assert slot["model"] == "P35_InputInst1"


def test_set_input_both_to_inst_reshapes_params_to_mono(goldfinger_body):
    # path 0's b00 starts stereo (P35_InputInst1_2); give it a stereo-shaped
    # param so the mono rewrite has something to reshape.
    b00_slot = goldfinger_body["preset"]["flow"][0]["b00"]["slot"][0]
    b00_slot["params"] = {"Gain": {"1": {"value": 0.5}, "2": {"value": 0.6}},
                           "StereoLink": {"value": False}}
    mutate.set_input(goldfinger_body, 0, "inst2")
    slot = goldfinger_body["preset"]["flow"][0]["b00"]["slot"][0]
    assert slot["model"] == "P35_InputInst2"
    assert slot["params"]["Gain"] == {"value": 0.5}
    assert "StereoLink" not in slot["params"]


def test_set_input_invalid_jack_raises(goldfinger_body):
    with pytest.raises(mutate.MutateError):
        mutate.set_input(goldfinger_body, 0, "nope")


def test_set_input_invalid_path_raises(goldfinger_body):
    with pytest.raises(mutate.MutateError):
        mutate.set_input(goldfinger_body, 5, "inst1")


# --- wire_footswitch / wire_expression / wire_wah_toe (Task 1g) ------------

def test_wire_footswitch_writes_targetbypass_controller(goldfinger_body, library):
    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)

    bnn = goldfinger_body["preset"]["flow"][0]["b01"]
    controller = bnn["@enabled"]["controller"]
    source_id = CONTROLLER_SOURCE_IDS["stadium_xl"]["FS3"]
    assert source_id == 0x01010102
    assert controller["source"] == source_id
    assert controller["type"] == "targetbypass"
    assert controller["behavior"] == "latching"
    # Digital footswitch: null bounds (only EXP-toe position switches need
    # explicit min/max/threshold).
    assert controller["min"] is None
    assert controller["max"] is None

    sources = goldfinger_body["preset"]["sources"]
    assert sources[str(source_id)] == {"bypass": False}


def test_wire_footswitch_momentary_behavior(goldfinger_body, library):
    mutate.wire_footswitch(goldfinger_body, "FS4", "Digital", "momentary", library)
    bnn = goldfinger_body["preset"]["flow"][0]["b04"]
    assert bnn["@enabled"]["controller"]["behavior"] == "momentary"


def test_wire_footswitch_duplicate_switch_raises(goldfinger_body, library):
    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)
    with pytest.raises(mutate.MutateError):
        mutate.wire_footswitch(goldfinger_body, "FS3", "Digital", "latching", library)


def test_wire_footswitch_unknown_block_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.wire_footswitch(goldfinger_body, "FS3", "Nope Amp", "latching", library)


def test_wire_footswitch_rejects_rewiring_block_to_different_switch(goldfinger_body, library):
    # Wiring a second, different switch to an already-wired block must not
    # silently overwrite the bnn-level controller -- that would orphan the
    # first switch's `sources` entry (still present, but nothing points at
    # it any more).
    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)
    with pytest.raises(mutate.MutateError):
        mutate.wire_footswitch(goldfinger_body, "FS4", "Scream 808", "latching", library)

    fs3_source = CONTROLLER_SOURCE_IDS["stadium_xl"]["FS3"]
    fs4_source = CONTROLLER_SOURCE_IDS["stadium_xl"]["FS4"]
    bnn = goldfinger_body["preset"]["flow"][0]["b01"]
    assert bnn["@enabled"]["controller"]["source"] == fs3_source
    sources = goldfinger_body["preset"]["sources"]
    assert str(fs3_source) in sources
    assert str(fs4_source) not in sources  # no orphan registered


def test_wire_footswitch_same_switch_same_block_is_idempotent(goldfinger_body, library):
    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)
    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)

    fs3_source = CONTROLLER_SOURCE_IDS["stadium_xl"]["FS3"]
    bnn = goldfinger_body["preset"]["flow"][0]["b01"]
    assert bnn["@enabled"]["controller"]["source"] == fs3_source
    sources = goldfinger_body["preset"]["sources"]
    assert list(sources.keys()) == [str(fs3_source)]


def test_wire_footswitch_allows_preexisting_source_metadata(goldfinger_body, library):
    # A chassis cloned from a real export carries pre-existing device-metadata
    # `sources` entries (e.g. FS1..FS10) that are NOT actual block bindings.
    # Presence of the target switch's source id in the `sources` table must
    # NOT be treated as a conflict when no bNN's `@enabled.controller` actually
    # points at it (regression: false-positive "switch already assigned").
    fs3_source = CONTROLLER_SOURCE_IDS["stadium_xl"]["FS3"]
    sources = goldfinger_body["preset"].setdefault("sources", {})
    sources[str(fs3_source)] = {"bypass": False}  # device metadata, no block bound

    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)

    bnn = goldfinger_body["preset"]["flow"][0]["b01"]
    assert bnn["@enabled"]["controller"]["source"] == fs3_source


def test_wire_footswitch_two_blocks_same_switch_still_raises(goldfinger_body, library):
    # Even with the flow-scan conflict check (not a sources-table check),
    # wiring two DIFFERENT blocks to the same switch must still raise, because
    # a real targetbypass binding on a different bNN already claims the source.
    mutate.wire_footswitch(goldfinger_body, "FS3", "Scream 808", "latching", library)
    with pytest.raises(mutate.MutateError, match="already assigned"):
        mutate.wire_footswitch(goldfinger_body, "FS3", "Digital", "latching", library)


def test_wire_expression_writes_param_controller(goldfinger_body, library):
    mutate.wire_expression(
        goldfinger_body, "EXP1",
        [{"block": "Brit 2204 Custom", "param": "Drive", "min": 0.1, "max": 0.9}],
        library,
    )
    wrapped = goldfinger_body["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]
    controller = wrapped["controller"]
    source_id = CONTROLLER_SOURCE_IDS["stadium_xl"]["EXP1"]
    assert source_id == 0x01020100
    assert controller["source"] == source_id
    assert controller["type"] == "param"
    assert controller["min"] == 0.1
    assert controller["max"] == 0.9

    sources = goldfinger_body["preset"]["sources"]
    assert sources[str(source_id)] == {"bypass": False}


def test_wire_expression_defaults_min_max(goldfinger_body, library):
    mutate.wire_expression(
        goldfinger_body, "EXP2",
        [{"block": "Digital", "param": "Mix"}],
        library,
    )
    wrapped = goldfinger_body["preset"]["flow"][0]["b04"]["slot"][0]["params"]["Mix"]
    assert wrapped["controller"]["min"] == 0.0
    assert wrapped["controller"]["max"] == 1.0


def test_wire_expression_multiple_targets_one_pedal(goldfinger_body, library):
    mutate.wire_expression(
        goldfinger_body, "EXP2",
        [
            {"block": "Brit 2204 Custom", "param": "Drive", "min": 0.1, "max": 0.9},
            {"block": "Digital", "param": "Mix", "min": 0.0, "max": 0.4},
        ],
        library,
    )
    drive = goldfinger_body["preset"]["flow"][0]["b02"]["slot"][0]["params"]["Drive"]
    mix = goldfinger_body["preset"]["flow"][0]["b04"]["slot"][0]["params"]["Mix"]
    assert drive["controller"]["source"] == mix["controller"]["source"]
    assert len(goldfinger_body["preset"]["sources"]) == 1


def test_wire_expression_duplicate_block_param_raises(goldfinger_body, library):
    mutate.wire_expression(
        goldfinger_body, "EXP1", [{"block": "Digital", "param": "Mix"}], library
    )
    with pytest.raises(mutate.MutateError):
        mutate.wire_expression(
            goldfinger_body, "EXP2", [{"block": "Digital", "param": "Mix"}], library
        )


def test_wire_expression_duplicate_within_call_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.wire_expression(
            goldfinger_body, "EXP1",
            [
                {"block": "Digital", "param": "Mix"},
                {"block": "Digital", "param": "Mix"},
            ],
            library,
        )


def test_wire_expression_unknown_param_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.wire_expression(
            goldfinger_body, "EXP1", [{"block": "Digital", "param": "NotAParam"}], library
        )


def test_wire_expression_empty_targets_raises(goldfinger_body, library):
    with pytest.raises(mutate.MutateError):
        mutate.wire_expression(goldfinger_body, "EXP1", [], library)


def test_wire_wah_toe_uses_exp1toe_source(goldfinger_body, library):
    mutate.wire_wah_toe(goldfinger_body, "Digital", library)

    bnn = goldfinger_body["preset"]["flow"][0]["b04"]
    controller = bnn["@enabled"]["controller"]
    source_id = CONTROLLER_SOURCE_IDS["stadium_xl"]["EXP1Toe"]
    assert source_id == 0x01010500
    assert controller["source"] == 0x01010500
    assert controller["type"] == "targetbypass"
    assert controller["behavior"] == "latching"
    # Position switch: explicit min/max/threshold, unlike a digital FS.
    assert controller["min"] is False
    assert controller["max"] is True
    assert controller["threshold"] == 0.0

    sources = goldfinger_body["preset"]["sources"]
    assert sources[str(source_id)] == {"bypass": False}


def test_wire_wah_toe_duplicate_raises(goldfinger_body, library):
    mutate.wire_wah_toe(goldfinger_body, "Digital", library)
    with pytest.raises(mutate.MutateError):
        mutate.wire_wah_toe(goldfinger_body, "Plate", library)


# --- golden micro-test (Task 1c step 4) -----------------------------------

def test_golden_micro_single_param_diff(goldfinger_body, library):
    """Mutating one param changes the parsed dict at exactly that one path;
    everything else (including any harness/dual-slot fields, had this fixture
    carried any) is byte-identical."""
    before = copy.deepcopy(goldfinger_body)
    mutate.set_param(goldfinger_body, "Digital", "Mix", 0.61, library)

    diffs = _diff_paths(before, goldfinger_body)
    assert diffs == [("preset", "flow", 0, "b04", "slot", 0, "params", "Mix", "value")]


def test_golden_micro_single_param_diff_survives_dual_cab_and_harness(dual_cab_raw_body, library):
    """The headline `raw`-preservation guarantee, exercised against a fixture
    that actually HAS opaque verbatim state: dual_cab_raw.hsp's b02 (cab)
    carries a 2-entry `slot` (dual-cab) and a block-level `harness` dict.
    Mutating an unrelated block's param must leave both byte-identical --
    the previous golden-micro test only covered a fixture with neither."""
    before = copy.deepcopy(dual_cab_raw_body)
    b02_before = before["preset"]["flow"][0]["b02"]
    assert len(b02_before["slot"]) == 2  # sanity: fixture really is dual-cab
    assert "harness" in b02_before  # sanity: fixture really carries a harness

    mutate.set_param(dual_cab_raw_body, "Brit 2204 Custom", "Drive", 0.42, library)

    diffs = _diff_paths(before, dual_cab_raw_body)
    assert diffs == [("preset", "flow", 0, "b01", "slot", 0, "params", "Drive", "value")]

    b02_after = dual_cab_raw_body["preset"]["flow"][0]["b02"]
    assert b02_after["slot"][1] == b02_before["slot"][1]
    assert b02_after["harness"] == b02_before["harness"]


def _diff_paths(a, b, prefix=()):
    """Return the list of key-paths where two nested dict/list structures
    differ (leaf paths only)."""
    if isinstance(a, dict) and isinstance(b, dict):
        diffs = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                diffs.append(prefix + (k,))
                continue
            diffs.extend(_diff_paths(a[k], b[k], prefix + (k,)))
        return diffs
    if isinstance(a, list) and isinstance(b, list):
        diffs = []
        for i, (av, bv) in enumerate(zip(a, b)):
            diffs.extend(_diff_paths(av, bv, prefix + (i,)))
        if len(a) != len(b):
            diffs.append(prefix + ("<length>",))
        return diffs
    if a != b:
        return [prefix]
    return []
