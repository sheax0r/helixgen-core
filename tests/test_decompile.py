import json
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.view import view


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
    d = view(p1, lib)
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
    spec2_dict = view(p1, lib)
    spec2 = parse_spec(spec2_dict)
    p2 = compose_preset(spec2, lib, source="t")
    assert strip_provenance(p2) == strip_provenance(p1)


def test_decompile_reads_base_bypass_from_bnn_level(hsp_library):
    """A block bypassed at the bNN level (slot level inert True) decompiles to
    enabled: false."""
    from helixgen.view import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    model_id = block.model_id  # ingest-time hsp model id round-trips via translate
    bnn = {
        "@enabled": {"value": False},                 # bNN: real bypass
        "type": "fx", "position": 1, "path": 0,
        "slot": [{"model": model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    assert entry["enabled"] is False


def test_decompile_recovers_meta_and_blocks(hsp_library):
    lib = hsp_library
    spec1 = parse_spec({"name": "Tone X", "author": "me", "paths": [
        {"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.7}}]}]})
    p1 = compose_preset(spec1, lib, source="t")
    d = view(p1, lib)
    assert d["name"] == "Tone X"
    assert d["author"] == "me"
    assert d["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert d["paths"][0]["blocks"][0]["params"] == {"Gain": 0.7}


def _endpoint_output(model, pos=13, path=0, endpoint="b00"):
    return {"@enabled": {"value": True}, "type": "output", "position": pos,
            "path": path, "endpoint": endpoint,
            "slot": [{"@enabled": {"value": True}, "model": model,
                      "params": {"gain": {"value": 0.0}, "pan": {"value": 0.5}}}]}


def _endpoint_input(model, pos=0, path=0, endpoint="b13"):
    return {"@enabled": {"value": True}, "type": "input", "position": pos,
            "path": path, "endpoint": endpoint,
            "slot": [{"@enabled": {"value": True}, "model": model, "params": {}}]}


def test_reconstruct_captures_branch_endpoints_no_keyerror(hsp_library):
    from helixgen.view import _reconstruct_path_blocks
    from helixgen.spec import StructuralEntry
    lib = hsp_library
    # A branch lane with an input endpoint (b14) and an output endpoint (b27)
    # that library.load_block cannot resolve — must NOT raise, must capture.
    path_dict = {
        "b00": _endpoint_input("P35_InputNone"),
        "b13": _endpoint_output("P35_OutputPath2A"),
        "b14": _endpoint_input("P35_InputNone", pos=0, path=1, endpoint="b01"),
        "b27": _endpoint_output("P35_OutputPath2B", pos=13, path=1, endpoint="b07"),
    }
    blocks = _reconstruct_path_blocks(path_dict, lib, None)
    structurals = [b for b in blocks if isinstance(b, StructuralEntry)]
    models = {f"b{14*b.lane+b.pos:02d}": b.raw["slot"][0]["model"] for b in structurals}
    # b00 is NOT captured (drives the `input` field); b13/b14/b27 are.
    assert models == {"b13": "P35_OutputPath2A", "b14": "P35_InputNone",
                      "b27": "P35_OutputPath2B"}


def test_reconstruct_orphaned_split_is_structural_balanced_is_semantic(hsp_library):
    from helixgen.view import _reconstruct_path_blocks
    from helixgen.spec import StructuralEntry, SplitEntry, JoinEntry
    lib = hsp_library
    # Orphaned split: endpoint points at an OUTPUT endpoint (b27), not a join.
    orphan = {
        "b00": _endpoint_input("P35_InputNone"),
        "b07": {"type": "split", "position": 7, "path": 0, "branch": "b15",
                "endpoint": "b27", "slot": [{"model": "P35_AppDSPSplitY", "params": {}}]},
        "b13": _endpoint_output("P35_OutputPath2A"),
        "b27": _endpoint_output("P35_OutputPath2B", pos=13, path=1, endpoint="b07"),
    }
    blocks = _reconstruct_path_blocks(orphan, lib, None)
    assert any(isinstance(b, StructuralEntry) and b.raw.get("type") == "split" for b in blocks)
    assert not any(isinstance(b, (SplitEntry, JoinEntry)) for b in blocks)


def test_structural_entry_survives_real_compose(hsp_library):
    # A spec carrying BOTH a balanced split (semantic) AND a verbatim
    # StructuralEntry (orphaned output endpoint) must compose without raising
    # and place the structural slot at its exact key.
    from helixgen.spec import parse_spec
    from helixgen.generate import compose_preset
    raw_out = {"@enabled": {"value": True}, "type": "output", "position": 13,
               "path": 1, "endpoint": "b07",
               "slot": [{"@enabled": {"value": True}, "model": "P35_OutputPath2B",
                         "params": {"gain": {"value": 0.0}}}]}
    spec = parse_spec({"name": "t", "paths": [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 1},
        {"split": {"model": "P35_AppDSPSplitY"}, "lane": 0, "pos": 2},
        {"block": "Tube Drive", "lane": 1, "pos": 3},
        {"join": {}, "lane": 0, "pos": 4},
        {"structural": raw_out, "lane": 1, "pos": 13},
    ]}]})
    body = compose_preset(spec, hsp_library, source="t")
    flow0 = body["preset"]["flow"][0]
    assert flow0["b27"]["slot"][0]["model"] == "P35_OutputPath2B"
    assert flow0["b27"] == raw_out


def test_decompile_captures_harness_and_extra_slots(hsp_library):
    from helixgen.view import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    bnn = {
        "@enabled": {"value": True},
        "type": "fx", "position": 1, "path": 0,
        "harness": {"@enabled": {"value": True},
                    "params": {"Trails": {"value": True}}},
        "slot": [
            {"model": block.model_id, "@enabled": {"value": True}, "params": {}},
            {"model": "HD2_CabMicIr_NoCab", "@enabled": {"value": True}, "params": {}},
        ],
    }
    entry = _block_entry(bnn, lib, None)
    assert entry["raw"]["harness"]["params"]["Trails"]["value"] is True
    assert entry["raw"]["slots"][0]["model"] == "HD2_CabMicIr_NoCab"


def _add_delay_block(lib):
    lib.save_block(Block(
        model_id="HD2_DelayTape", category="delay", display_name="Tape Delay",
        params={"Mix": {"type": "float"}},
        exemplar={"@model": "HD2_DelayTape", "@type": "fx", "@enabled": True,
                  "Mix": 0.3},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-07-08"}))
    lib.rebuild_index()
    return lib.find_block("Tape Delay")


def test_decompile_lifts_trails_on_delay(hsp_library):
    from helixgen.view import _block_entry
    lib = hsp_library
    block = _add_delay_block(lib)
    bnn = {
        "@enabled": {"value": True}, "type": "fx", "position": 1, "path": 0,
        "harness": {"@enabled": {"value": True},
                    "params": {"EvtIdx": {"value": -1},
                               "Trails": {"value": True},
                               "upper": {"value": True}}},
        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    # Trails lifted to a clean field...
    assert entry["trails"] is True
    # ...and removed from the verbatim harness (single source of truth)...
    assert "Trails" not in entry["raw"]["harness"]["params"]
    # ...while the other harness constants are retained verbatim.
    assert entry["raw"]["harness"]["params"]["upper"]["value"] is True
    assert entry["raw"]["harness"]["params"]["EvtIdx"]["value"] == -1


def test_decompile_does_not_lift_trails_on_non_delay_reverb(hsp_library):
    """Symmetric with the generate guard: a drive block's Trails is NOT lifted
    (it could not be regenerated as a `trails` field), so it stays verbatim."""
    from helixgen.view import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")  # category drive
    bnn = {
        "@enabled": {"value": True}, "type": "fx", "position": 1, "path": 0,
        "harness": {"@enabled": {"value": True},
                    "params": {"Trails": {"value": True}}},
        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    assert "trails" not in entry
    assert entry["raw"]["harness"]["params"]["Trails"]["value"] is True


def test_decompile_delay_without_trails_no_trails_field(hsp_library):
    from helixgen.view import _block_entry
    lib = hsp_library
    block = _add_delay_block(lib)
    bnn = {
        "@enabled": {"value": True}, "type": "fx", "position": 1, "path": 0,
        "harness": {"@enabled": {"value": True},
                    "params": {"upper": {"value": True}}},
        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    assert "trails" not in entry
    assert entry["raw"]["harness"]["params"]["upper"]["value"] is True


def test_decompile_generate_trails_roundtrip(hsp_library):
    """decompile -> parse -> generate reproduces the original harness dict."""
    from helixgen.view import _block_entry
    lib = hsp_library
    block = _add_delay_block(lib)
    harness = {"@enabled": {"value": True},
               "params": {"EvtIdx": {"value": -1},
                          "Trails": {"value": True},
                          "bypass": {"value": False},
                          "upper": {"value": True}}}
    bnn = {
        "@enabled": {"value": True}, "type": "fx", "position": 1, "path": 0,
        "harness": json.loads(json.dumps(harness)),
        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    spec = parse_spec({"name": "S", "paths": [{"blocks": [entry]}]}, source="t")
    preset = compose_preset(spec, lib, source="t")
    regen = preset["preset"]["flow"][0]["b01"]["harness"]
    assert regen == harness


def test_decompile_no_raw_when_no_harness_or_extra_slots(hsp_library):
    from helixgen.view import _block_entry
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    bnn = {
        "@enabled": {"value": True}, "type": "fx", "position": 1, "path": 0,
        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}],
    }
    entry = _block_entry(bnn, lib, None)
    assert "raw" not in entry


def test_decompile_warns_on_unrepresentable_enable(hsp_library, capsys):
    """base=False + enabled in a named snapshot + NO disable => can't round-trip;
    decompile must warn."""
    from helixgen.view import view
    lib = hsp_library
    block = lib.find_block("Tube Drive")
    body = {
        "meta": {"name": "T", "device_id": "stadium_xl"},
        "preset": {
            "snapshots": [{"name": "A"}, {"name": "B"}],  # 2 named
            "flow": [{
                "b00": {"@enabled": {"value": True}, "type": "input", "position": 0,
                        "path": 0, "endpoint": "b13",
                        "slot": [{"model": "P35_InputInst1", "@enabled": {"value": True}, "params": {}}]},
                "b01": {"@enabled": {"value": False,
                                     "snapshots": [True, True, None, None, None, None, None, None]},
                        "type": "fx", "position": 1, "path": 0,
                        "slot": [{"model": block.model_id, "@enabled": {"value": True}, "params": {}}]},
                "b13": {"@enabled": {"value": True}, "type": "output", "position": 13,
                        "path": 0, "endpoint": "b00",
                        "slot": [{"model": "P35_OutputPath2A", "@enabled": {"value": True}, "params": {}}]},
            }],
        },
    }
    view(body, lib, irs=None)
    err = capsys.readouterr().err
    assert "cannot round-trip" in err and "b01" in err
