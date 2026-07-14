"""Generate-side coverage for signal-flow param depth (parity #18):
input-endpoint param normalization + overrides, preset-level impedance,
output level/pan, split type + params emission.

Uses the real-chassis skip-gated pattern from test_generate_input.py.
"""
from pathlib import Path

import pytest

from helixgen.generate import GenerateError, compose_preset
from helixgen.library import Library
from helixgen.spec import parse_spec

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _b00_params(preset, path_index=0):
    return preset["preset"]["flow"][path_index]["b00"]["slot"][0]["params"]


def _b13_params(preset, path_index=0):
    return preset["preset"]["flow"][path_index]["b13"]["slot"][0]["params"]


def _compose(library, paths, **extra):
    spec = parse_spec({"name": "flow-test", "paths": paths, **extra})
    return compose_preset(spec, library, source="test")


# --- input params --------------------------------------------------------------

class TestInputParams:
    def test_string_input_normalizes_b00_to_schema_defaults(self, tmp_path):
        preset = _compose(_library(tmp_path), [{"input": "inst1", "blocks": []}])
        assert _b00_params(preset) == {
            "Pad": {"value": 1},
            "Trim": {"value": 0.0},
            "noiseGate": {"value": False},
            "threshold": {"value": -48.0},
            "decay": {"value": 0.1},
        }

    def test_none_input_has_no_pad(self, tmp_path):
        preset = _compose(_library(tmp_path),
                          [{"blocks": []}, {"input": "none", "blocks": []}])
        params = _b00_params(preset, 1)
        assert "Pad" not in params
        assert params["threshold"] == {"value": -48.0}

    def test_object_overrides_mono(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "input": {"source": "inst1", "pad": True, "trim": -6.0,
                      "gate": {"enabled": True, "threshold": -55.0, "decay": 0.2}},
            "blocks": []}])
        assert _b00_params(preset) == {
            "Pad": {"value": 2},
            "Trim": {"value": -6.0},
            "noiseGate": {"value": True},
            "threshold": {"value": -55.0},
            "decay": {"value": 0.2},
        }

    def test_object_stereo_shape(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "input": {"source": "both", "trim": {"1": 0.0, "2": -3.0},
                      "link": True},
            "blocks": []}])
        params = _b00_params(preset)
        assert params["Trim"] == {"1": {"value": 0.0}, "2": {"value": -3.0}}
        assert params["Pad"] == {"1": {"value": 1}, "2": {"value": 1}}
        assert params["StereoLink"] == {"value": True}

    def test_scalar_writes_both_channels_on_stereo(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "input": {"source": "both", "gate": True}, "blocks": []}])
        params = _b00_params(preset)
        assert params["noiseGate"] == {"1": {"value": True}, "2": {"value": True}}


# --- impedance -------------------------------------------------------------------

