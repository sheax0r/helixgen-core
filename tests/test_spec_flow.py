"""parse_spec coverage for the signal-flow param depth fields (parity #18):
input object form, output object, split type + validated params, join
(merge-mixer) validated params.
"""
import pytest

from helixgen.spec import (
    InputSpec,
    OutputSpec,
    SpecError,
    SplitEntry,
    parse_spec,
)


def _spec(paths):
    return {"name": "t", "paths": paths}


def _parse_path0(path):
    return parse_spec(_spec([path])).paths[0]


# --- input: object form -------------------------------------------------------

class TestInputObject:
    def test_string_form_still_parses(self):
        p = _parse_path0({"input": "inst1", "blocks": []})
        assert p.input == "inst1"

    def test_object_form_parses_to_input_spec(self):
        p = _parse_path0({"input": {"source": "inst1", "impedance": "1M",
                                    "pad": True, "trim": -6.0,
                                    "gate": {"enabled": True, "threshold": -55.0,
                                             "decay": 0.2}},
                          "blocks": []})
        spec = p.input
        assert isinstance(spec, InputSpec)
        assert spec.source == "inst1"
        assert spec.impedance == "1M"
        assert spec.pad is True
        assert spec.trim == -6.0
        assert spec.gate_enabled is True
        assert spec.gate_threshold == -55.0
        assert spec.gate_decay == 0.2

    def test_object_source_optional(self):
        p = _parse_path0({"input": {"trim": -3.0}, "blocks": []})
        assert p.input.source is None
        assert p.input.trim == -3.0

    def test_object_bad_source_rejected(self):
        with pytest.raises(SpecError, match="inst1"):
            _parse_path0({"input": {"source": "mic"}, "blocks": []})

    def test_gate_bool_shorthand(self):
        p = _parse_path0({"input": {"gate": True}, "blocks": []})
        assert p.input.gate_enabled is True
        p = _parse_path0({"input": {"gate": False}, "blocks": []})
        assert p.input.gate_enabled is False

    def test_gate_object_enabled_defaults_true(self):
        p = _parse_path0({"input": {"gate": {"threshold": -60.0}}, "blocks": []})
        assert p.input.gate_enabled is True
        assert p.input.gate_threshold == -60.0

    def test_gate_unknown_key_rejected(self):
        with pytest.raises(SpecError, match="gate"):
            _parse_path0({"input": {"gate": {"thresh": -60.0}}, "blocks": []})

    def test_unknown_input_key_rejected(self):
        with pytest.raises(SpecError, match="padding"):
            _parse_path0({"input": {"padding": True}, "blocks": []})

    def test_trim_out_of_range_rejected(self):
        with pytest.raises(SpecError, match="trim"):
            _parse_path0({"input": {"trim": 12.0}, "blocks": []})

    def test_bad_impedance_rejected(self):
        with pytest.raises(SpecError, match="impedance"):
            _parse_path0({"input": {"impedance": "3.5M"}, "blocks": []})

    def test_impedance_per_jack_object(self):
        p = _parse_path0({"input": {"source": "both",
                                    "impedance": {"inst1": "1M", "inst2": "230K"}},
                          "blocks": []})
        assert p.input.impedance == {"inst1": "1M", "inst2": "230K"}

    def test_impedance_object_unknown_jack_rejected(self):
        with pytest.raises(SpecError, match="inst1"):
            _parse_path0({"input": {"source": "both",
                                    "impedance": {"aux": "1M"}}, "blocks": []})

    def test_impedance_on_none_source_rejected(self):
        with pytest.raises(SpecError, match="none"):
            _parse_path0({"input": {"source": "none", "impedance": "1M"},
                          "blocks": []})

    def test_pad_on_none_source_rejected(self):
        with pytest.raises(SpecError, match="pad"):
            _parse_path0({"input": {"source": "none", "pad": True}, "blocks": []})

    def test_link_requires_both(self):
        with pytest.raises(SpecError, match="link"):
            _parse_path0({"input": {"source": "inst1", "link": True}, "blocks": []})
        p = _parse_path0({"input": {"source": "both", "link": True}, "blocks": []})
        assert p.input.link is True

    def test_per_channel_values_on_both(self):
        p = _parse_path0({"input": {"source": "both",
                                    "trim": {"1": 0.0, "2": -3.0}}, "blocks": []})
        assert p.input.trim == {"1": 0.0, "2": -3.0}

    def test_per_channel_values_rejected_on_mono(self):
        with pytest.raises(SpecError, match="both"):
            _parse_path0({"input": {"source": "inst1",
                                    "trim": {"1": 0.0, "2": -3.0}}, "blocks": []})

    def test_per_channel_value_validated(self):
        with pytest.raises(SpecError, match="trim"):
            _parse_path0({"input": {"source": "both",
                                    "trim": {"1": 0.0, "2": 99.0}}, "blocks": []})

    def test_path1_defaults_none_so_pad_rejected(self):
        # paths[1] defaults to source "none"; pad without an explicit live
        # source is a validation error there.
        with pytest.raises(SpecError, match="pad"):
            parse_spec(_spec([{"blocks": []},
                              {"input": {"pad": True}, "blocks": []}]))

    def test_path0_defaults_both_so_link_allowed(self):
        p = _parse_path0({"input": {"link": True}, "blocks": []})
        assert p.input.link is True


# --- output --------------------------------------------------------------------

