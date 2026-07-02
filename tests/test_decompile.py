import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.decompile import decompile_body


def test_decompile_ambiguous_display_name_uses_model_id(tmp_path, sample_serial_preset_hsp, strip_provenance):
    """When two blocks share a display_name, decompile emits the model_id so the
    spec regenerates without hitting the LookupError raised by find_block."""
    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    # Two DRIVE blocks with the SAME display_name, different model_ids.
    for mid in ("HD2_DriveOne", "HD2_DriveTwo"):
        lib.save_block(Block(
            model_id=mid, category="drive", display_name="Twin Drive",
            params={"Gain": {"type": "float"}},
            exemplar={"@model": mid, "@type": "fx", "@enabled": True, "Gain": 0.5},
            first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    # Reference by model_id so generate can place it unambiguously.
    spec1 = parse_spec({"name": "Amb", "paths": [{"blocks": [
        {"block": "HD2_DriveOne", "params": {"Gain": 0.7}}]}]})
    p1 = compose_preset(spec1, lib, source="t")
    d = decompile_body(p1, lib)
    # Decompiled reference must be the model_id (display_name is ambiguous).
    assert d["paths"][0]["blocks"][0]["block"] == "HD2_DriveOne"
    # And it must regenerate cleanly (no LookupError).
    p2 = compose_preset(parse_spec(d), lib, source="t")
    assert strip_provenance(p2) == strip_provenance(p1)


def test_decompile_roundtrip_stable(hsp_library, strip_provenance):
    lib = hsp_library
    spec1 = parse_spec({
        "name": "RT", "author": "me",
        "paths": [{"input": "inst1", "blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.7}, "enabled": False},
            {"block": "Brit Amp",   "params": {"Drive": 0.8, "Master": 0.6}},
        ]}],
    })
    p1 = compose_preset(spec1, lib, source="t")
    spec2_dict = decompile_body(p1, lib)
    spec2 = parse_spec(spec2_dict)
    p2 = compose_preset(spec2, lib, source="t")
    assert strip_provenance(p2) == strip_provenance(p1)


def test_decompile_recovers_meta_and_blocks(hsp_library):
    lib = hsp_library
    spec1 = parse_spec({"name": "Tone X", "author": "me", "paths": [
        {"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.7}}]}]})
    p1 = compose_preset(spec1, lib, source="t")
    d = decompile_body(p1, lib)
    assert d["name"] == "Tone X"
    assert d["author"] == "me"
    assert d["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert d["paths"][0]["blocks"][0]["params"] == {"Gain": 0.7}
