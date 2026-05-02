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
    ]


def test_extract_blocks_from_preset_handles_empty_dsp1(sample_serial_preset):
    blocks = extract_blocks_from_preset(sample_serial_preset)
    assert len(blocks) == 4


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
