"""Tests for the .hsp (Helix Stadium) reader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from helixgen.hsp import (
    HSP_MAGIC,
    extract_blocks_from_hsp,
    is_hsp_bytes,
    read_hsp,
    read_hsp_blocks,
)


def _make_hsp_bytes(payload: dict) -> bytes:
    return HSP_MAGIC + json.dumps(payload).encode("utf-8")


def _synthetic_hsp_payload() -> dict:
    """Minimal .hsp payload: 1 path with input + 2 user blocks + cab + output."""
    return {
        "meta": {"name": "Synthetic", "device_id": 0, "device_version": 0},
        "preset": {
            "flow": [
                {
                    "@enabled": True,
                    "b00": {  # input endpoint — must be skipped
                        "type": "input",
                        "position": 0,
                        "path": 0,
                        "slot": [{"model": "P35_InputGuitar", "params": {}, "version": 0}],
                    },
                    "b01": {
                        "type": "fx",
                        "position": 1,
                        "path": 0,
                        "slot": [{
                            "model": "HD2_DistScream808Mono",
                            "@enabled": {"value": True},
                            "params": {
                                "Gain": {"value": 0.4},
                                "Tone": {"value": 0.5},
                                "Level": {"value": 0.6},
                            },
                            "version": 0,
                        }],
                    },
                    "b02": {
                        "type": "amp",
                        "position": 3,
                        "path": 0,
                        "slot": [{
                            "model": "HD2_AmpBrit2204",
                            "@enabled": {"value": True},
                            "params": {
                                "Drive": {"value": 0.62},
                                "Master": {
                                    "controller": {"min": 0, "max": 1},
                                    "value": 0.36,
                                },
                            },
                            "version": 0,
                        }],
                    },
                    "b03": {
                        "type": "cab",
                        "position": 4,
                        "path": 0,
                        "slot": [
                            {
                                "model": "HD2_Cab4x12Greenback25",
                                "@enabled": {"value": True},
                                "params": {
                                    "LowCut": {"value": 80.0},
                                    "HighCut": {
                                        "1": {"value": 8000.0},
                                        "2": {"value": 8000.0},
                                    },
                                },
                                "version": 0,
                            },
                            {  # second cab slot — also a real model in dual-cab setups
                                "model": "HD2_CabMicNoCab",
                                "@enabled": {"value": False},
                                "params": {},
                                "version": 0,
                            },
                        ],
                    },
                    "b13": {  # output endpoint — must be skipped
                        "type": "output",
                        "position": 13,
                        "path": 0,
                        "slot": [{"model": "P35_OutputMain", "params": {}, "version": 0}],
                    },
                },
                {},  # empty path 1
            ],
        },
    }


def test_is_hsp_bytes_recognizes_magic():
    payload = _make_hsp_bytes({})
    assert is_hsp_bytes(payload)


def test_is_hsp_bytes_rejects_non_hsp():
    assert not is_hsp_bytes(b"not an hsp file")
    assert not is_hsp_bytes(b"")


def test_read_hsp_strips_magic_and_parses_json(tmp_path):
    raw = _make_hsp_bytes({"meta": {"name": "X"}, "preset": {"flow": []}})
    f = tmp_path / "x.hsp"
    f.write_bytes(raw)
    data = read_hsp(f)
    assert data["meta"]["name"] == "X"


def test_read_hsp_rejects_files_without_magic(tmp_path):
    f = tmp_path / "x.hsp"
    f.write_bytes(b'{"meta": {}}')  # JSON without magic header
    with pytest.raises(ValueError, match="not a .hsp file"):
        read_hsp(f)


def test_extract_blocks_yields_user_blocks_only_skipping_endpoints():
    blocks = extract_blocks_from_hsp(_synthetic_hsp_payload())
    models = [b["@model"] for b in blocks]
    # Endpoints b00/b13 must NOT appear
    assert "P35_InputGuitar" not in models
    assert "P35_OutputMain" not in models


def test_extract_blocks_unwraps_param_values():
    blocks = extract_blocks_from_hsp(_synthetic_hsp_payload())
    drive_block = next(b for b in blocks if b["@model"] == "HD2_AmpBrit2204")
    assert drive_block["Drive"] == 0.62
    # controlled param: take .value, drop the controller wrapper
    assert drive_block["Master"] == 0.36


def test_extract_blocks_unwraps_stereo_params_to_channel_1():
    blocks = extract_blocks_from_hsp(_synthetic_hsp_payload())
    cab_block = next(b for b in blocks if b["@model"] == "HD2_Cab4x12Greenback25")
    assert cab_block["HighCut"] == 8000.0
    assert cab_block["LowCut"] == 80.0


def test_extract_blocks_translates_known_stadium_model_ids():
    blocks = extract_blocks_from_hsp(_synthetic_hsp_payload())
    models = [b["@model"] for b in blocks]
    # Stadium "Mono" suffix gets translated to its .hlx counterpart
    assert "HD2_DrvScream808" in models
    assert "HD2_DistScream808Mono" not in models


def test_extract_blocks_yields_each_cab_slot_separately():
    blocks = extract_blocks_from_hsp(_synthetic_hsp_payload())
    models = [b["@model"] for b in blocks]
    # Both cab slots yield a catalogued block (the second is the "no-cab" pairing)
    assert "HD2_Cab4x12Greenback25" in models
    assert "HD2_CabMicNoCab" in models


def test_extract_blocks_carries_block_meta_to_hlx_block():
    blocks = extract_blocks_from_hsp(_synthetic_hsp_payload())
    amp = next(b for b in blocks if b["@model"] == "HD2_AmpBrit2204")
    assert amp["@type"] == "amp"
    assert amp["@position"] == 3
    assert amp["@path"] == 0
    assert amp["@enabled"] is True


def test_extract_blocks_unwrap_handles_empty_flow():
    assert extract_blocks_from_hsp({"preset": {"flow": []}}) == []
    assert extract_blocks_from_hsp({}) == []


def test_read_hsp_blocks_end_to_end(tmp_path):
    f = tmp_path / "x.hsp"
    f.write_bytes(_make_hsp_bytes(_synthetic_hsp_payload()))
    blocks = read_hsp_blocks(f)
    assert any(b["@model"] == "HD2_AmpBrit2204" for b in blocks)


# ----- real-export smoke test, only runs if the user's data/ dir is present -----

REAL_GOLDFINGER = Path(__file__).parents[1] / "data" / "Goldfinger.hsp"


@pytest.mark.skipif(
    not REAL_GOLDFINGER.exists(),
    reason="data/Goldfinger.hsp not present (gitignored user export)",
)
def test_real_goldfinger_hsp_extracts_blocks():
    blocks = read_hsp_blocks(REAL_GOLDFINGER)
    assert blocks, "expected to extract at least one block from real .hsp"
    models = [b["@model"] for b in blocks]
    # Goldfinger is amp-driven; we expect at least one amp model
    assert any("Amp" in m for m in models), f"no amp model found among: {models}"
    # No endpoint models leaked through
    for m in models:
        assert not m.startswith("P35_"), f"endpoint leaked: {m}"
