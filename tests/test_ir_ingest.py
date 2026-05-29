"""Ingest captures the slot-level irhash on IR-block schemas."""
import json

import pytest

from helixgen.ingest import block_from_raw


def test_block_from_raw_captures_irhash():
    raw_slot = {
        "@model": "HX2_ImpulseResponseWithPan",
        "irhash": "ad8182e1ebe9fd95dffde5dd54b6d89c",
        "HighCut": 6500.0,
    }
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    block = block_from_raw(raw_slot, src)
    assert block.default_irhash == "ad8182e1ebe9fd95dffde5dd54b6d89c"


def test_block_from_raw_no_irhash_for_non_ir_block():
    raw_slot = {"@model": "HD2_AmpBritPlexi", "Drive": 0.7}
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    block = block_from_raw(raw_slot, src)
    assert block.default_irhash is None


def test_block_from_raw_does_not_leak_irhash_into_params():
    """irhash is slot-level metadata, not a tunable param. It must not appear in block.params."""
    raw = {
        "@model": "HX2_ImpulseResponseWithPan",
        "irhash": "ad8182e1ebe9fd95dffde5dd54b6d89c",
        "HighCut": 6500.0,
        "Mix": 1.0,
    }
    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    block = block_from_raw(raw, src)
    # Real tunable params still present
    assert "HighCut" in block.params
    assert "Mix" in block.params
    # Slot-level metadata excluded
    assert "irhash" not in block.params
    # But still captured at the top level
    assert block.default_irhash == "ad8182e1ebe9fd95dffde5dd54b6d89c"


def test_irhash_preserved_through_full_hsp_ingest_path(tmp_path):
    """The real ingest path (extract_blocks_from_hsp → block_from_raw) must preserve irhash."""
    from helixgen.hsp import HSP_MAGIC, extract_blocks_from_hsp

    # Minimal Stadium-shape preset with one IR block carrying a slot-level irhash
    preset = {
        "meta": {"name": "t", "color": "auto", "device_id": 2490368,
                 "device_version": 318833973, "info": ""},
        "preset": {
            "clip": {"end": 0.0, "filename": "", "path": "", "start": 0.0},
            "cursor": {"flow": 0, "path": 0, "position": 0},
            "flow": [{
                "b00": {"path": 0, "position": 0,
                        "slot": [{"model": "P35_InputInst1", "params": {}}]},
                "b01": {"path": 0, "position": 1,
                        "slot": [{
                            "model": "HX2_ImpulseResponseWithPan",
                            "irhash": "ad8182e1ebe9fd95dffde5dd54b6d89c",
                            "params": {"HighCut": {"value": 6500.0}},
                        }]},
            }],
        },
    }
    hsp_path = tmp_path / "x.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(preset).encode())

    src = {"preset": "x.hsp", "firmware": "t", "date": "2026-05-28"}
    raw_blocks = extract_blocks_from_hsp(json.loads(hsp_path.read_bytes()[len(HSP_MAGIC):]))
    ir_blocks = [b for b in raw_blocks if str(b.get("@model", "")).startswith("HX2_ImpulseResponse")]
    assert len(ir_blocks) == 1, f"expected 1 IR block, got {len(ir_blocks)}"

    block = block_from_raw(ir_blocks[0], src)
    assert block.default_irhash == "ad8182e1ebe9fd95dffde5dd54b6d89c", (
        f"canonical irhash dropped during real ingest path; got {block.default_irhash!r}"
    )
