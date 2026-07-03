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
        ("HX2_GateNoiseGateMono", "dynamics"),
        ("HX2_EQParametricMono", "eq"),
        ("HX2_AmpBrit2204Mono", "amp"),
        ("HX2_DistScream808Mono", "drive"),
        # Line 6 legacy stompbox modelers — DL4/DM4/FM4/MM4 prefixes
        ("HD2_DL4AnalogDelayStereo", "delay"),
        ("HD2_DL4TapeEchoStereo", "delay"),
        ("HD2_DL4LowResDelay", "delay"),
        ("HD2_DL4AutoVolStereo", "volume"),
        ("HD2_DM4ColorDrive", "drive"),
        ("HD2_DM4JumboFuzz", "drive"),
        ("HD2_DM4BassOctaver", "pitch"),
        ("HD2_FM4Growler", "filter"),
        ("HD2_FM4ObiWah", "filter"),
        ("HD2_FM4SynthOMatic", "filter"),
        ("HD2_FM4VoiceBox", "filter"),
        ("HD2_MM4AnalogChorus", "modulation"),
        ("HD2_MM4BarberpolePhaser", "modulation"),
        ("HD2_MM4PitchVibrato", "modulation"),
        # Stadium HX2_ comps
        ("HX2_DM4BlueComp", "dynamics"),
        ("HX2_DM4RedComp", "dynamics"),
        ("HX2_DM4TubeComp", "dynamics"),
        # Acoustic guitar simulators — shaping/filter family
        ("L6SPB_AcousGtrSimMono", "filter"),
        ("L6SPB_AcousGtrSimStereo", "filter"),
        # VIC plate reverb (despite the "Dyn" in the name, it's a reverb)
        ("VIC_DynPlateMono", "reverb"),
        ("VIC_DynPlateStereo", "reverb"),
        # Tape Eater is a saturation/drive effect
        ("TapeEater", "drive"),
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


def test_extract_schema_int_and_float(sample_cab_block):
    schema = extract_schema(sample_cab_block)
    assert schema["HighCut"]["type"] == "int"
    assert schema["HighCut"]["default"] == 8000
    assert schema["Distance"]["type"] == "int"
    assert schema["Distance"]["default"] == 3


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
        "HD2_DrvScream808",
        "HD2_AmpBrit2204Custom",
        "HD2_DlyDigital",
        "HD2_RvbPlate",
        "HD2_Cab4x12Greenback25",
    ]


def test_extract_blocks_from_preset_skips_infrastructure(sample_serial_preset):
    """inputA/outputA/split/join must never show up as user blocks."""
    blocks = extract_blocks_from_preset(sample_serial_preset)
    for b in blocks:
        assert not b["@model"].startswith("HD2_AppDSPFlow"), b["@model"]


def test_extract_blocks_from_preset_real_possum():
    """Possum.hlx → 3 user blocks + 1 cab = 4 catalogued items in dsp0."""
    import json as _json
    from pathlib import Path as _Path
    fixture = _Path(__file__).parent / "fixtures" / "presets" / "possum.hlx"
    preset = _json.loads(fixture.read_text())
    blocks = extract_blocks_from_preset(preset)
    model_ids = [b["@model"] for b in blocks]
    assert model_ids == [
        "HD2_DistCompulsiveDrive",
        "HD2_AmpBrit2204",
        "HD2_VolPanVol",
        "HD2_Cab4x121960T75",
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
    dsp0 = chassis["data"]["tone"]["dsp0"]
    assert not any(k.startswith("block") for k in dsp0)
    assert not any(k.startswith("cab") for k in dsp0)


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


def _hsp_bytes_for(payload: dict) -> bytes:
    from helixgen.hsp import HSP_MAGIC
    return HSP_MAGIC + json.dumps(payload).encode("utf-8")


def _minimal_hsp_payload() -> dict:
    return {
        "meta": {"name": "T", "device_version": 38},
        "preset": {
            "flow": [
                {
                    "b00": {"type": "input", "slot": [{"model": "P35_InputGuitar"}]},
                    "b01": {
                        "type": "fx",
                        "slot": [{
                            "model": "HD2_DrvScream808",
                            "params": {"Gain": {"value": 0.5}},
                        }],
                    },
                    "b13": {"type": "output", "slot": [{"model": "P35_OutputMain"}]},
                },
            ],
        },
    }


def test_ingest_hsp_extracts_chassis_on_first_encounter(tmp_library, tmp_path):
    hsp_path = tmp_path / "preset.hsp"
    hsp_path.write_bytes(_hsp_bytes_for(_minimal_hsp_payload()))
    lib = Library(tmp_library)

    assert not lib.has_chassis()
    summary = ingest_file(hsp_path, lib)

    assert summary.chassis_extracted is True
    assert lib.has_chassis()
    chassis = lib.load_chassis()
    assert chassis.get("_helixgen_chassis_shape") == "hsp"
    # User block b01 stripped; endpoints retained.
    path0 = chassis["preset"]["flow"][0]
    assert "b01" not in path0
    assert "b00" in path0 and "b13" in path0


def test_ingest_hsp_does_not_re_extract_chassis(tmp_library, tmp_path):
    hsp_path = tmp_path / "preset.hsp"
    hsp_path.write_bytes(_hsp_bytes_for(_minimal_hsp_payload()))
    lib = Library(tmp_library)

    ingest_file(hsp_path, lib)
    second = ingest_file(hsp_path, lib)
    assert second.chassis_extracted is False


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


def test_looper_is_catalogued(tmp_path, sample_serial_preset_hsp):
    from helixgen.hsp import HSP_MAGIC
    # inject a looper block into path 0
    body = json.loads(json.dumps(sample_serial_preset_hsp))
    body["preset"]["flow"][0]["b01"] = {
        "type": "fx", "position": 1, "path": 0,
        "slot": [{"model": "P35_LooperHelixStereo",
                  "@enabled": {"value": True}, "params": {}}],
    }
    p = tmp_path / "loop.hsp"
    p.write_bytes(HSP_MAGIC + json.dumps(body).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(p, lib)
    models = [b.model_id for b in lib.list_blocks()]
    assert "P35_LooperHelixStereo" in models
    assert infer_category("P35_LooperHelixStereo") == "looper"
