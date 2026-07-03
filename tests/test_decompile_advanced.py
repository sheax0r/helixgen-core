import json
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


def test_ir_default_hash_emits_no_ir_field(tmp_path, sample_serial_preset_hsp):
    """Decompiling an IR slot whose hash equals the block default emits no 'ir' key."""
    lib = _make_ir_library(tmp_path, sample_serial_preset_hsp)
    body = _make_ir_body(_DEFAULT_IRHASH)
    empty_irs = IrMapping(irs_dir=tmp_path / "irs")
    d = decompile_body(body, lib, irs=empty_irs)
    block_entry = d["paths"][0]["blocks"][0]
    assert "ir" not in block_entry


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
