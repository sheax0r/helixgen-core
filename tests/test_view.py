"""Tests for the read-only `.hsp` -> recipe-shape projection (view.py).

view() is a straight port of decompile.decompile_body(): given an
already-parsed body dict, it returns the readable spec-shape projection. It
never reads or writes a file (no sidecar) -- unlike the old `decompile()`
entry point in decompile.py, which reads a path via read_hsp.
"""
import json
import os

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC, read_hsp, write_hsp
from helixgen.ir import IR_MODEL_PREFIX, IrMapping
from helixgen.library import Block, Library
from helixgen.spec import parse_spec
from helixgen.view import view


def _write_and_read(tmp_path, body, name="out.hsp"):
    """Round-trip a composed body through actual .hsp bytes on disk, so tests
    exercise view() against what hsp.read_hsp() really returns."""
    path = tmp_path / name
    write_hsp(path, body)
    return read_hsp(path)


def test_view_recovers_name_and_blocks(hsp_library, tmp_path):
    lib = hsp_library
    spec = parse_spec({"name": "Tone X", "author": "me", "paths": [
        {"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.7}}]}]})
    p1 = compose_preset(spec, lib, source="t")
    body = _write_and_read(tmp_path, p1)
    d = view(body, lib)
    assert d["name"] == "Tone X"
    assert d["author"] == "me"
    assert d["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert d["paths"][0]["blocks"][0]["params"] == {"Gain": 0.7}


def test_view_recovers_snapshots(hsp_library, tmp_path):
    lib = hsp_library
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Brit Amp"}]}],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": ["Tube Drive"],
             "params": {"Brit Amp": {"Drive": 0.9}}}]}
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    body = _write_and_read(tmp_path, p1)
    d = view(body, lib)
    names = [s["name"] for s in d["snapshots"]]
    assert names[:2] == ["Rhythm", "Lead"]
    assert d["snapshots"][1]["disable"] == ["Tube Drive"]
    assert d["snapshots"][1]["params"] == {"Brit Amp": {"Drive": 0.9}}


def test_view_recovers_footswitches_and_expression(hsp_library, tmp_path):
    lib = hsp_library
    spec = {
        "name": "Full",
        "paths": [{"blocks": [
            {"block": "Tube Drive"},
            {"block": "Brit Amp"},
        ]}],
        "footswitches": [
            {"switch": "FS1", "block": "Tube Drive", "behavior": "latching"},
        ],
        "expression": [
            {"pedal": "EXP1", "targets": [
                {"block": "Brit Amp", "param": "Master", "min": 0.0, "max": 0.7},
            ]},
        ],
    }
    p1 = compose_preset(parse_spec(spec), lib, source="t")
    body = _write_and_read(tmp_path, p1)
    d = view(body, lib)
    assert d["footswitches"] == [
        {"switch": "FS1", "block": "Tube Drive", "behavior": "latching"}]
    assert d["expression"] == [
        {"pedal": "EXP1", "targets": [
            {"block": "Brit Amp", "param": "Master", "min": 0.0, "max": 0.7}]}]


def _make_ir_library(tmp_path, sample_serial_preset_hsp):
    chassis = tmp_path / "chassis.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    from helixgen.ingest import ingest_path
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
        default_irhash="a" * 32,
    ))
    return lib


def test_view_recovers_ir_basename(tmp_path, sample_serial_preset_hsp):
    lib = _make_ir_library(tmp_path, sample_serial_preset_hsp)
    irhash = "a" * 32
    irs = IrMapping(irs_dir=tmp_path / "irs")
    irs.entries[irhash] = "/wavs/MyCab.wav"
    body = {
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
    body = _write_and_read(tmp_path, body)
    d = view(body, lib, irs=irs)
    entry = d["paths"][0]["blocks"][0]
    assert entry["block"] == "IR Mono"
    assert entry["ir"] == "MyCab.wav"


def test_view_does_not_create_any_file(hsp_library, tmp_path):
    lib = hsp_library
    spec = parse_spec({"name": "NoWrite", "paths": [
        {"blocks": [{"block": "Tube Drive"}]}]})
    p1 = compose_preset(spec, lib, source="t")
    body = _write_and_read(tmp_path, p1, name="src.hsp")
    before = set(os.listdir(tmp_path))
    view(body, lib)
    after = set(os.listdir(tmp_path))
    assert before == after
