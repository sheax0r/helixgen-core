"""Surgical-edit coverage for signal-flow pseudo-blocks (parity #18):
`set_param` on the pseudo-block names input/output/split/join/merge routes to
`mutate.set_flow_param`, editing the endpoints / split / merge in place.
"""
import pytest

from helixgen import mutate
from helixgen.generate import compose_preset
from helixgen.mutate import MutateError
from helixgen.spec import parse_spec


def _body(hsp_library, paths):
    spec = parse_spec({"name": "m", "paths": paths})
    return compose_preset(spec, hsp_library, source="t")


def _split_body(hsp_library):
    return _body(hsp_library, [{"blocks": [
        {"block": "Tube Drive", "lane": 0, "pos": 5},
        {"split": {"type": "y"}, "lane": 0, "pos": 6},
        {"block": "Brit Amp", "lane": 1, "pos": 1},
        {"join": {}, "lane": 0, "pos": 8},
    ]}])


class TestInputPseudoBlock:
    def test_set_trim_mono(self, hsp_library):
        body = _body(hsp_library, [{"input": "inst1", "blocks": []}])
        mutate.set_param(body, "input", "trim", -6.0, hsp_library)
        params = body["preset"]["flow"][0]["b00"]["slot"][0]["params"]
        assert params["Trim"] == {"value": -6.0}

    def test_set_gate_stereo_writes_both_channels(self, hsp_library):
        body = _body(hsp_library, [{"input": "both", "blocks": []}])
        mutate.set_param(body, "input", "gate", True, hsp_library)
        params = body["preset"]["flow"][0]["b00"]["slot"][0]["params"]
        assert params["noiseGate"] == {"1": {"value": True}, "2": {"value": True}}

    def test_set_pad_bool_maps_to_enum(self, hsp_library):
        body = _body(hsp_library, [{"input": "inst1", "blocks": []}])
        mutate.set_param(body, "input", "pad", True, hsp_library)
        params = body["preset"]["flow"][0]["b00"]["slot"][0]["params"]
        assert params["Pad"] == {"value": 2}

    def test_set_impedance_writes_used_jacks(self, hsp_library):
        body = _body(hsp_library, [{"input": "both", "blocks": []}])
        mutate.set_param(body, "input", "impedance", "1M", hsp_library)
        assert body["preset"]["params"]["inst1Z"] == "1M"
        assert body["preset"]["params"]["inst2Z"] == "1M"

    def test_impedance_on_none_input_errors(self, hsp_library):
        body = _body(hsp_library, [{"blocks": []}, {"input": "none", "blocks": []}])
        with pytest.raises(MutateError, match="jack"):
            mutate.set_param(body, "input", "impedance", "1M", hsp_library, path=1)

    def test_link_requires_stereo(self, hsp_library):
        body = _body(hsp_library, [{"input": "inst1", "blocks": []}])
        with pytest.raises(MutateError, match="both|stereo"):
            mutate.set_param(body, "input", "link", True, hsp_library)

    def test_range_validated(self, hsp_library):
        body = _body(hsp_library, [{"input": "inst1", "blocks": []}])
        with pytest.raises(MutateError, match="trim"):
            mutate.set_param(body, "input", "trim", 40.0, hsp_library)

    def test_unknown_param_lists_valid(self, hsp_library):
        body = _body(hsp_library, [{"input": "inst1", "blocks": []}])
        with pytest.raises(MutateError, match="impedance"):
            mutate.set_param(body, "input", "zzz", 1.0, hsp_library)


class TestOutputPseudoBlock:
    def test_set_level(self, hsp_library):
        body = _body(hsp_library, [{"blocks": []}])
        mutate.set_param(body, "output", "level", -4.5, hsp_library)
        params = body["preset"]["flow"][0]["b13"]["slot"][0]["params"]
        assert params["gain"]["value"] == -4.5

    def test_set_pan_on_path1(self, hsp_library):
        body = _body(hsp_library, [{"blocks": []}, {"blocks": []}])
        mutate.set_param(body, "output", "pan", 0.25, hsp_library, path=1)
        params = body["preset"]["flow"][1]["b13"]["slot"][0]["params"]
        assert params["pan"]["value"] == 0.25

    def test_range_validated(self, hsp_library):
        body = _body(hsp_library, [{"blocks": []}])
        with pytest.raises(MutateError, match="pan"):
            mutate.set_param(body, "output", "pan", 1.5, hsp_library)


class TestSplitJoinPseudoBlocks:
    def test_set_split_param(self, hsp_library):
        body = _split_body(hsp_library)
        mutate.set_param(body, "split", "BalanceA", 0.2, hsp_library)
        flow0 = body["preset"]["flow"][0]
        splits = [b for b in flow0.values()
                  if isinstance(b, dict) and b.get("type") == "split"]
        assert splits[0]["slot"][0]["params"]["BalanceA"] == {"value": 0.2}

    def test_split_param_validated_against_placed_model(self, hsp_library):
        body = _split_body(hsp_library)
        with pytest.raises(MutateError, match="BalanceA"):
            mutate.set_param(body, "split", "Frequency", 800.0, hsp_library)

    def test_set_join_param_wire_name(self, hsp_library):
        body = _split_body(hsp_library)
        mutate.set_param(body, "join", "A Level", -2.0, hsp_library)
        flow0 = body["preset"]["flow"][0]
        joins = [b for b in flow0.values()
                 if isinstance(b, dict) and b.get("type") == "join"]
        assert joins[0]["slot"][0]["params"]["A Level"] == {"value": -2.0}

    def test_merge_alias(self, hsp_library):
        body = _split_body(hsp_library)
        mutate.set_param(body, "merge", "B Polarity", True, hsp_library)
        flow0 = body["preset"]["flow"][0]
        joins = [b for b in flow0.values()
                 if isinstance(b, dict) and b.get("type") == "join"]
        assert joins[0]["slot"][0]["params"]["B Polarity"] == {"value": True}

    def test_no_split_in_path_errors(self, hsp_library):
        body = _body(hsp_library, [{"blocks": []}])
        with pytest.raises(MutateError, match="split"):
            mutate.set_param(body, "split", "BalanceA", 0.2, hsp_library)

    def test_join_int_value_coerced_to_float(self, hsp_library):
        body = _split_body(hsp_library)
        mutate.set_param(body, "join", "A Level", -5, hsp_library)
        flow0 = body["preset"]["flow"][0]
        joins = [b for b in flow0.values()
                 if isinstance(b, dict) and b.get("type") == "join"]
        v = joins[0]["slot"][0]["params"]["A Level"]["value"]
        assert v == -5.0 and isinstance(v, float)

    def test_lane_rejected_on_pseudo_block(self, hsp_library):
        # review finding 7: lane was silently ignored
        body = _split_body(hsp_library)
        with pytest.raises(MutateError, match="lane"):
            mutate.set_param(body, "split", "BalanceA", 0.2, hsp_library, lane=1)
