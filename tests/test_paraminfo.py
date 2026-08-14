"""hgc-285: real min/max/default + units + enum labels for library blocks.

The block library records one sighted sample per param; everything an author
needs to set that param correctly lives in the two vendored assets that
``helixgen.device.paraminfo`` overlays at read time. These tests pin the
resolver, the unit/label join, and the coverage those assets actually give —
so a regression in either asset is loud rather than silent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device import defs, modelmap, paraminfo
from helixgen.library import Block, Library

# Real models, resolved entirely from vendored assets (no ~/.helixgen needed).
AGOURA_AMP = "Agoura_AmpBrit2203MV"      # Level is in dB
LEGACY_AMP = "HD2_AmpBritPlexiBrt"       # ChVol is 0..1
CAB = "HD2_CabMicIr_1x12BlueBellWithPan"  # Mic is an enum, IrData is internal
DELAY = "HD2_DL4TapeEchoStereo"          # SyncSelect1 is the implied sync_note


# --- the 3-candidate model resolver -----------------------------------------

def test_candidate_1_exact_model_id():
    assert paraminfo.model_candidates(LEGACY_AMP)[0] == LEGACY_AMP


def test_candidate_2_modelmap_translation():
    # helixgen translates this id on ingest; the device spells it differently.
    assert defs.model_id_for("HD2_DrvScream808") is None
    assert modelmap.device_model_id("HD2_DrvScream808") is not None
    assert "HD2_DistScream808Mono" in paraminfo.model_candidates("HD2_DrvScream808")


def test_candidate_3_stereo_falls_back_to_mono_sibling():
    stereo, mono = "HX2_GateHorizonGateStereo", "HX2_GateHorizonGateMono"
    # The stereo model exists in the defs but has no UI entry of its own.
    assert stereo in defs.load_defs()["models"]
    assert stereo not in paraminfo.load_param_ui()["models"]
    assert paraminfo.model_candidates(stereo) == (stereo, mono)
    # ...so its labels come from the mono sibling rather than going missing.
    assert paraminfo.param_info(stereo, "Mode")["enum_labels"] == ["Bass", "Guitar"]


def test_unknown_model_resolves_to_nothing():
    assert paraminfo.model_candidates("NOPE_NotAModel") == ()
    assert paraminfo.param_info("NOPE_NotAModel", "Drive") == {}


# --- the bug this issue exists to prevent: dB vs 0..1 -----------------------

def test_agoura_level_is_decibels_not_a_unit_interval():
    info = paraminfo.param_info(AGOURA_AMP, "Level")
    assert info["unit"] == "dB"
    assert (info["min"], info["max"]) == (-40, 10)
    assert info["default"] == -10
    assert "scale" not in info  # dB is displayed raw


def test_legacy_amp_channel_volume_is_a_unit_interval():
    info = paraminfo.param_info(LEGACY_AMP, "ChVol")
    assert (info["min"], info["max"]) == (0, 1)
    assert "unit" not in info
    assert info["scale"] == 10          # a 0..1 knob reads 0.0-10.0 on screen
    assert info["display_name"] == "Level"  # ...and is LABELLED "Level" there


def test_show_block_makes_the_two_level_params_distinguishable(tmp_path):
    library = _library_with(tmp_path, [
        (AGOURA_AMP, "amp", "Brit 2203 MV", {"Level": {"type": "float", "default": 2.7}}),
        (LEGACY_AMP, "amp", "Brit Plexi Brt", {"ChVol": {"type": "float", "default": 0.5}}),
    ])
    agoura = _show_block(library, "Brit 2203 MV")
    legacy = _show_block(library, "Brit Plexi Brt")
    assert "Level  float -40..10 dB" in agoura
    assert "dB" not in legacy
    assert "ChVol  float 0..1" in legacy
    assert "displays 0-10" in legacy


# --- enum labels ------------------------------------------------------------

def test_enum_labels_render_the_value_not_just_the_number():
    info = paraminfo.param_info(CAB, "Mic")
    assert (info["min"], info["max"]) == (0, 11)
    assert paraminfo.label_for(info, 5) == "121 Ribbon"
    assert paraminfo.label_for(info, 0) == "57 Dynamic"
    assert paraminfo.label_for(info, 12) is None   # out of range: no guess
    assert paraminfo.label_for(info, None) is None


def test_syncselect_gets_the_note_divisions_the_uidefs_omit():
    info = paraminfo.param_info(DELAY, "SyncSelect1")
    assert (info["min"], info["max"]) == (1, 19)
    assert len(info["enum_labels"]) == 19
    # Labels are aligned to min, not to zero: 6 is a quarter note, not 1/4 Trip.
    assert paraminfo.label_for(info, 6) == "1/4"


def test_show_block_prints_enum_labels(tmp_path):
    library = _library_with(tmp_path, [
        (CAB, "cab", "Blue Bell", {"Mic": {"type": "int", "default": 4}}),
    ])
    out = _show_block(library, "Blue Bell")
    assert 'sighted 4 "30 Dynamic"' in out
    assert "values: 0=57 Dynamic" in out
    assert "5=121 Ribbon" in out


# --- params the editor deliberately does not expose -------------------------

def test_internal_param_gets_no_invented_unit():
    info = paraminfo.param_info(CAB, "IrData")
    assert info["internal"] is True
    assert "unit" not in info and "enum_labels" not in info
    assert "min" in info  # the raw range is still reported


def test_show_block_flags_internal_params(tmp_path):
    library = _library_with(tmp_path, [
        (CAB, "cab", "Blue Bell", {"IrData": {"type": "int", "default": 0},
                                   "Mic": {"type": "int", "default": 4}}),
    ])
    lines = {line.split()[0]: line for line in _show_block(library, "Blue Bell").splitlines()
             if line.startswith("  ")}
    assert "[internal]" in lines["IrData"]
    assert "[internal]" not in lines["Mic"]


# --- the library schema stops lying about being a range ---------------------

def test_block_param_info_renames_the_single_sample_to_sighted():
    schema = {"Level": {"type": "float", "default": 2.7, "observed_range": [2.7, 2.7]}}
    info = paraminfo.block_param_info(AGOURA_AMP, schema)["Level"]
    assert info["sighted"] == 2.7
    assert "observed_range" not in info
    assert info["default"] == -10  # the DEVICE default, not the sighted value


def test_show_block_never_prints_a_degenerate_observed_range(tmp_path):
    library = _library_with(tmp_path, [
        (AGOURA_AMP, "amp", "Brit 2203 MV",
         {"Level": {"type": "float", "default": 2.7, "observed_range": [2.7, 2.7]}}),
    ])
    out = _show_block(library, "Brit 2203 MV")
    assert "observed" not in out
    assert "[2.7, 2.7]" not in out


def test_show_block_json_carries_the_resolved_facts(tmp_path):
    library = _library_with(tmp_path, [
        (CAB, "cab", "Blue Bell", {"Mic": {"type": "int", "default": 4},
                                   "Level": {"type": "float", "default": 6.0}}),
    ])
    data = json.loads(_show_block(library, "Blue Bell", "--json"))
    mic = data["params"]["Mic"]
    assert mic["min"] == 0 and mic["max"] == 11 and mic["default"] == 11
    assert mic["sighted"] == 4 and mic["sighted_label"] == "30 Dynamic"
    assert mic["enum_labels"][5] == "121 Ribbon"
    assert data["params"]["Level"]["unit"] == "dB"


# --- coverage: the asset claims, pinned ------------------------------------

def _mapped_models() -> list[str]:
    return sorted(modelmap.load_modelmap()["map"])


def test_every_mapped_model_resolves_a_param_table():
    unresolved = [m for m in _mapped_models() if not paraminfo.model_candidates(m)]
    assert unresolved == []


def test_every_device_param_of_every_mapped_model_has_a_real_range():
    missing = []
    for model in _mapped_models():
        for param in defs.model_params_for(paraminfo.model_candidates(model)[0]):
            info = paraminfo.param_info(model, param)
            if "min" not in info or "max" not in info or "default" not in info:
                missing.append((model, param))
    assert missing == [], f"{len(missing)} params lost their range"


def test_ui_coverage_stays_around_the_measured_89_percent():
    total = exposed = 0
    for model in _mapped_models():
        for param in defs.model_params_for(paraminfo.model_candidates(model)[0]):
            total += 1
            if not paraminfo.param_info(model, param).get("internal"):
                exposed += 1
    ratio = exposed / total
    # Measured 2915/3280 = 88.9% when the asset was built (app 1.3.2.9805).
    # The uncovered remainder is internal plumbing; a big move either way means
    # the UIDefs join broke, not that the editor changed its mind.
    assert 0.86 <= ratio <= 0.93, f"UI coverage moved to {ratio:.3f}"


def test_enum_labels_always_span_the_declared_range():
    """`label_for` indexes labels from `min`; that is only sound if the label
    list covers exactly the declared range."""
    bad = []
    for model in _mapped_models():
        for param in defs.model_params_for(paraminfo.model_candidates(model)[0]):
            info = paraminfo.param_info(model, param)
            labels = info.get("enum_labels")
            if not labels or "min" not in info or "max" not in info:
                continue
            if len(labels) != int(info["max"]) - int(info["min"]) + 1:
                bad.append((model, param, len(labels), info["min"], info["max"]))
    assert bad == []


def test_shipped_library_resolves_completely_if_present():
    """The real user library, when this machine has one (skipped on CI)."""
    root = Path.home() / ".helixgen" / "library" / "blocks"
    if not root.is_dir():
        pytest.skip("no local block library")
    total = ranged = exposed = 0
    for path in root.glob("*/*.json"):
        block = json.loads(path.read_text())
        for info in paraminfo.block_param_info(block["model_id"], block["params"]).values():
            total += 1
            ranged += "min" in info and "max" in info and "default" in info
            exposed += not info.get("internal")
    assert total > 0
    assert ranged == total, f"{total - ranged}/{total} library params have no range"
    assert exposed / total >= 0.86


# --- helpers ----------------------------------------------------------------

def _library_with(tmp_path: Path, blocks) -> Path:
    """Build a throwaway library holding the given (model_id, category, name,
    params) blocks."""
    library = Library(root=tmp_path / "lib")
    for model_id, category, display_name, params in blocks:
        library.save_block(Block(
            model_id=model_id, category=category, display_name=display_name,
            params=params, exemplar={"@model": model_id},
            first_seen={"preset": "_", "firmware": "_", "date": "2026-08-13"}))
    library.rebuild_index()
    return library.root


def _show_block(library: Path, name: str, *flags: str) -> str:
    result = CliRunner().invoke(
        cli, ["show-block", name, "--library", str(library), *flags])
    assert result.exit_code == 0, result.output
    return result.output
