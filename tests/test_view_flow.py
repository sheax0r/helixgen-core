"""view() lifts for signal-flow param depth (parity #18): input object,
impedance, output level/pan, split type — plus generate→view→generate
round-trip stability for those fields.
"""
from pathlib import Path

import pytest

from helixgen.generate import compose_preset
from helixgen.library import Library
from helixgen.spec import parse_spec
from helixgen.view import view

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _library(tmp_path) -> Library:
    samples = sorted(DATA_DIR.glob("*.hsp"))
    if not samples:
        pytest.skip("No .hsp fixtures in data/; skipping integration test.")
    from helixgen.ingest import ingest_path
    library = Library(root=tmp_path)
    ingest_path(samples[0], library)
    return library


def _compose(library, paths, **extra):
    spec = parse_spec({"name": "view-flow", "paths": paths, **extra})
    return compose_preset(spec, library, source="test")


class TestInputLift:
    def test_all_default_input_stays_string(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{"input": "inst1", "blocks": []}])
        out = view(body, library)
        assert out["paths"][0]["input"] == "inst1"

    def test_non_default_params_lift_object(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{
            "input": {"source": "inst1", "pad": True, "trim": -6.0,
                      "gate": {"enabled": True, "threshold": -55.0,
                               "decay": 0.2}},
            "blocks": []}])
        out = view(body, library)
        inp = out["paths"][0]["input"]
        assert inp["source"] == "inst1"
        assert inp["pad"] is True
        assert inp["trim"] == -6.0
        assert inp["gate"]["enabled"] is True
        assert inp["gate"]["threshold"] == -55.0
        assert inp["gate"]["decay"] == 0.2

    def test_impedance_lifts_when_not_default(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{
            "input": {"source": "inst1", "impedance": "1M"}, "blocks": []}])
        out = view(body, library)
        assert out["paths"][0]["input"]["impedance"] == "1M"

    def test_default_impedance_suppressed(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{
            "input": {"source": "inst1", "impedance": "FirstEnabled"},
            "blocks": []}])
        out = view(body, library)
        assert out["paths"][0]["input"] == "inst1"

    def test_stereo_per_channel_lift(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{
            "input": {"source": "both", "trim": {"1": 0.0, "2": -3.0},
                      "link": True},
            "blocks": []}])
        out = view(body, library)
        inp = out["paths"][0]["input"]
        assert inp["source"] == "both"
        assert inp["trim"] == {"1": 0.0, "2": -3.0}
        assert inp["link"] is True

    def test_stereo_equal_channels_lift_scalar(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{
            "input": {"source": "both", "gate": True}, "blocks": []}])
        out = view(body, library)
        assert out["paths"][0]["input"]["gate"]["enabled"] is True

    def test_pad_not_lifted_on_none_input(self, tmp_path):
        # review F2: a "none" input carrying a leftover Pad param must not
        # lift pad (parse rejects pad-with-none) — parse_spec(view(x)) holds.
        from helixgen import mutate
        from helixgen.spec import parse_spec as _parse
        library = _library(tmp_path)
        body = _compose(library, [{
            "input": {"source": "inst1", "pad": True}, "blocks": []}])
        mutate.set_input(body, 0, "none")
        out = view(body, library)
        inp = out["paths"][0]["input"]
        assert inp == "none" or "pad" not in inp
        _parse(out)  # must not raise

    def test_link_not_lifted_on_mono_input(self, tmp_path):
        from helixgen.spec import parse_spec as _parse
        library = _library(tmp_path)
        body = _compose(library, [{"input": "inst1", "blocks": []}])
        b00 = body["preset"]["flow"][0]["b00"]["slot"][0]
        b00["params"]["StereoLink"] = {"value": True}
        out = view(body, library)
        inp = out["paths"][0]["input"]
        assert inp == "inst1" or "link" not in inp
        _parse(out)  # must not raise

    def test_float32_default_decay_not_spuriously_lifted(self, tmp_path):
        # Real exports store decay as float32(0.1) = 0.10000000149011612;
        # comparing with tolerance keeps that a non-lift.
        library = _library(tmp_path)
        body = _compose(library, [{"input": "inst1", "blocks": []}])
        b00 = body["preset"]["flow"][0]["b00"]["slot"][0]
        b00["params"]["decay"]["value"] = 0.10000000149011612
        out = view(body, library)
        assert out["paths"][0]["input"] == "inst1"


class TestOutputLift:
    def test_default_output_not_emitted(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{"blocks": []}])
        out = view(body, library)
        assert "output" not in out["paths"][0]

    def test_non_default_output_lifts(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{
            "output": {"level": -4.5, "pan": 0.25}, "blocks": []}])
        out = view(body, library)
        assert out["paths"][0]["output"] == {"level": -4.5, "pan": 0.25}

    def test_partial_lift(self, tmp_path):
        library = _library(tmp_path)
        body = _compose(library, [{"output": {"level": 2.0}, "blocks": []}])
        out = view(body, library)
        assert out["paths"][0]["output"] == {"level": 2.0}


class TestSplitTypeLift:
    def test_known_split_model_gains_type(self, tmp_path):
        library = _library(tmp_path)
        name = library.list_blocks()[0].display_name or library.list_blocks()[0].model_id
        body = _compose(library, [{"blocks": [
            {"split": {"type": "crossover", "params": {"Frequency": 800.0}}},
            {"block": name, "lane": 1},
            {"join": {"params": {"A Level": -2.0}}},
        ]}])
        out = view(body, library)
        splits = [b for p in out["paths"] for b in p["blocks"] if "split" in b]
        assert splits
        assert splits[0]["split"]["type"] == "crossover"
        assert splits[0]["split"]["model"] == "P35_AppDSPSplitXOver"
        assert splits[0]["split"]["params"]["Frequency"] == 800.0


class TestRoundTrip:
    def test_flow_params_roundtrip_stable(self, tmp_path):
        library = _library(tmp_path)
        name = library.list_blocks()[0].display_name or library.list_blocks()[0].model_id
        body = _compose(library, [{
            "input": {"source": "inst1", "impedance": "230K", "pad": True,
                      "gate": {"threshold": -60.0}},
            "output": {"level": -3.0, "pan": 0.75},
            "blocks": [
                {"split": {"type": "dynamic",
                           "params": {"Threshold": -24.5, "Reverse": True}}},
                {"block": name, "lane": 1},
                {"join": {"params": {"B Level": -6.0, "B Polarity": True}}},
            ]}])
        projected = view(body, library)
        body2 = compose_preset(parse_spec(projected), library, source="rt")
        projected2 = view(body2, library)
        assert projected == projected2
        # and the wire-level facts survive
        assert body2["preset"]["params"]["inst1Z"] == "230K"
        assert body2["preset"]["flow"][0]["b00"]["slot"][0]["params"]["Pad"] == {"value": 2}
        assert body2["preset"]["flow"][0]["b13"]["slot"][0]["params"]["gain"]["value"] == -3.0
