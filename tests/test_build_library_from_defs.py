"""``tools/build_library_from_defs.py`` fills a block library with every
user-placeable catalog model it lacks, straight from the vendored device defs
(the modeldefs carry each param's raw default, the same units a ``.hsp``
stores). Sighted entries are never touched (bead hgc-6r5.3)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from helixgen.library import Library

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "build_library_from_defs.py"
_spec = importlib.util.spec_from_file_location("build_library_from_defs", _TOOL)
blf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(blf)


def _fill(tmp_path, **kw):
    lib = Library(tmp_path)
    written, skipped = blf.fill_library(lib, **kw)
    return lib, written, skipped


def test_new_1_4_amp_is_generated_with_raw_defaults(tmp_path):
    lib, written, _ = _fill(tmp_path)
    b = lib.load_block("Agoura_AmpBritSL100")
    assert b.category == "amp"
    assert b.display_name == "Brit SL100"
    assert b.exemplar["@model"] == "Agoura_AmpBritSL100"
    assert b.exemplar["@type"] == "amp"
    # Level is dB-raw on Agoura amps (def -10), knobs are 0..1, enums int.
    assert b.params["Level"]["type"] == "float"
    assert b.exemplar["Level"] == -10.0
    assert b.params["Era"]["type"] == "int"
    assert "Agoura_AmpBritSL100" in written


def test_stereo_partner_and_fx_type(tmp_path):
    lib, written, _ = _fill(tmp_path)
    mono = lib.load_block("HD2_DistMercatorMono")
    stereo = lib.load_block("HD2_DistMercatorStereo")
    assert mono.category == stereo.category == "drive"
    assert mono.exemplar["@type"] == "fx"
    assert set(mono.params) == set(stereo.params)
    assert lib.load_block("HD2_ChorusDimensionWStereo").category == "modulation"


def test_exemplar_params_sorted_and_typed(tmp_path):
    lib, _, _ = _fill(tmp_path)
    ex = lib.load_block("HD2_VibratoEssexVibeMono").exemplar
    keys = [k for k in ex if not k.startswith("@")]
    assert keys == sorted(keys)
    params = lib.load_block("HD2_VibratoEssexVibeMono").params
    for k in keys:
        assert type(ex[k]).__name__ == params[k]["type"]


def test_existing_entries_are_left_alone(tmp_path):
    lib = Library(tmp_path)
    path = tmp_path / "blocks" / "drive" / "HD2_DistMercatorMono.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "model_id": "HD2_DistMercatorMono", "category": "drive",
        "display_name": "x", "params": {}, "exemplar": {}, "first_seen": {}}))
    before = path.read_text()
    written, _ = blf.fill_library(lib)
    assert "HD2_DistMercatorMono" not in written
    assert path.read_text() == before


def test_io_and_capture_models_are_skipped(tmp_path):
    lib, written, skipped = _fill(tmp_path)
    for m in ("P35_InputInst1", "P35_InputVariax", "P35_OutputNexusL6L",
              "VIC_AmpFlexNet", "HX2_ImpulseResponseWithPan"):
        assert m not in written
    assert "VIC_AmpFlexNet" in skipped
    assert not list((tmp_path / "blocks").glob("uncategorized/*.json"))


def test_a_model_covered_under_another_spelling_is_not_duplicated(tmp_path):
    """``HD2_DrvScream808`` (HX-era spelling) is the device's
    ``HD2_DistScream808Mono``; generating the latter would put the same model
    in the library twice."""
    lib = Library(tmp_path)
    path = tmp_path / "blocks" / "drive" / "HD2_DrvScream808.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "model_id": "HD2_DrvScream808", "category": "drive",
        "display_name": "x", "params": {}, "exemplar": {}, "first_seen": {}}))
    written, _ = blf.fill_library(lib)
    assert "HD2_DistScream808Mono" not in written
    assert "HD2_DistScream808Stereo" in written


def test_floats_are_float32_like_a_real_export(tmp_path):
    """The device stores and exports float32; an exemplar holding the bare
    decimal (0.707) reads back as a moved knob against a real export's
    0.7070000171661377 — `view` showed every internal AmpCab Q as changed on
    a device-converted Brit SL100."""
    lib, _, _ = _fill(tmp_path)
    ex = lib.load_block("Agoura_AmpBritSL100").exemplar
    assert ex["AmpCabPeakQ"] == 0.7070000171661377


def test_a_generated_name_never_shadows_an_existing_alias(tmp_path):
    """The editor names the legacy HX amp ``HD2_AmpGermanXtraRed`` plain
    "German Xtra Red" — which is the sighted Agoura amp's legacy alias. Display
    names beat aliases in ``find_block``, so a generated entry under the plain
    name would silently re-point every recipe saying "German Xtra Red" at a
    different amp. The generated one gets a distinct, pinned name instead."""
    lib = Library(tmp_path)
    path = tmp_path / "blocks" / "amp" / "Agoura_AmpGermanXtraRed.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "model_id": "Agoura_AmpGermanXtraRed", "category": "amp",
        "display_name": "German Xtra Red", "params": {}, "exemplar": {},
        "first_seen": {}}))
    assert lib.find_block("German Xtra Red").model_id == "Agoura_AmpGermanXtraRed"
    written, _ = blf.fill_library(lib)
    assert "HD2_AmpGermanXtraRed" in written
    assert lib.find_block("German Xtra Red").model_id == "Agoura_AmpGermanXtraRed"
    legacy = lib.load_block("HD2_AmpGermanXtraRed")
    assert legacy.display_name == "German Xtra Red (Legacy)"
    assert lib.find_block("German Xtra Red (Legacy)").model_id == "HD2_AmpGermanXtraRed"


def test_catalog_category_beats_the_prefix_rule(tmp_path):
    """``HD2_FM4*Synth`` would be "filter" by prefix and ``L6SPB_PolyChorus``
    "pitch"; the app's own catalog files them under Pitch/Synth and Modulation."""
    lib, _, _ = _fill(tmp_path)
    assert lib.load_block("HD2_FM4OctiSynth").category == "pitch"
    assert lib.load_block("L6SPB_PolyChorus").category == "modulation"


def test_params_new_in_the_latest_defs_are_left_out(tmp_path):
    """Sighted Agoura amps carry no ``CabIdx`` (1.4-new, factory content on
    1.4 lacks it too), so a generated pre-1.4 Agoura amp leaves it out as well;
    a model new in 1.4 keeps it."""
    lib, _, _ = _fill(tmp_path)
    assert "CabIdx" not in lib.load_block("Agoura_AmpCali2CPlus").params
    assert "CabIdx" in lib.load_block("Agoura_AmpBritSL100").params