class TestOutput:
    def test_output_object(self):
        p = _parse_path0({"output": {"level": -3.0, "pan": 0.4}, "blocks": []})
        assert isinstance(p.output, OutputSpec)
        assert p.output.level == -3.0
        assert p.output.pan == 0.4

    def test_output_partial(self):
        p = _parse_path0({"output": {"level": 2.0}, "blocks": []})
        assert p.output.level == 2.0
        assert p.output.pan is None

    def test_output_string_now_actionably_rejected(self):
        with pytest.raises(SpecError, match="level"):
            _parse_path0({"output": "xlr", "blocks": []})

    def test_output_unknown_key_rejected(self):
        with pytest.raises(SpecError, match="gain"):
            _parse_path0({"output": {"gain": 1.0}, "blocks": []})

    def test_output_range_validated(self):
        with pytest.raises(SpecError, match="pan"):
            _parse_path0({"output": {"pan": 2.0}, "blocks": []})


# --- split type + params --------------------------------------------------------

def _split_path(split, blocks=None):
    return {"blocks": [
        {"split": split},
        {"block": "Something", "lane": 1},
        {"join": {}},
    ] + (blocks or [])}


class TestSplitType:
    def test_type_resolves_model(self):
        p = _parse_path0(_split_path({"type": "crossover",
                                      "params": {"Frequency": 800.0}}))
        entry = p.blocks[0]
        assert isinstance(entry, SplitEntry)
        assert entry.model == "P35_AppDSPSplitXOver"
        assert entry.params == {"Frequency": 800.0}

    def test_model_still_accepted(self):
        p = _parse_path0(_split_path({"model": "P35_AppDSPSplitY"}))
        assert p.blocks[0].model == "P35_AppDSPSplitY"

    def test_type_and_model_must_agree(self):
        with pytest.raises(SpecError, match="agree|match"):
            _parse_path0(_split_path({"type": "y",
                                      "model": "P35_AppDSPSplitAB"}))

    def test_type_and_matching_model_ok(self):
        p = _parse_path0(_split_path({"type": "ab",
                                      "model": "P35_AppDSPSplitAB"}))
        assert p.blocks[0].model == "P35_AppDSPSplitAB"

    def test_unknown_type_lists_valid(self):
        with pytest.raises(SpecError, match="crossover"):
            _parse_path0(_split_path({"type": "xover"}))

    def test_neither_type_nor_model_rejected(self):
        with pytest.raises(SpecError, match="type|model"):
            _parse_path0(_split_path({"params": {"BalanceA": 0.2}}))

    def test_split_params_validated_for_known_model(self):
        with pytest.raises(SpecError, match="BalanceA"):
            _parse_path0(_split_path({"type": "y", "params": {"Balance": 0.1}}))

    def test_split_param_range_validated(self):
        with pytest.raises(SpecError, match="Frequency"):
            _parse_path0(_split_path({"type": "crossover",
                                      "params": {"Frequency": 24000}}))

    def test_unknown_model_params_pass_through(self):
        p = _parse_path0(_split_path({"model": "P35_FutureSplit",
                                      "params": {"Zork": 1.0}}))
        assert p.blocks[0].params == {"Zork": 1.0}

    def test_non_dict_split_params_is_spec_error(self):
        # review finding 2: was a TypeError crash
        with pytest.raises(SpecError, match="params"):
            _parse_path0(_split_path({"type": "y", "params": [1, 2]}))

    def test_non_dict_join_params_is_spec_error(self):
        with pytest.raises(SpecError, match="params"):
            _parse_path0({"blocks": [
                {"split": {"type": "y"}},
                {"block": "X", "lane": 1},
                {"join": {"params": "x"}},
            ]})

    def test_unhashable_impedance_is_spec_error(self):
        # review finding 3: was a TypeError crash
        with pytest.raises(SpecError, match="impedance"):
            _parse_path0({"input": {"impedance": ["1M"]}, "blocks": []})
        with pytest.raises(SpecError, match="impedance"):
            _parse_path0({"input": {"source": "both",
                                    "impedance": {"inst1": ["1M"]}},
                          "blocks": []})


class TestJoinParams:
    def test_join_params_validated(self):
        p = _parse_path0(_split_path({"type": "y"}, []))
        # baseline parse ok; now a join with valid mixer params:
        p = _parse_path0({"blocks": [
            {"split": {"type": "y"}},
            {"block": "X", "lane": 1},
            {"join": {"params": {"A Level": -2.0, "B Pan": 0.1,
                                 "B Polarity": True}}},
        ]})
        join = p.blocks[2]
        assert join.params["A Level"] == -2.0

    def test_join_unknown_param_rejected(self):
        with pytest.raises(SpecError, match="A Level"):
            _parse_path0({"blocks": [
                {"split": {"type": "y"}},
                {"block": "X", "lane": 1},
                {"join": {"params": {"ALevel": -2.0}}},
            ]})

    def test_join_range_validated(self):
        with pytest.raises(SpecError, match="B Level"):
            _parse_path0({"blocks": [
                {"split": {"type": "y"}},
                {"block": "X", "lane": 1},
                {"join": {"params": {"B Level": 40.0}}},
            ]})

    def test_join_custom_model_permissive(self):
        p = _parse_path0({"blocks": [
            {"split": {"type": "y"}},
            {"block": "X", "lane": 1},
            {"join": {"model": "P35_FutureJoin", "params": {"Zork": 2.0}}},
        ]})
        assert p.blocks[2].params == {"Zork": 2.0}
