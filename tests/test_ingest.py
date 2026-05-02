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
