import json

from helixgen.generate import generate_preset
from helixgen.ingest import (
    DSP_BLOCK_KEY_PREFIX,
    DSP_CAB_KEY_PREFIX,
    block_from_raw,
    extract_blocks_from_preset,
    ingest_path,
)
from helixgen.library import Library


def test_roundtrip_serial_preset(tmp_library, sample_serial_preset, tmp_path):
    """Ingest a fixture preset, generate from a derived spec, re-ingest the
    output, and verify the library is structurally unchanged."""

    preset_path = tmp_path / "in.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)
    first_summary = ingest_path(preset_path, lib)
    assert first_summary.new == 5
    assert first_summary.chassis_extracted

    initial_blocks = sorted(b.model_id for b in lib.list_blocks())
    initial_chassis = lib.load_chassis()

    spec_blocks = []
    for raw in extract_blocks_from_preset(sample_serial_preset):
        block = block_from_raw(raw, {"preset": "_", "firmware": "_", "date": "2026-05-01"})
        params = {}
        for k, v in raw.items():
            if k.startswith("@"):
                continue
            params[k] = v
            break
        spec_blocks.append({"block": block.display_name, "params": params})

    spec = {"name": "Roundtrip", "paths": [{"blocks": spec_blocks}]}
    spec_path = tmp_path / "rt.json"
    spec_path.write_text(json.dumps(spec))

    out_path = tmp_path / "rt.hlx"
    generate_preset(spec_path, out_path, lib)
    assert out_path.exists()

    second_summary = ingest_path(out_path, lib)
    assert second_summary.matched == 5
    assert second_summary.new == 0
    assert second_summary.conflicted == 0

    assert sorted(b.model_id for b in lib.list_blocks()) == initial_blocks
    assert lib.load_chassis() == initial_chassis

    out_data = json.loads(out_path.read_text())
    in_models = [b["@model"] for b in extract_blocks_from_preset(sample_serial_preset)]
    out_models = [b["@model"] for b in extract_blocks_from_preset(out_data)]
    assert sorted(in_models) == sorted(out_models)


def test_roundtrip_real_possum_preset(tmp_library, tmp_path):
    """The strongest acceptance signal: round-trip a real .hlx export.

    Ingest tests/fixtures/presets/possum.hlx, derive a spec from each block,
    regenerate, and verify re-ingest produces no library deltas.
    """
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "presets" / "possum.hlx"
    preset = json.loads(fixture.read_text())

    lib = Library(tmp_library)
    first = ingest_path(fixture, lib)
    assert first.new == 4
    assert first.chassis_extracted

    initial_blocks = sorted(b.model_id for b in lib.list_blocks())

    spec_blocks = []
    for raw in extract_blocks_from_preset(preset):
        block = block_from_raw(raw, {"preset": "_", "firmware": "_", "date": "2026-05-01"})
        spec_blocks.append({"block": block.display_name})

    spec = {"name": "Roundtrip Possum", "paths": [{"blocks": spec_blocks}]}
    spec_path = tmp_path / "rt.json"
    spec_path.write_text(json.dumps(spec))

    out_path = tmp_path / "rt.hlx"
    generate_preset(spec_path, out_path, lib)

    second = ingest_path(out_path, lib)
    assert second.new == 0
    assert second.matched == 4
    assert second.conflicted == 0
    assert sorted(b.model_id for b in lib.list_blocks()) == initial_blocks

    out_data = json.loads(out_path.read_text())
    dsp0 = out_data["data"]["tone"]["dsp0"]
    block_keys = sorted(k for k in dsp0 if k.startswith(DSP_BLOCK_KEY_PREFIX) and k[len(DSP_BLOCK_KEY_PREFIX):].isdigit())
    cab_keys = sorted(k for k in dsp0 if k.startswith(DSP_CAB_KEY_PREFIX) and k[len(DSP_CAB_KEY_PREFIX):].isdigit())
    assert [dsp0[k]["@model"] for k in block_keys] == [
        "HD2_DistCompulsiveDrive",
        "HD2_AmpBrit2204",
        "HD2_VolPanVol",
    ]
    assert [dsp0[k]["@model"] for k in cab_keys] == ["HD2_Cab4x121960T75"]
    amp_slot = next(k for k in block_keys if dsp0[k]["@model"] == "HD2_AmpBrit2204")
    assert dsp0[amp_slot]["@cab"] == "cab0"
