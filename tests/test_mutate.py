"""Tests for helixgen.mutate — the .hsp-canonical body-mutation verbs.

These operate directly on a parsed `.hsp` body dict (`preset.flow[*].bNN`),
not on a spec.json. See docs/superpowers/plans/2026-07-08-hsp-canonical-redesign.md
Tasks 1b/1c.
"""
from __future__ import annotations

import copy

import pytest

from helixgen import mutate
from helixgen.generate import ParamValidationError
from helixgen.hsp import read_hsp
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


# --- golden micro-test (Task 1c step 4) -----------------------------------

def test_golden_micro_single_param_diff(goldfinger_body, library):
    """Mutating one param changes the parsed dict at exactly that one path;
    everything else (including any harness/dual-slot fields, had this fixture
    carried any) is byte-identical."""
    before = copy.deepcopy(goldfinger_body)
    mutate.set_param(goldfinger_body, "Digital", "Mix", 0.61, library)

    diffs = _diff_paths(before, goldfinger_body)
    assert diffs == [("preset", "flow", 0, "b04", "slot", 0, "params", "Mix", "value")]


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
