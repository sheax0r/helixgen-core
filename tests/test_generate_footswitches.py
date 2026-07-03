"""Round-trip tests: spec footswitches → controller block on @enabled + preset.sources."""
from pathlib import Path

import json
import pytest

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC
from helixgen.ingest import ingest_path
from helixgen.library import Block, Library
from helixgen.spec import parse_spec

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _dup_ir_lib(tmp_path, sample_serial_preset_hsp):
    chassis = tmp_path / "c.hsp"
    chassis.write_bytes(HSP_MAGIC + json.dumps(sample_serial_preset_hsp).encode())
    lib = Library(root=tmp_path / "lib")
    ingest_path(chassis, lib)
    lib.save_block(Block(model_id="HX2_ImpulseResponseWithPan", category="cab",
        display_name="With Pan", params={"Mix": {"type": "float"}},
        exemplar={"@model": "HX2_ImpulseResponseWithPan", "@type": "cab", "@enabled": True, "Mix": 1.0},
        first_seen={"preset": "_", "firmware": "_", "date": "x"}, default_irhash="a"*32))
    return lib


def test_footswitch_targets_duplicate_block_by_coordinate(tmp_path, sample_serial_preset_hsp):
    lib = _dup_ir_lib(tmp_path, sample_serial_preset_hsp)
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 1},
        {"block": "With Pan", "ir": "a"*32, "lane": 0, "pos": 2}]}],
        "footswitches": [{"switch": "FS1", "block": "With Pan", "pos": 2}]})
    preset = compose_preset(spec, lib, source="t")
    # the FS controller must be attached to the pos-2 slot (b02), not b01
    assert "controller" in preset["preset"]["flow"][0]["b02"]["@enabled"]
    assert "controller" not in preset["preset"]["flow"][0]["b01"]["@enabled"]


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _drive_block_name(library: Library) -> str:
    """Return the display_name of the first drive block in the library."""
    drive_blocks = [b for b in library.list_blocks() if b.category == "drive"]
    if not drive_blocks:
        pytest.skip("No drive blocks in library; cannot build FS test spec.")
    return drive_blocks[0].display_name


def _build_spec(drive_name: str, **extra):
    """Build a parsed Spec with one drive block on path 0, plus extras."""
    from helixgen.spec import parse_spec
    return parse_spec({
        "name": "fs-test",
        "paths": [{"input": "inst1", "blocks": [{"block": drive_name}]}],
        **extra,
    })


def _b01_enabled(preset):
    return preset["preset"]["flow"][0]["b01"]["@enabled"]


def test_fs_assigned_block_gets_controller_on_enabled(tmp_path):
    library = _library(tmp_path)
    drive_name = _drive_block_name(library)
    spec = _build_spec(drive_name, footswitches=[
        {"switch": "FS3", "block": drive_name},
    ])
    preset = compose_preset(spec, library, source="test")
    enabled = _b01_enabled(preset)
    assert "controller" in enabled
    ctrl = enabled["controller"]
    assert ctrl["type"] == "targetbypass"
    assert ctrl["behavior"] == "latching"
    assert ctrl["source"] == 0x01010102  # FS3
    assert ctrl["min"] is None and ctrl["max"] is None


def test_fs_momentary_behavior_propagates_to_controller(tmp_path):
    library = _library(tmp_path)
    drive_name = _drive_block_name(library)
    spec = _build_spec(drive_name, footswitches=[
        {"switch": "FS4", "block": drive_name, "behavior": "momentary"},
    ])
    preset = compose_preset(spec, library, source="test")
    assert _b01_enabled(preset)["controller"]["behavior"] == "momentary"


def test_fs_source_id_added_to_preset_sources(tmp_path):
    library = _library(tmp_path)
    drive_name = _drive_block_name(library)
    spec = _build_spec(drive_name, footswitches=[
        {"switch": "FS5", "block": drive_name},
    ])
    preset = compose_preset(spec, library, source="test")
    sources = preset["preset"]["sources"]
    # The implementation chooses string-int keys; whichever form is used, the
    # entry value should be {"bypass": false}.
    key_used = next(k for k in sources if int(k) == 0x01010104)
    assert sources[key_used]["bypass"] is False


def test_no_fs_means_no_controller_wrap(tmp_path):
    library = _library(tmp_path)
    drive_name = _drive_block_name(library)
    spec = _build_spec(drive_name, footswitches=[])
    preset = compose_preset(spec, library, source="test")
    enabled = _b01_enabled(preset)
    assert "controller" not in enabled


def test_fs_with_snapshot_disable_composes(tmp_path):
    """A block that has both an FS assignment and a snapshot-disable should
    get both: @enabled wrapper carries 'snapshots' AND 'controller'."""
    library = _library(tmp_path)
    drive_name = _drive_block_name(library)
    spec = _build_spec(drive_name,
        snapshots=[
            {"name": "A"},
            {"name": "B", "disable": [drive_name]},
        ],
        footswitches=[{"switch": "FS3", "block": drive_name}],
    )
    preset = compose_preset(spec, library, source="test")
    enabled = _b01_enabled(preset)
    assert "controller" in enabled
    assert "snapshots" in enabled
    assert enabled["snapshots"][1] is False
