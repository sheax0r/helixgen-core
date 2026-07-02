import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.decompile import decompile_body


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
