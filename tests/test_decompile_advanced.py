import json
from pathlib import Path

import pytest
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from helixgen.decompile import decompile_body
from helixgen.ir import IR_MODEL_PREFIX, IrMapping


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


def test_snapshot_decompile_filters_phantom_dense_overrides(hsp_library):
    """Task 1's densify fills every non-diverging snapshot slot with the base
    value (instead of leaving it null). The recovered spec must not turn those
    fills into spurious per-snapshot param overrides -- only the one real
    override on "Lead" should survive, and "Rhythm" (which diverges nowhere)
    must carry no "params" key at all."""
    lib = hsp_library
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Brit Amp"}]}],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"],
             "params": {"Brit Amp": {"Drive": 0.9}}}]}
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    d = decompile_body(p1, lib)
    snaps = {s["name"]: s for s in d["snapshots"]}
    assert "params" not in snaps["Rhythm"]
    assert snaps["Lead"]["params"] == {"Brit Amp": {"Drive": 0.9}}


def _dup_tube_drive_split_spec(snapshots):
    """A path with "Tube Drive" placed twice (ambiguous display_name): once
    in lane 0 and once in lane 1 via a split, so snapshot refs to it must
    disambiguate with lane/pos."""
    return {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 1},
        {"split": {"model": "P35_AppDSPSplitY", "params": {}}, "lane": 0, "pos": 2},
        {"block": "Tube Drive", "lane": 1, "pos": 1},
        {"join": {}, "lane": 0, "pos": 3},
    ]}], "snapshots": snapshots}


def test_snapshot_decompile_emits_coordinates_when_ambiguous(hsp_library, strip_provenance):
    lib = hsp_library
    spec = _dup_tube_drive_split_spec([
        {"name": "Rhythm"},
        {"name": "Lead",
         "disable": [{"block": "Tube Drive", "lane": 0, "pos": 1}],
         "params": [{"block": "Tube Drive", "lane": 1, "pos": 1, "params": {"Gain": 0.9}}]},
    ])
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    d = decompile_body(p1, lib)
    snap = d["snapshots"][1]
    # ambiguous name -> list form with coordinates, not a bare dict
    assert isinstance(snap.get("params"), list)
    assert all("lane" in e and "pos" in e for e in snap["params"])
    assert isinstance(snap.get("disable"), list)
    assert all(isinstance(e, dict) and "lane" in e and "pos" in e for e in snap["disable"])
    parse_spec(d)  # must round-trip through the parser
    # And the regenerated preset must match the source (coordinates resolved
    # back to the right physical block).
    p2 = compose_preset(parse_spec(d), lib, source="t")
    assert strip_provenance(p1) == strip_provenance(p2)


def test_snapshot_decompile_stays_dict_when_unambiguous(hsp_library):
    lib = hsp_library
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Brit Amp"}]}],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"],
             "params": {"Brit Amp": {"Drive": 0.9}}}]}
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    d = decompile_body(p1, lib)
    # unambiguous -> current dict form preserved (backward compatible)
    assert isinstance(d["snapshots"][1].get("params"), dict)
    assert d["snapshots"][1]["disable"] == ["Tube Drive"]


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


