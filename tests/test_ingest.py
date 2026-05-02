import pytest

from helixgen.ingest import humanize_model_id, infer_category


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("HD2_AmpBrit2204Custom", "Brit 2204 Custom"),
        ("HD2_Cab4x12Greenback25", "4x12 Greenback 25"),
        ("HD2_DrvScream808", "Scream 808"),
        ("HD2_DynamicsNoiseGate", "Noise Gate"),
        ("HD2_RvbPlate", "Plate"),
        ("UnknownPrefixThing", "Unknown Prefix Thing"),
    ],
)
def test_humanize_model_id(model_id, expected):
    assert humanize_model_id(model_id) == expected


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("HD2_AmpBrit2204Custom", "amp"),
        ("HD2_Cab4x12Greenback25", "cab"),
        ("HD2_DrvScream808", "drive"),
        ("HD2_DistFuzz", "drive"),
        ("HD2_RvbPlate", "reverb"),
        ("HD2_DlyDigital", "delay"),
        ("HD2_EQParametric", "eq"),
        ("HD2_DynamicsNoiseGate", "dynamics"),
        ("HD2_ModChorus", "modulation"),
        ("HD2_PitchShift", "pitch"),
        ("HD2_WahCryBaby", "filter"),
        ("HD2_TotallyNewThing", "uncategorized"),
        ("WeirdNoPrefix", "uncategorized"),
    ],
)
def test_infer_category(model_id, expected):
    assert infer_category(model_id) == expected


from helixgen.ingest import Shape, detect_shape


def test_detect_full_preset(sample_serial_preset):
    assert detect_shape(sample_serial_preset) == Shape.PRESET


def test_detect_single_block(sample_amp_block):
    assert detect_shape(sample_amp_block) == Shape.SINGLE_BLOCK


def test_detect_unknown_shape():
    assert detect_shape({"foo": "bar"}) == Shape.UNKNOWN
    assert detect_shape([]) == Shape.UNKNOWN
    assert detect_shape("just a string") == Shape.UNKNOWN


from helixgen.ingest import extract_schema


def test_extract_schema_floats(sample_amp_block):
    schema = extract_schema(sample_amp_block)
    assert "Drive" in schema
    assert schema["Drive"]["type"] == "float"
    assert schema["Drive"]["default"] == 0.6
    assert schema["Drive"]["observed_range"] == [0.6, 0.6]


def test_extract_schema_skips_system_keys(sample_amp_block):
    schema = extract_schema(sample_amp_block)
    assert "@model" not in schema
    assert "@enabled" not in schema


def test_extract_schema_int_and_string(sample_cab_block):
    schema = extract_schema(sample_cab_block)
    assert schema["High Cut"]["type"] == "int"
    assert schema["High Cut"]["default"] == 8000
    assert schema["Mic"]["type"] == "str"
    assert schema["Mic"]["default"] == "57 Dynamic"


def test_extract_schema_handles_bool():
    schema = extract_schema({"@model": "X", "Loop": True})
    assert schema["Loop"]["type"] == "bool"
    assert schema["Loop"]["default"] is True


from helixgen.ingest import (
    block_from_raw,
    extract_block_from_single,
    extract_blocks_from_preset,
)


def test_extract_blocks_from_preset(sample_serial_preset):
    blocks = extract_blocks_from_preset(sample_serial_preset)
    model_ids = [b["@model"] for b in blocks]
    assert model_ids == [
        "HD2_DynamicsNoiseGate",
        "HD2_DrvScream808",
        "HD2_AmpBrit2204Custom",
        "HD2_Cab4x12Greenback25",
        "HD2_RvbPlate",
    ]


def test_extract_blocks_from_preset_handles_empty_dsp1(sample_serial_preset):
    blocks = extract_blocks_from_preset(sample_serial_preset)
    assert len(blocks) == 5


def test_extract_block_from_single(sample_amp_block):
    block = extract_block_from_single(sample_amp_block)
    assert block["@model"] == "HD2_AmpBrit2204Custom"


def test_block_from_raw_uses_humanized_name_when_no_explicit_name(sample_amp_block):
    source_info = {"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"}
    block = block_from_raw(sample_amp_block, source_info)
    assert block.model_id == "HD2_AmpBrit2204Custom"
    assert block.category == "amp"
    assert block.display_name == "Brit 2204 Custom"
    assert "Drive" in block.params
    assert block.exemplar == sample_amp_block
    assert block.first_seen == source_info


