import json
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.decompile import decompile_body


def _roundtrip(spec_dict, lib, strip):
    p1 = compose_preset(parse_spec(spec_dict), lib, source="t")
    spec2 = parse_spec(decompile_body(p1, lib))
    p2 = compose_preset(spec2, lib, source="t")
    return strip(p1), strip(p2)


def test_snapshots_roundtrip_stable(hsp_library, strip_provenance):
    lib = hsp_library
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Brit Amp"}]}],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"],
             "params": {"Brit Amp": {"Drive": 0.9}}}]}
    p1, p2 = _roundtrip(spec, lib, strip_provenance)
    assert p1 == p2
    d = decompile_body(compose_preset(parse_spec(spec), lib, source="t"), lib)
    names = [s["name"] for s in d["snapshots"]]
    assert names[:2] == ["Rhythm", "Lead"]


def test_footswitch_roundtrip_stable(hsp_library, strip_provenance):
    lib = hsp_library
    spec = {"name": "F", "paths": [{"blocks": [{"block": "Tube Drive"}]}],
            "footswitches": [{"switch": "FS3", "block": "Tube Drive",
                              "behavior": "momentary"}]}
    p1, p2 = _roundtrip(spec, lib, strip_provenance)
    assert p1 == p2


def test_expression_roundtrip_stable(hsp_library, strip_provenance):
    lib = hsp_library
    spec = {"name": "E", "paths": [{"blocks": [{"block": "Brit Amp"}]}],
            "expression": [{"pedal": "EXP1", "targets": [
                {"block": "Brit Amp", "param": "Master", "min": 0.1, "max": 0.8}]}]}
    p1, p2 = _roundtrip(spec, lib, strip_provenance)
    assert p1 == p2