def test_expression_recovery_skips_bool_and_non_exp(hsp_library, capsys, tmp_path):
    """FS-as-parameter controllers (e.g. FS9) and bool-typed min/max sweeps
    are out of v1 scope (v1 expression is EXP1/EXP2, numeric non-bool
    min/max only) -- mirrors data/BAS_Goliathan.hsp's "Ch1 Boost" controller
    (source 0x01010108 == FS9, min=False, max=True). _recover_expression must
    skip such controllers with a stderr warning rather than emit a spec that
    parse_spec rejects.
    """
    lib = hsp_library

    spec = {
        "name": "Mixed",
        "paths": [{"blocks": [{"block": "Brit Amp"}, {"block": "Tube Drive"}]}],
        "expression": [{"pedal": "EXP2", "targets": [
            {"block": "Brit Amp", "param": "Master", "min": 0.1, "max": 0.8}]}],
    }
    preset = compose_preset(parse_spec(spec), lib, source="t")

    # Inject a footswitch-as-parameter (FS9) bool sweep directly onto Tube
    # Drive's Gain param -- this shape cannot be produced via the spec model
    # (v1 has no such construct) so it's built by hand, as real device
    # exports carry it.
    found = False
    for path in preset["preset"]["flow"]:
        for key, bnn in path.items():
            if key.startswith("@"):
                continue
            slot = bnn.get("slot", [{}])[0]
            if slot.get("model") == "HD2_DistTube":
                gain = slot["params"]["Gain"]
                gain["controller"] = {
                    "behavior": "latching", "bypassed": True, "curve": "linear",
                    "delay": 0, "goid": 0, "max": True, "midisource": 0,
                    "min": False, "source": 0x01010108, "threshold": 0.0,
                    "type": "param",
                }
                found = True
    assert found, "Tube Drive slot not found in composed preset"

    spec2 = decompile_body(preset, lib)

    for a in spec2.get("expression", []):
        assert a["pedal"] in ("EXP1", "EXP2")
        for t in a["targets"]:
            assert not isinstance(t["min"], bool) and isinstance(t["min"], (int, float))
            assert not isinstance(t["max"], bool) and isinstance(t["max"], (int, float))

    parse_spec(spec2)  # must parse -- the whole point of the filter

    # the valid EXP2 sweep is still recovered
    exp2 = [a for a in spec2["expression"] if a["pedal"] == "EXP2"]
    assert exp2 and exp2[0]["targets"][0]["param"] == "Master"

    err = capsys.readouterr().err
    assert "warning:" in err

    # Skip-if-absent real-export integration check. BAS_Goliathan.hsp is a
    # real device export (not a repo fixture, gitignored under data/) that
    # empirically carries exactly this shape: source 0x01020101 (EXP2) on a
    # float pedal-position param, plus source 0x01010108/0x01010109
    # (FS9/FS10) bool sweeps on "Ch1 Boost" / "Ch2 Boost".
    real = Path(__file__).parent.parent / "data" / "BAS_Goliathan.hsp"
    if real.exists():
        from helixgen.hsp import read_hsp
        from helixgen.ingest import ingest_path
        from helixgen.library import Library

        real_lib = Library(root=tmp_path / "real_lib")
        ingest_path(real, real_lib)
        real_body = read_hsp(real)
        real_spec = decompile_body(real_body, real_lib)
        for a in real_spec.get("expression", []):
            assert a["pedal"] in ("EXP1", "EXP2")
            for t in a["targets"]:
                assert not isinstance(t["min"], bool) and isinstance(t["min"], (int, float))
                assert not isinstance(t["max"], bool) and isinstance(t["max"], (int, float))
        parse_spec(real_spec)


def test_refs_never_emit_empty_block_name_when_display_name_blank(hsp_library):
    """Library blocks whose display_name is "" (empirically observed in some
    real exports) must never surface as an empty "block" reference in
    footswitches/expression/snapshots -- fall back to model_id instead,
    mirroring _block_entry. Otherwise parse_spec rejects the recovered spec
    with '"block" must be a non-empty string'.

    Both placed blocks are blanked (not just one) so the blank name is
    ambiguous in the library the same way real exports exhibit it -- this
    keeps the test isolated to the footswitch/expression/snapshot recovery
    paths (which, pre-fix, emit the raw display_name with no ambiguity
    check at all) rather than incidentally exercising the unrelated
    self-match short-circuit `_block_entry`'s own resolver takes when a
    blank name happens to be unique."""
    from helixgen.library import Block

    lib = hsp_library
    spec = {
        "name": "F",
        "paths": [{"blocks": [{"block": "Tube Drive"}, {"block": "Brit Amp"}]}],
        "footswitches": [{"switch": "FS3", "block": "Tube Drive"}],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "Brit Amp", "param": "Master"}]}],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"]},
        ],
    }
    p1 = compose_preset(parse_spec(spec), lib, source="t")

    # Blank the display_name of BOTH placed blocks in the library *after*
    # composing -- model_id references inside the composed preset still
    # resolve, but the blocks' display_names are now "" the way some real
    # exports carry them.
    for model_id in ("HD2_DistTube", "HD2_AmpBrit"):
        orig = lib.load_block(model_id)
        lib.save_block(Block(
            model_id=orig.model_id, category=orig.category, display_name="",
            params=orig.params, exemplar=orig.exemplar, first_seen=orig.first_seen))

    d = decompile_body(p1, lib)

    for fs in d.get("footswitches", []):
        assert fs["block"], f"empty footswitch block ref: {fs!r}"
    for exp in d.get("expression", []):
        for t in exp["targets"]:
            assert t["block"], f"empty expression target block ref: {t!r}"
    for snap in d.get("snapshots", []):
        for dis in snap.get("disable", []):
            name = dis if isinstance(dis, str) else dis.get("block")
            assert name, f"empty snapshot disable block ref: {dis!r}"

    parse_spec(d)  # must parse -- this is the real failure mode being fixed


