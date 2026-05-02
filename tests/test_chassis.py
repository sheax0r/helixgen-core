import json

from helixgen.chassis import extract_chassis


def test_extract_chassis_strips_blocks_and_cabs(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    dsp0 = chassis["data"]["tone"]["dsp0"]
    assert not any(k.startswith("block") for k in dsp0)
    assert not any(k.startswith("cab") for k in dsp0)


def test_extract_chassis_preserves_dsp_infrastructure(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    dsp0 = chassis["data"]["tone"]["dsp0"]
    for key in ("inputA", "inputB", "outputA", "outputB", "split", "join"):
        assert key in dsp0, f"missing infrastructure key {key!r}"


def test_extract_chassis_preserves_meta_and_top_tone(sample_serial_preset):
    chassis = extract_chassis(sample_serial_preset)
    assert chassis["version"] == 6
    assert chassis["schema"] == "L6Preset"
    assert chassis["data"]["device"] == 2162689
    assert "global" in chassis["data"]["tone"]


def test_extract_chassis_does_not_mutate_input(sample_serial_preset):
    original = json.loads(json.dumps(sample_serial_preset))
    extract_chassis(sample_serial_preset)
    assert sample_serial_preset == original


def test_extract_chassis_against_real_possum(tmp_path):
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "presets" / "possum.hlx"
    preset = json.loads(fixture.read_text())
    chassis = extract_chassis(preset)
    dsp0 = chassis["data"]["tone"]["dsp0"]
    assert not any(k.startswith("block") for k in dsp0)
    assert not any(k.startswith("cab") for k in dsp0)
    assert "inputA" in dsp0 and "outputA" in dsp0 and "split" in dsp0
    assert "snapshot0" in chassis["data"]["tone"]
    assert "global" in chassis["data"]["tone"]
