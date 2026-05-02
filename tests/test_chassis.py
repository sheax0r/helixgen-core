import json

from helixgen.chassis import extract_chassis


def test_extract_chassis_strips_blocks(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    assert chassis["data"]["tone"]["dsp0"]["blocks"] == {}
    assert chassis["data"]["tone"]["dsp1"]["blocks"] == {}


def test_extract_chassis_records_position_keys(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    keys = chassis["_helixgen"]["position_keys"]
    assert keys["dsp0"] == [
        "dsp0_block_0",
        "dsp0_block_1",
        "dsp0_block_2",
        "dsp0_block_3",
        "dsp0_block_4",
    ]
    assert keys["dsp1"] == []


def test_extract_chassis_preserves_meta_and_routing(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    assert chassis["version"] == 6
    assert chassis["schema"] == "L6Preset"
    assert chassis["data"]["device"]["name"] == "Helix"
    assert chassis["data"]["tone"]["dsp0"]["input"] == "Multi"


def test_extract_chassis_does_not_mutate_input(sample_serial_preset):
    original = json.loads(json.dumps(sample_serial_preset))  # deep copy
    extract_chassis(sample_serial_preset)
    assert sample_serial_preset == original