# ---------------------------------------------------------------------------
# FIX 2 — pin orphan-IR-hash decompile behavior
# ---------------------------------------------------------------------------

_DEFAULT_IRHASH = "a" * 32
_ORPHAN_IRHASH  = "b" * 32  # 32-hex, not registered, not the default


def _make_ir_body(irhash: str) -> dict:
    """Minimal .hsp body with one path containing a single IR block."""
    return {
        "meta": {"name": "IR Test", "color": "auto", "device_id": 2490368,
                 "device_version": 0, "info": ""},
        "preset": {
            "clip": {"end": 0.0, "filename": "", "path": "", "start": 0.0},
            "cursor": {"flow": 0, "path": 0, "position": 0},
            "flow": [
                {
                    "@enabled": True,
                    "b00": {"type": "input", "position": 0, "path": 0,
                            "slot": [{"model": "P35_InputInst1", "params": {}, "version": 0}]},
                    "b01": {"type": "cab", "position": 1, "path": 0,
                            "slot": [{"model": f"{IR_MODEL_PREFIX}Mono",
                                      "@enabled": True, "params": {}, "version": 0,
                                      "irhash": irhash}]},
                    "b13": {"type": "output", "position": 13, "path": 0,
                            "slot": [{"model": "P35_OutputMatrix", "params": {}, "version": 0}]},
                },
            ],
        },
    }


def _make_ir_library(tmp_path, sample_serial_preset_hsp):
    """Library with an IR block registered (model_id HX2_ImpulseResponseMono)."""
    from helixgen.hsp import HSP_MAGIC
    from helixgen.ingest import ingest_path
    from helixgen.library import Block, Library

    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(
        model_id=f"{IR_MODEL_PREFIX}Mono",
        category="cab",
        display_name="IR Mono",
        params={},
        exemplar={"@model": f"{IR_MODEL_PREFIX}Mono", "@type": "cab",
                  "@enabled": True, "params": {}},
        first_seen={"preset": "_", "firmware": "_", "date": "x"},
        default_irhash=_DEFAULT_IRHASH,
    ))
    return lib


def test_ir_default_hash_always_emits_ir_field(tmp_path, sample_serial_preset_hsp):
    """FIX B: Decompiling an IR slot always emits 'ir', even when hash == block default."""
    lib = _make_ir_library(tmp_path, sample_serial_preset_hsp)
    body = _make_ir_body(_DEFAULT_IRHASH)
    empty_irs = IrMapping(irs_dir=tmp_path / "irs")
    d = decompile_body(body, lib, irs=empty_irs)
    block_entry = d["paths"][0]["blocks"][0]
    # Always emit — even when the hash matches the library block's default_irhash.
    assert block_entry.get("ir") == _DEFAULT_IRHASH


