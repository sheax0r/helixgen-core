import json

from helixgen.generate import generate_preset
from helixgen.ingest import block_from_raw, ingest_path
from helixgen.library import Library


def test_roundtrip_serial_preset(tmp_library, sample_serial_preset, tmp_path):
    """Ingest a fixture preset, generate from a derived spec, re-ingest the
    output, and verify the library is structurally unchanged."""

    # 1. Ingest fixture preset
    preset_path = tmp_path / "in.hlx"
    preset_path.write_text(json.dumps(sample_serial_preset))
    lib = Library(tmp_library)
    first_summary = ingest_path(preset_path, lib)
    assert first_summary.new == 5
    assert first_summary.chassis_extracted

    initial_blocks = sorted(b.model_id for b in lib.list_blocks())
    initial_chassis = lib.load_chassis()

    # 2. Derive a spec from the original preset (using each block's display name)
    spec_blocks = []
    for raw in sample_serial_preset["data"]["tone"]["dsp0"]["blocks"].values():
        block = block_from_raw(raw, {"preset": "_", "firmware": "_", "date": "2026-05-01"})
        # Pull just one param verbatim to verify overrides round-trip
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

    # 3. Generate
    out_path = tmp_path / "rt.hlx"
    generate_preset(spec_path, out_path, lib)
    assert out_path.exists()

    # 4. Re-ingest the generated file; library should be unchanged
    second_summary = ingest_path(out_path, lib)
    assert second_summary.matched == 5
    assert second_summary.new == 0
    assert second_summary.conflicted == 0

    assert sorted(b.model_id for b in lib.list_blocks()) == initial_blocks
    assert lib.load_chassis() == initial_chassis

    # 5. Generated preset structurally equivalent: same model_ids in same dsp0 slot order
    out_data = json.loads(out_path.read_text())
    in_blocks = sample_serial_preset["data"]["tone"]["dsp0"]["blocks"]
    out_blocks = out_data["data"]["tone"]["dsp0"]["blocks"]
    in_models = [b["@model"] for b in in_blocks.values()]
    out_models = [out_blocks[k]["@model"] for k in sorted(out_blocks.keys())]
    assert in_models == out_models
