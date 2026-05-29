"""Round-trip tests: spec input mode → generated .hsp b00 model + param shape."""
import json
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC
from helixgen.library import Library


# These tests need a real Stadium chassis. They're skipped when the user's
# data/ directory is empty (clean clone), matching the project's existing
# fixture-gated test pattern.

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library_with_stadium_chassis(tmp_path) -> Library:
    """Build a Library by ingesting the first .hsp in data/ as the chassis."""
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _b00_model(preset: dict, path_index: int) -> str:
    return preset["preset"]["flow"][path_index]["b00"]["slot"][0]["model"]


def _b00_params(preset: dict, path_index: int) -> dict:
    return preset["preset"]["flow"][path_index]["b00"]["slot"][0]["params"]


def _is_stereo(params: dict) -> bool:
    sample = next(iter(v for k, v in params.items() if k != "StereoLink"), None)
    return isinstance(sample, dict) and "1" in sample


@pytest.mark.parametrize("mode,expected_model", [
    ("inst1", "P35_InputInst1"),
    ("inst2", "P35_InputInst2"),
    ("both",  "P35_InputInst1_2"),
    ("none",  "P35_InputNone"),
])
def test_path0_input_mode_sets_model(tmp_path, mode, expected_model):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"input": mode, "blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    assert _b00_model(preset, 0) == expected_model


def test_path0_input_both_yields_stereo_params(tmp_path):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"input": "both", "blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    params = _b00_params(preset, 0)
    assert _is_stereo(params)
    assert params["StereoLink"] == {"value": False}


def test_path0_input_inst1_yields_mono_params(tmp_path):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"input": "inst1", "blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    params = _b00_params(preset, 0)
    assert not _is_stereo(params)
    assert "StereoLink" not in params


def test_default_path0_is_both_path1_is_none(tmp_path):
    library = _library_with_stadium_chassis(tmp_path)
    from helixgen.spec import parse_spec
    spec = parse_spec({
        "name": "input-test",
        "paths": [{"blocks": []}, {"blocks": []}],
    })
    preset = compose_preset(spec, library, source="test")
    assert _b00_model(preset, 0) == "P35_InputInst1_2"
    assert _b00_model(preset, 1) == "P35_InputNone"