def test_ir_block_no_default_hash_emits_raw_hash(tmp_path, sample_serial_preset_hsp):
    """FIX B: IR block with default_irhash=None and unregistered slot irhash emits the raw hash."""
    from helixgen.hsp import HSP_MAGIC
    from helixgen.ingest import ingest_path
    from helixgen.library import Block, Library

    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + __import__("json").dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib2")
    ingest_path(chassis, lib)
    _UNREGISTERED = "c" * 32
    lib.save_block(Block(
        model_id=f"{IR_MODEL_PREFIX}WithPan",
        category="cab",
        display_name="With Pan",
        params={},
        exemplar={"@model": f"{IR_MODEL_PREFIX}WithPan", "@type": "cab",
                  "@enabled": True, "params": {}},
        first_seen={"preset": "_", "firmware": "_", "date": "x"},
        default_irhash=None,  # no default — the always-emit path must handle this
    ))
    body = {
        "meta": {"name": "IR Test", "color": "auto", "device_id": 2490368,
                 "device_version": 0, "info": ""},
        "preset": {
            "clip": {"end": 0.0, "filename": "", "path": "", "start": 0.0},
            "cursor": {"flow": 0, "path": 0, "position": 0},
            "flow": [{
                "@enabled": True,
                "b00": {"type": "input", "position": 0, "path": 0,
                        "slot": [{"model": "P35_InputInst1", "params": {}, "version": 0}]},
                "b01": {"type": "cab", "position": 1, "path": 0,
                        "slot": [{"model": f"{IR_MODEL_PREFIX}WithPan",
                                  "@enabled": True, "params": {}, "version": 0,
                                  "irhash": _UNREGISTERED}]},
                "b13": {"type": "output", "position": 13, "path": 0,
                        "slot": [{"model": "P35_OutputMatrix", "params": {}, "version": 0}]},
            }],
        },
    }
    empty_irs = IrMapping(irs_dir=tmp_path / "irs")
    d = decompile_body(body, lib, irs=empty_irs)
    block_entry = d["paths"][0]["blocks"][0]
    assert block_entry.get("ir") == _UNREGISTERED
    # Round-trip must not raise — unregistered hex hash passes through with a warning.
    compose_preset(parse_spec(d), lib, source="t", irs=empty_irs)


def test_ir_orphan_hash_emits_raw_hash(tmp_path, sample_serial_preset_hsp):
    """Decompiling an IR slot with an unregistered, non-default hash emits the raw hash."""
    lib = _make_ir_library(tmp_path, sample_serial_preset_hsp)
    body = _make_ir_body(_ORPHAN_IRHASH)
    empty_irs = IrMapping(irs_dir=tmp_path / "irs")
    d = decompile_body(body, lib, irs=empty_irs)
    block_entry = d["paths"][0]["blocks"][0]
    assert block_entry.get("ir") == _ORPHAN_IRHASH


def test_ir_orphan_hash_regenerate_passthrough(tmp_path, sample_serial_preset_hsp, capsys):
    """Regenerating a spec that carries an unregistered IR hash passes it through with a warning."""
    from helixgen.generate import compose_preset
    lib = _make_ir_library(tmp_path, sample_serial_preset_hsp)
    body = _make_ir_body(_ORPHAN_IRHASH)
    empty_irs = IrMapping(irs_dir=tmp_path / "irs")
    d = decompile_body(body, lib, irs=empty_irs)
    # Must NOT raise — orphan hash is passed through unchanged
    compose_preset(parse_spec(d), lib, source="t", irs=empty_irs)
    assert "warning" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Task 6 — IR block with no assigned IR round-trips via a `no_ir` marker
# ---------------------------------------------------------------------------

def _make_ir_body_no_hash() -> dict:
    """Minimal .hsp body with one path containing a single IR block that has
    NO irhash key at all (device slot with no IR loaded)."""
    body = _make_ir_body("placeholder")
    del body["preset"]["flow"][0]["b01"]["slot"][0]["irhash"]
    return body