class TestImpedance:
    def test_default_written_for_used_jacks(self, tmp_path):
        preset = _compose(_library(tmp_path), [{"input": "inst1", "blocks": []}])
        assert preset["preset"]["params"]["inst1Z"] == "FirstEnabled"

    def test_recipe_value_written(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "input": {"source": "inst1", "impedance": "1M"}, "blocks": []}])
        assert preset["preset"]["params"]["inst1Z"] == "1M"

    def test_both_jacks_scalar(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "input": {"source": "both", "impedance": "230K"}, "blocks": []}])
        assert preset["preset"]["params"]["inst1Z"] == "230K"
        assert preset["preset"]["params"]["inst2Z"] == "230K"

    def test_per_jack_object(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "input": {"source": "both",
                      "impedance": {"inst1": "1M", "inst2": "FirstBlock"}},
            "blocks": []}])
        assert preset["preset"]["params"]["inst1Z"] == "1M"
        assert preset["preset"]["params"]["inst2Z"] == "FirstBlock"

    def test_unused_jack_keeps_chassis_value(self, tmp_path):
        library = _library(tmp_path)
        chassis = library.load_chassis()
        chassis_inst2 = ((chassis.get("preset") or {}).get("params") or {}).get("inst2Z")
        preset = _compose(library, [{"input": "inst1", "blocks": []}])
        assert preset["preset"]["params"].get("inst2Z") == chassis_inst2

    def test_cross_path_conflict_raises(self, tmp_path):
        with pytest.raises(GenerateError, match="inst1"):
            _compose(_library(tmp_path), [
                {"input": {"source": "inst1", "impedance": "1M"}, "blocks": []},
                {"input": {"source": "inst1", "impedance": "230K"}, "blocks": []},
            ])

    def test_cross_path_same_value_ok(self, tmp_path):
        preset = _compose(_library(tmp_path), [
            {"input": {"source": "inst1", "impedance": "1M"}, "blocks": []},
            {"input": {"source": "inst1", "impedance": "1M"}, "blocks": []},
        ])
        assert preset["preset"]["params"]["inst1Z"] == "1M"

    def test_explicit_wins_over_omitted_same_jack(self, tmp_path):
        # review F1: an omitted impedance is NOT an explicit "FirstEnabled"
        # request — explicit-then-default and default-then-explicit both
        # resolve to the explicit value without erroring.
        preset = _compose(_library(tmp_path), [
            {"input": {"source": "inst1", "impedance": "1M"}, "blocks": []},
            {"input": "inst1", "blocks": []},
        ])
        assert preset["preset"]["params"]["inst1Z"] == "1M"
        preset = _compose(_library(tmp_path), [
            {"input": "inst1", "blocks": []},
            {"input": {"source": "inst1", "impedance": "1M"}, "blocks": []},
        ])
        assert preset["preset"]["params"]["inst1Z"] == "1M"

    def test_default_both_plus_explicit_inst1_ok(self, tmp_path):
        # the most common dual-path shape: paths[0] defaults to "both"
        preset = _compose(_library(tmp_path), [
            {"blocks": []},
            {"input": {"source": "inst1", "impedance": "230K"}, "blocks": []},
        ])
        assert preset["preset"]["params"]["inst1Z"] == "230K"
        assert preset["preset"]["params"]["inst2Z"] == "FirstEnabled"

    def test_per_jack_dict_omission_is_not_explicit(self, tmp_path):
        preset = _compose(_library(tmp_path), [
            {"input": {"source": "both", "impedance": {"inst1": "1M"}},
             "blocks": []},
            {"input": {"source": "inst2", "impedance": "230K"}, "blocks": []},
        ])
        assert preset["preset"]["params"]["inst1Z"] == "1M"
        assert preset["preset"]["params"]["inst2Z"] == "230K"


# --- output ---------------------------------------------------------------------

class TestOutput:
    def test_defaults_normalized(self, tmp_path):
        preset = _compose(_library(tmp_path), [{"blocks": []}])
        params = _b13_params(preset)
        assert params["gain"]["value"] == 0.0
        assert params["pan"]["value"] == 0.5

    def test_level_pan_applied(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "output": {"level": -4.5, "pan": 0.25}, "blocks": []}])
        params = _b13_params(preset)
        assert params["gain"]["value"] == -4.5
        assert params["pan"]["value"] == 0.25

    def test_partial_output(self, tmp_path):
        preset = _compose(_library(tmp_path), [{
            "output": {"level": 2.0}, "blocks": []}])
        params = _b13_params(preset)
        assert params["gain"]["value"] == 2.0
        assert params["pan"]["value"] == 0.5


# --- split/join emission ----------------------------------------------------------

class TestSplitJoinEmission:
    def _first_amp(self, library):
        # any real block name usable in a lane-1 branch
        blocks = library.list_blocks()
        assert blocks
        return blocks[0].display_name or blocks[0].model_id

    def test_split_type_emits_resolved_model_and_params(self, tmp_path):
        library = _library(tmp_path)
        name = self._first_amp(library)
        spec = parse_spec({"name": "s", "paths": [{"blocks": [
            {"split": {"type": "crossover",
                       "params": {"Frequency": 800.0, "Reverse": True}}},
            {"block": name, "lane": 1},
            {"join": {"params": {"A Level": -2.0, "B Polarity": True}}},
        ]}]})
        preset = compose_preset(spec, library, source="test")
        flow0 = preset["preset"]["flow"][0]
        splits = [b for b in flow0.values()
                  if isinstance(b, dict) and b.get("type") == "split"]
        joins = [b for b in flow0.values()
                 if isinstance(b, dict) and b.get("type") == "join"]
        assert splits and joins
        s_slot = splits[0]["slot"][0]
        assert s_slot["model"] == "P35_AppDSPSplitXOver"
        assert s_slot["params"]["Frequency"] == {"value": 800.0}
        assert s_slot["params"]["Reverse"] == {"value": True}
        j_slot = joins[0]["slot"][0]
        assert j_slot["model"] == "P35_AppDSPJoin"
        assert j_slot["params"]["A Level"] == {"value": -2.0}
        assert j_slot["params"]["B Polarity"] == {"value": True}
