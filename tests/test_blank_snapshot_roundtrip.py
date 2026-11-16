"""hgc-0c3: a VALID snapshot whose device name is blank still carries real
per-snapshot deltas. Since hgc-dym (#121) untranscode emits that blank name
verbatim, and since #118 view skipped every blank/placeholder-named slot, the
`device to-hsp -> view -> generate` re-author loop silently DROPPED the slot's
deltas. These are the acceptance gates: view must keep the slot's deltas, and
the whole loop must reach a fixed point, without fabricating phantom scenes.
"""
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.view import view


def _blanked_body(lib):
    """Build [{A}, {B, disable Tube Drive}, {C}] then blank snapshot 1's name,
    exactly as a device export / untranscode leaves a valid unnamed snapshot."""
    spec = parse_spec({
        "name": "Blank", "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}},
            {"block": "Brit Amp", "params": {"Master": 0.6}},
        ]}],
        "snapshots": [
            {"name": "A"},
            {"name": "B", "disable": ["Tube Drive"]},
            {"name": "C"},
        ],
    })
    body = compose_preset(spec, lib, source="t")
    body["preset"]["snapshots"][1]["name"] = ""  # device leaves it blank
    return body


def test_blank_named_snapshot_keeps_its_deltas(hsp_library):
    """view must not drop a blank-named snapshot that disables a block."""
    lib = hsp_library
    d = view(_blanked_body(lib), lib)
    snaps = d.get("snapshots") or []
    # No phantom scenes, no lost scenes: still exactly A / <blank> / C.
    assert [s["name"] for s in snaps] == ["A", "", "C"]
    # The blank slot's disable survived.
    assert snaps[1].get("disable") == ["Tube Drive"]


def test_blank_named_snapshot_roundtrips_to_a_fixed_point(hsp_library):
    """view(generate(parse_spec(view(hsp)))) == view(hsp): the re-author loop
    is a true fixed point for a valid blank-named delta-carrying snapshot."""
    lib = hsp_library
    d1 = view(_blanked_body(lib), lib)
    d2 = view(compose_preset(parse_spec(d1), lib, source="t"), lib)
    assert d1 == d2
    # and the disable is still there after the round-trip (not baked away)
    assert d2["snapshots"][1].get("disable") == ["Tube Drive"]
