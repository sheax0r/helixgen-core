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


# ---------------------------------------------------------------------------
# .hsp Stadium chassis: structurally different from .hlx (preset.flow paths,
# bNN block keys). The chassis form is the .hsp payload with user blocks
# (b01..b12) removed from each path, preserving b00/b13 endpoints + meta.
# ---------------------------------------------------------------------------


def _hsp_preset_for_chassis() -> dict:
    return {
        "meta": {"name": "Possum", "device_version": 38},
        "preset": {
            "flow": [
                {
                    "@enabled": True,
                    "b00": {"type": "input", "slot": [{"model": "P35_InputGuitar"}]},
                    "b01": {"type": "fx", "slot": [{"model": "HD2_DrvScream808"}]},
                    "b02": {"type": "amp", "slot": [{"model": "HD2_AmpBrit2204"}]},
                    "b13": {"type": "output", "slot": [{"model": "P35_OutputMain"}]},
                },
                {
                    "b00": {"type": "input", "slot": [{"model": "P35_InputNone"}]},
                    "b13": {"type": "output", "slot": [{"model": "P35_OutputNone"}]},
                },
            ],
        },
    }


def test_extract_chassis_from_hsp_strips_user_blocks():
    from helixgen.chassis import extract_chassis_from_hsp
    chassis = extract_chassis_from_hsp(_hsp_preset_for_chassis())
    path0 = chassis["preset"]["flow"][0]
    # User-block slots (b01, b02) are gone
    assert "b01" not in path0
    assert "b02" not in path0
    # Endpoints b00/b13 are preserved
    assert "b00" in path0 and "b13" in path0


def test_extract_chassis_from_hsp_preserves_path_metadata_and_meta():
    from helixgen.chassis import extract_chassis_from_hsp
    chassis = extract_chassis_from_hsp(_hsp_preset_for_chassis())
    assert chassis["meta"]["name"] == "Possum"
    assert chassis["meta"]["device_version"] == 38
    assert chassis["preset"]["flow"][0]["@enabled"] is True
    # Second path (just endpoints) survives unchanged
    assert chassis["preset"]["flow"][1] == _hsp_preset_for_chassis()["preset"]["flow"][1]


def test_extract_chassis_from_hsp_marks_format():
    """The chassis must carry a shape marker so generate can tell .hsp from .hlx."""
    from helixgen.chassis import extract_chassis_from_hsp
    chassis = extract_chassis_from_hsp(_hsp_preset_for_chassis())
    assert chassis.get("_helixgen_chassis_shape") == "hsp"


def test_extract_chassis_from_hsp_does_not_mutate_input():
    from helixgen.chassis import extract_chassis_from_hsp
    source = _hsp_preset_for_chassis()
    snapshot = json.loads(json.dumps(source))
    extract_chassis_from_hsp(source)
    assert source == snapshot