def test_block_from_raw_prefers_explicit_name_field():
    raw = {"@model": "HD2_AmpBrit2204Custom", "@name": "Brit JCM 800", "Drive": 0.5}
    block = block_from_raw(raw, {"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"})
    assert block.display_name == "Brit JCM 800"


def test_block_from_raw_prefers_explicit_category_field():
    raw = {"@model": "HD2_TotallyNewThing", "@category": "amp", "Drive": 0.5}
    block = block_from_raw(raw, {"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"})
    assert block.category == "amp"


import json
from pathlib import Path

from helixgen.ingest import IngestSummary, ingest_file
from helixgen.library import Library


def test_ingest_file_full_preset(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    summary = ingest_file(preset_path, lib)

    assert summary.new == 5
    assert summary.matched == 0
    assert summary.conflicted == 0
    assert len(lib.list_blocks()) == 5


def test_ingest_file_single_block(tmp_library, sample_amp_block, tmp_path):
    block_path = tmp_path / "amp.json"
    block_path.write_text(json.dumps(sample_amp_block))
    lib = Library(tmp_library)

    summary = ingest_file(block_path, lib)

    assert summary.new == 1
    assert len(lib.list_blocks()) == 1


def test_ingest_file_idempotent(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    first = ingest_file(preset_path, lib)
    second = ingest_file(preset_path, lib)

    assert first.new == 5
    assert second.new == 0
    assert second.matched == 5


def test_ingest_file_unparseable_returns_skipped(tmp_library, tmp_path):
    bad_path = tmp_path / "bad.hlx"
    bad_path.write_text("not json {{{")
    lib = Library(tmp_library)

    summary = ingest_file(bad_path, lib)
    assert summary.skipped == 1
    assert summary.new == 0


def test_ingest_file_unknown_shape_returns_skipped(tmp_library, tmp_path):
    weird = tmp_path / "weird.json"
    weird.write_text(json.dumps({"foo": "bar"}))
    lib = Library(tmp_library)

    summary = ingest_file(weird, lib)
    assert summary.skipped == 1


def test_ingest_extracts_chassis_on_first_full_preset(
    tmp_library, sample_serial_preset, tmp_path
):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    assert not lib.has_chassis()
    summary = ingest_file(preset_path, lib)

    assert summary.chassis_extracted is True
    assert lib.has_chassis()
    chassis = lib.load_chassis()
    assert chassis["data"]["tone"]["dsp0"]["blocks"] == {}


def test_ingest_does_not_re_extract_chassis(
    tmp_library, sample_serial_preset, tmp_path
):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    ingest_file(preset_path, lib)
    second = ingest_file(preset_path, lib)
    assert second.chassis_extracted is False


def test_ingest_single_block_does_not_extract_chassis(
    tmp_library, sample_amp_block, tmp_path
):
    block_path = tmp_path / "amp.json"
    block_path.write_text(json.dumps(sample_amp_block))
    lib = Library(tmp_library)

    summary = ingest_file(block_path, lib)
    assert summary.chassis_extracted is False
    assert not lib.has_chassis()


from helixgen.ingest import ingest_path


def test_ingest_path_directory(tmp_library, sample_serial_preset, sample_amp_block, tmp_path):
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    (presets_dir / "preset.hlx").write_text(json.dumps(sample_serial_preset))
    (presets_dir / "amp.json").write_text(json.dumps(sample_amp_block))
    (presets_dir / "junk.txt").write_text("ignore me")

    lib = Library(tmp_library)
    summary = ingest_path(presets_dir, lib)

    assert summary.new + summary.matched == 6
    assert summary.skipped == 0


def test_ingest_path_recurses(tmp_library, sample_serial_preset, tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.hlx").write_text(json.dumps(sample_serial_preset))

    lib = Library(tmp_library)
    summary = ingest_path(tmp_path, lib)

    assert summary.new == 5


def test_ingest_path_single_file_arg(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    summary = ingest_path(preset_path, lib)
    assert summary.new == 5


def test_ingest_path_rebuilds_index(tmp_library, sample_serial_preset, tmp_path):
    preset_path = tmp_path / "preset.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)

    ingest_path(preset_path, lib)
    assert (tmp_library / "index.json").exists()
