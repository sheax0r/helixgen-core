"""Integration test: input + snapshots + footswitches + expression in one spec."""
import json
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC
from helixgen.library import Library

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def test_combined_spec_roundtrips_with_all_features(tmp_path):
    library = _library(tmp_path)
    amps = [b for b in library.list_blocks() if b.category == "amp"]
    drives = [b for b in library.list_blocks() if b.category == "drive"]
    if not amps or not drives:
        pytest.skip("Need at least one amp and one drive in the library.")
    amp = amps[0]
    drive = drives[0]
    amp_param = next(iter(amp.params.keys()))

    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "combined",
        "paths": [{
            "input": "both",
            "blocks": [
                {"block": drive.display_name},
                {"block": amp.display_name},
            ],
        }],
        "snapshots": [
            {"name": "Rhythm"},
            {"name": "Lead", "disable": [drive.display_name]},
        ],
        "footswitches": [
            {"switch": "FS3", "block": drive.display_name},
        ],
        "expression": [
            {"pedal": "EXP1", "targets": [
                {"block": amp.display_name, "param": amp_param, "min": 0.1, "max": 0.9},
            ]},
        ],
    })
    preset = compose_preset(spec, library, source="test")

    # Input: path 0 is stereo (both)
    assert preset["preset"]["flow"][0]["b00"]["slot"][0]["model"] == "P35_InputInst1_2"

    # Snapshot: drive block has snapshots array showing disable in snap 1
    drive_enabled = preset["preset"]["flow"][0]["b01"]["@enabled"]
    assert drive_enabled["snapshots"][1] is False

    # Footswitch: drive block's @enabled has a controller
    assert "controller" in drive_enabled
    assert drive_enabled["controller"]["source"] == 0x01010102  # FS3

    # Expression: amp block's chosen param has a controller
    amp_param_wrapped = preset["preset"]["flow"][0]["b02"]["slot"][0]["params"][amp_param]
    assert "controller" in amp_param_wrapped
    assert amp_param_wrapped["controller"]["source"] == 0x01020100  # EXP1
    assert amp_param_wrapped["controller"]["min"] == 0.1

    # Sources: both source IDs are registered
    sources = preset["preset"]["sources"]
    source_ids = {int(k) for k in sources}
    assert 0x01010102 in source_ids
    assert 0x01020100 in source_ids
