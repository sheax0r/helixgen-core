"""Tests for the vendored helixgen -> device numeric model-id map.

These run against the VENDORED ``_modelmap.json`` (and the vendored device
defs) only — they never require the editor app bundle or the on-disk helixgen
library, so the suite stays green on a clean clone.
"""
from __future__ import annotations

import pytest

from helixgen.device import defs, modelmap


def test_asset_loads_and_is_shaped():
    m = modelmap.load_modelmap()
    assert set(m).issuperset({"map", "unmatched", "meta"})
    assert isinstance(m["map"], dict)
    assert isinstance(m["unmatched"], list)
    assert m["map"], "map must not be empty"


def test_map_is_sorted_and_values_are_ints():
    m = modelmap.load_modelmap()
    keys = list(m["map"].keys())
    assert keys == sorted(keys), "map keys must be deterministically sorted"
    assert all(isinstance(v, int) for v in m["map"].values())


def test_coverage_is_sane():
    cov = modelmap.coverage()
    assert cov["helixgen_total"] > 0
    assert cov["matched"] + cov["unmatched"] == cov["helixgen_total"]
    assert 0.0 <= cov["coverage_pct"] <= 100.0
    assert cov["matched"] > 0


def test_exact_string_matches_agree_with_device_defs():
    """Every model whose helixgen id IS a device model string must resolve to
    that same device numeric id — a direct cross-check against the (separately
    vendored) device defs, no app required."""
    m = modelmap.load_modelmap()
    checked = 0
    for lib_id, dev_id in m["map"].items():
        expected = defs.model_id_for(lib_id)
        if expected is not None:  # lib_id is itself a device model string
            assert dev_id == expected, (
                f"{lib_id} maps to {dev_id} but device defs say {expected}"
            )
            checked += 1
    assert checked > 100, "expected many exact string matches to cross-check"


@pytest.mark.parametrize(
    "lib_id,dev_id,dev_str",
    [
        # Translated helixgen ids (helixgen strips the Mono suffix / renames);
        # must reconcile to the device's Mono numeric ids. Cross-checked below
        # against defs so the expected numbers aren't magic constants.
        ("HD2_DrvScream808", 310, "HD2_DistScream808Mono"),
        ("HD2_DistCompulsiveDrive", 305, "HD2_DistCompulsiveDriveMono"),
        ("HD2_VolPanVol", 268, "HD2_VolPanVolMono"),
    ],
)
def test_known_translated_models_resolve(lib_id, dev_id, dev_str):
    assert modelmap.device_model_id(lib_id) == dev_id
    # the numeric id must be the device model string we claim it is
    assert defs.model_id_for(dev_str) == dev_id
    assert defs.model_name_for(dev_id) == dev_str
    # and these are NOT trivial exact-string matches
    assert defs.model_id_for(lib_id) is None
    detail = modelmap.match_detail(lib_id)
    assert detail is not None
    assert detail["method"] != "exact_id"
    assert detail["param_jaccard"] == 1.0


def test_a_plain_exact_match_resolves():
    # A representative directly-shared model id.
    assert modelmap.device_model_id("HD2_ReverbPlateStereo") == defs.model_id_for(
        "HD2_ReverbPlateStereo"
    )
    detail = modelmap.match_detail("HD2_ReverbPlateStereo")
    assert detail["method"] == "exact_id"


def test_unknown_id_returns_none_and_is_not_flagged_unmatched():
    assert modelmap.device_model_id("HD2_NotARealModel_xyz") is None
    assert modelmap.is_unmatched("HD2_NotARealModel_xyz") is False


def test_unmatched_entries_return_none_and_flag_true():
    m = modelmap.load_modelmap()
    for lib_id in m["unmatched"]:
        assert modelmap.device_model_id(lib_id) is None
        assert modelmap.is_unmatched(lib_id) is True
        assert lib_id not in m["map"]


def test_matched_ids_are_not_in_unmatched():
    m = modelmap.load_modelmap()
    assert set(m["map"]).isdisjoint(set(m["unmatched"]))


def test_all_device_ids_are_real_device_models():
    """No mapped numeric id may be a phantom — each must round-trip through the
    device defs to a real model string."""
    m = modelmap.load_modelmap()
    for lib_id, dev_id in m["map"].items():
        assert defs.model_name_for(dev_id) is not None, (
            f"{lib_id} -> {dev_id} is not a known device numeric id"
        )
