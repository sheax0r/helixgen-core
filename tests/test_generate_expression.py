"""Round-trip tests: spec expression → controller block on slot.params[X]."""
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
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


def _amp_block(library):
    amps = [b for b in library.list_blocks() if b.category == "amp"]
    if not amps:
        pytest.skip("No amp blocks in library.")
    return amps[0]


def _b01_param(preset, name):
    return preset["preset"]["flow"][0]["b01"]["slot"][0]["params"][name]


def test_exp_target_wraps_param_value_with_controller(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    sample_param = next(iter(amp.params.keys()))
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [{"block": amp.display_name, "param": sample_param}],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    wrapped = _b01_param(preset, sample_param)
    assert "controller" in wrapped
    ctrl = wrapped["controller"]
    assert ctrl["type"] == "param"
    assert ctrl["behavior"] == "continuous"
    assert ctrl["source"] == 0x01020100  # EXP1
    assert ctrl["min"] == 0.0
    assert ctrl["max"] == 1.0


def test_exp_custom_min_max_propagates(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    sample_param = next(iter(amp.params.keys()))
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [{
                "block": amp.display_name, "param": sample_param,
                "min": 0.25, "max": 0.75,
            }],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    ctrl = _b01_param(preset, sample_param)["controller"]
    assert ctrl["min"] == 0.25
    assert ctrl["max"] == 0.75


def test_exp_source_id_registered_in_preset_sources(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    sample_param = next(iter(amp.params.keys()))
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [{"block": amp.display_name, "param": sample_param}],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    sources = preset["preset"]["sources"]
    key = next(k for k in sources if int(k) == 0x01020100)
    assert sources[key] == {"bypass": False}


def test_exp_multi_target_wraps_each_param(tmp_path):
    library = _library(tmp_path)
    amp = _amp_block(library)
    p1, p2 = list(amp.params.keys())[:2]
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "exp-test",
        "paths": [{"input": "inst1", "blocks": [{"block": amp.display_name}]}],
        "expression": [{
            "pedal": "EXP1",
            "targets": [
                {"block": amp.display_name, "param": p1},
                {"block": amp.display_name, "param": p2},
            ],
        }],
    })
    preset = compose_preset(spec, library, source="test")
    assert "controller" in _b01_param(preset, p1)
    assert "controller" in _b01_param(preset, p2)