def test_decompile_ir_without_irhash_sets_no_ir(tmp_path, sample_serial_preset_hsp):
    """An IR slot with no irhash at all must round-trip to no_ir=True, and
    must NOT emit an "ir" field."""
    lib = _make_ir_library(tmp_path, sample_serial_preset_hsp)
    body = _make_ir_body_no_hash()
    empty_irs = IrMapping(irs_dir=tmp_path / "irs")
    d = decompile_body(body, lib, irs=empty_irs)
    entry = d["paths"][0]["blocks"][0]
    assert entry.get("no_ir") is True
    assert "ir" not in entry
    # Must round-trip through the parser and regenerate without raising.
    compose_preset(parse_spec(d), lib, source="t", irs=empty_irs)


def test_real_export_a_like_supreme_now_roundtrips(tmp_path):
    """`A like supreme.hsp` carries an IR block with no irhash — previously
    the largest real-export round-trip failure bucket (Category 3). Skips if
    the personal data/ export isn't present (gitignored, not on a clean clone)."""
    from pathlib import Path
    from helixgen.hsp import read_hsp
    from helixgen.ingest import ingest_path
    from helixgen.library import Library

    data_dir = Path(__file__).resolve().parent.parent / "data"
    sample = data_dir / "A like supreme.hsp"
    if not sample.exists():
        pytest.skip(f"{sample} not present; skipping real-export integration check.")

    samples = sorted(data_dir.glob("*.hsp"))
    lib = Library(root=tmp_path / "lib")
    for s in samples:
        ingest_path(s, lib)
    irs = IrMapping.load()
    body = read_hsp(sample)
    spec = parse_spec(decompile_body(body, lib, irs=irs))
    compose_preset(spec, lib, source=str(sample), irs=irs)  # must not raise


# ---------------------------------------------------------------------------
# FIX 3 — combined-feature round-trip test
# ---------------------------------------------------------------------------

def test_combined_features_roundtrip_stable(hsp_library, strip_provenance):
    """All optional features at once: input=inst1, enabled=False, FS, EXP, snapshots."""
    lib = hsp_library
    spec = {
        "name": "Full",
        "paths": [
            {
                "input": "inst1",
                "blocks": [
                    {"block": "Tube Drive", "params": {"Gain": 0.6}, "enabled": False},
                    {"block": "Brit Amp",   "params": {"Drive": 0.7, "Master": 0.5}},
                ],
            }
        ],
        "footswitches": [
            {"switch": "FS1", "block": "Tube Drive", "behavior": "latching"},
        ],
        "expression": [
            {"pedal": "EXP1", "targets": [
                {"block": "Brit Amp", "param": "Master", "min": 0.0, "max": 0.7},
            ]},
        ],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"],
             "params": {"Brit Amp": {"Drive": 0.9}}},
        ],
    }
    p1, p2 = _roundtrip(spec, lib, strip_provenance)
    assert p1 == p2


def test_duplicate_block_footswitches_roundtrip(tmp_path, sample_serial_preset_hsp, strip_provenance):
    from tests.test_generate_footswitches import _dup_ir_lib  # reuse helper
    lib = _dup_ir_lib(tmp_path, sample_serial_preset_hsp)
    spec = {"name": "n", "paths": [{"blocks": [
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 1},
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 2}]}],
        "footswitches": [{"switch": "FS1", "block": "With Pan", "pos": 1},
                         {"switch": "FS2", "block": "With Pan", "pos": 2}]}
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    spec2 = parse_spec(decompile_body(p1, lib))
    p2 = compose_preset(spec2, lib, source="t")
    assert strip_provenance(p1) == strip_provenance(p2)


def test_split_roundtrip_stable(hsp_library, strip_provenance):
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 5},
        {"split": {"model": "P35_AppDSPSplitY", "params": {}}, "lane": 0, "pos": 6},
        {"block": "Brit Amp", "lane": 1, "pos": 1},
        {"join": {}, "lane": 0, "pos": 8}]}]}
    from helixgen.generate import compose_preset
    from helixgen.spec import parse_spec
    p1 = compose_preset(parse_spec(spec), hsp_library, source="t")
    spec2 = parse_spec(decompile_body(p1, hsp_library))
    p2 = compose_preset(spec2, hsp_library, source="t")
    assert strip_provenance(p1) == strip_provenance(p2)
