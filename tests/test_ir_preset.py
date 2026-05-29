"""Tests for extract_ir_hashes — reads slot-level irhash values from a .hsp body dict."""
from helixgen.ir import extract_ir_hashes


def make_ir_block(path: int, position: int, irhash: str) -> tuple[str, dict]:
    key = f"b{position:02d}_p{path}"
    return key, {
        "path": path,
        "position": position,
        "slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": irhash, "params": {}}],
    }


def make_input_block(key: str = "b00") -> tuple[str, dict]:
    return key, {
        "path": 0,
        "position": 0,
        "slot": [{"model": "P35_InputInst1", "params": {}}],
    }


def test_extract_ir_hashes_in_path_then_position_order():
    flow = {}
    flow.update([make_input_block("b00")])
    flow.update([make_ir_block(0, 2, "hash_p0_pos2")])
    flow.update([make_ir_block(0, 1, "hash_p0_pos1")])
    flow.update([make_ir_block(1, 1, "hash_p1_pos1")])

    preset = {"preset": {"flow": [flow]}}
    assert extract_ir_hashes(preset) == [
        "hash_p0_pos1",
        "hash_p0_pos2",
        "hash_p1_pos1",
    ]


def test_extract_ir_hashes_ignores_non_ir_blocks():
    flow = {}
    flow.update([make_input_block("b00")])
    flow.update([make_ir_block(0, 1, "ir_a")])
    flow["b02"] = {
        "path": 0,
        "position": 2,
        "slot": [{"model": "HD2_AmpBritPlexi", "params": {}}],
    }
    flow.update([make_ir_block(0, 3, "ir_b")])

    preset = {"preset": {"flow": [flow]}}
    assert extract_ir_hashes(preset) == ["ir_a", "ir_b"]


def test_extract_ir_hashes_empty_preset():
    assert extract_ir_hashes({"preset": {"flow": [{}]}}) == []
