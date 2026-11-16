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


# --------------------------------------------------------------------------
# Device-export-shaped cases (hgc-0c3 review): a real export saved while the
# blank/unnamed snapshot is ACTIVE carries each wrapper's `value` MIRRORING
# that active snapshot, not the base. `compose_preset` normalises
# activesnapshot->0 / value->snaps[0], so these shapes only arise from a
# device export — built here directly. The keep-decision must NOT trust
# `value`, or an active-on-blank delta is silently dropped.
# --------------------------------------------------------------------------

def _mirror_value_to_active(body, active):
    """Rewrite every snapshot wrapper's `value` to mirror snaps[active], as a
    device export saved on `active` does, and mark that snapshot active."""
    body["preset"].setdefault("params", {})["activesnapshot"] = active
    for path in body["preset"]["flow"]:
        if not isinstance(path, dict):
            continue
        for k, bnn in path.items():
            if not (k.startswith("b") and k[1:].isdigit() and isinstance(bnn, dict)):
                continue
            en = bnn.get("@enabled")
            if isinstance(en, dict) and isinstance(en.get("snapshots"), list):
                en["value"] = en["snapshots"][active]
            slot = (bnn.get("slot") or [{}])[0]
            for w in (slot.get("params") or {}).values():
                if isinstance(w, dict) and isinstance(w.get("snapshots"), list):
                    w["value"] = w["snapshots"][active]


def _device_shaped_body(lib, *, with_param):
    """[{A}, {blank, disable Tube Drive (+Brit Amp param)}, {C}] saved with the
    blank snapshot ACTIVE — the shape that defeats a `value`-based base."""
    snap1 = {"name": "B", "disable": ["Tube Drive"]}
    if with_param:
        snap1["params"] = {"Brit Amp": {"Master": 0.9}}
    spec = parse_spec({
        "name": "Dev", "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}},
            {"block": "Brit Amp", "params": {"Master": 0.6}},
        ]}],
        "snapshots": [{"name": "A"}, snap1, {"name": "C"}],
    })
    body = compose_preset(spec, lib, source="t")
    body["preset"]["snapshots"][1]["name"] = ""   # device leaves it blank
    _mirror_value_to_active(body, active=1)        # saved on the blank snapshot
    return body


def test_active_on_blank_disable_survives(hsp_library):
    """The confirmed defect: blank snapshot is ACTIVE and its only delta is a
    base-enabled block bypassed there. `@enabled.value` mirrors the active
    (False), so a value-based base would drop the whole scene."""
    lib = hsp_library
    d = view(_device_shaped_body(lib, with_param=False), lib)
    assert [s["name"] for s in (d.get("snapshots") or [])] == ["A", "", "C"]
    assert d["snapshots"][1].get("disable") == ["Tube Drive"]


def test_active_on_blank_disable_roundtrips(hsp_library):
    lib = hsp_library
    d1 = view(_device_shaped_body(lib, with_param=False), lib)
    d2 = view(compose_preset(parse_spec(d1), lib, source="t"), lib)
    assert d1 == d2
    assert d2["snapshots"][1].get("disable") == ["Tube Drive"]


def test_active_on_blank_param_override_survives_and_lands_right(hsp_library):
    """Param-only active-on-blank: `value` mirrors the active override, so a
    value-based base both drops the real override AND fabricates a phantom one
    on snapshot A (index 0). The override must land on the blank slot only."""
    lib = hsp_library
    d = view(_device_shaped_body(lib, with_param=True), lib)
    snaps = d.get("snapshots") or []
    assert [s["name"] for s in snaps] == ["A", "", "C"]
    # override lands on the blank slot (index 1), not on A or C
    assert snaps[1]["params"]["Brit Amp"]["Master"] == 0.9
    assert "params" not in snaps[0] and "params" not in snaps[2]
    # fixed point
    d2 = view(compose_preset(parse_spec(d), lib, source="t"), lib)
    assert d == d2


def test_base_bypassed_block_makes_no_phantom_scene(hsp_library):
    """Guard: a block bypassed in EVERY snapshot is a constant all-False array
    (no variance) and must not fabricate a blank scene from an unnamed slot."""
    lib = hsp_library
    spec = parse_spec({
        "name": "Byp", "paths": [{"blocks": [
            {"block": "Tube Drive", "enabled": False},   # base-bypassed
            {"block": "Brit Amp"},
        ]}],
        "snapshots": [{"name": "A"}],
    })
    body = compose_preset(spec, lib, source="t")
    # force a constant all-False @enabled.snapshots on the bypassed block and a
    # blank name on an otherwise-empty slot 1 (device-export residue)
    for path in body["preset"]["flow"]:
        if isinstance(path, dict):
            b01 = path.get("b01")
            if isinstance(b01, dict):
                b01["@enabled"] = {"value": False,
                                   "snapshots": [False] * 8}
    body["preset"]["snapshots"][1]["name"] = ""
    d = view(body, lib)
    # only the real named scene A projects; no phantom blank from slot 1
    assert [s["name"] for s in (d.get("snapshots") or [])] == ["A"]
