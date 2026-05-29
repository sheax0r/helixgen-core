"""Ingest captures the slot-level irhash on IR-block schemas."""
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
