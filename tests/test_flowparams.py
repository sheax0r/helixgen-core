"""Schema tables + pure validators for signal-flow params (parity #18).

Shapes are pinned by the design spec
(docs/superpowers/specs/2026-07-14-signal-flow-param-depth-design.md):
derived from the 211-export corpus + bundled device defs + the device's own
self-described `preset.instN.z` PropertyDef.
"""
import pytest

from helixgen import flowparams as fp


# --- impedance ---------------------------------------------------------------

class TestImpedance:
    def test_values_are_the_device_enum_ladder(self):
        assert fp.IMPEDANCE_VALUES == (
            "FirstBlock", "FirstEnabled", "10K", "22K", "32K",
            "70K", "90K", "136K", "230K", "1M",
        )

    def test_default_is_device_declared_first_enabled(self):
        assert fp.IMPEDANCE_DEFAULT == "FirstEnabled"

    def test_device_int_is_enum_index(self):
        assert fp.impedance_device_int("FirstBlock") == 0
        assert fp.impedance_device_int("FirstEnabled") == 1
        assert fp.impedance_device_int("230K") == 8
        assert fp.impedance_device_int("1M") == 9

    def test_device_int_unknown_string_falls_back_to_default(self, capsys):
        assert fp.impedance_device_int("nonsense") == 1
        assert "unrecognized impedance" in capsys.readouterr().err

    def test_validate_impedance_rejects_unknown(self):
        with pytest.raises(ValueError, match="FirstEnabled"):
            fp.validate_impedance("470K")

    def test_validate_impedance_accepts_every_ladder_value(self):
        for v in fp.IMPEDANCE_VALUES:
            fp.validate_impedance(v)


# --- input fields ------------------------------------------------------------

class TestInputFields:
    def test_hsp_defaults_match_device_defs(self):
        assert fp.INPUT_HSP_DEFAULTS == {
            "Pad": 1, "Trim": 0.0, "noiseGate": False,
            "threshold": -48.0, "decay": 0.1,
        }
        assert fp.STEREO_LINK_DEFAULT is False

    def test_validate_input_field_ranges(self):
        fp.validate_input_field("trim", -24.0)
        fp.validate_input_field("trim", 6)
        with pytest.raises(ValueError, match="trim"):
            fp.validate_input_field("trim", 7.0)
        with pytest.raises(ValueError, match="threshold"):
            fp.validate_input_field("threshold", 5.0)
        with pytest.raises(ValueError, match="decay"):
            fp.validate_input_field("decay", 0.0)

    def test_validate_input_field_types(self):
        with pytest.raises(ValueError, match="pad"):
            fp.validate_input_field("pad", 1)  # bool required, not int
        fp.validate_input_field("pad", True)
        with pytest.raises(ValueError, match="link"):
            fp.validate_input_field("link", "yes")


# --- split / join / output schemas -------------------------------------------

class TestSplitTypes:
    def test_type_to_model_table(self):
        assert fp.SPLIT_TYPES == {
            "y": "P35_AppDSPSplitY",
            "ab": "P35_AppDSPSplitAB",
            "crossover": "P35_AppDSPSplitXOver",
            "dynamic": "P35_AppDSPSplitDyn",
        }

    def test_model_to_type_inverse(self):
        assert fp.SPLIT_MODEL_TO_TYPE["P35_AppDSPSplitXOver"] == "crossover"

    def test_split_param_schema_names(self):
        assert set(fp.SPLIT_PARAM_SCHEMAS["P35_AppDSPSplitY"]) == {
            "BalanceA", "BalanceB", "enable"}
        assert set(fp.SPLIT_PARAM_SCHEMAS["P35_AppDSPSplitDyn"]) == {
            "Threshold", "Attack", "Decay", "Reverse", "enable"}

    def test_validate_wire_params_ok(self):
        fp.validate_wire_params("P35_AppDSPSplitXOver",
                                {"Frequency": 800.0, "Reverse": True})

    def test_validate_wire_params_unknown_name_lists_valid(self):
        with pytest.raises(ValueError, match="BalanceA"):
            fp.validate_wire_params("P35_AppDSPSplitY", {"Balance": 0.3})

    def test_validate_wire_params_range(self):
        with pytest.raises(ValueError, match="Frequency"):
            fp.validate_wire_params("P35_AppDSPSplitXOver", {"Frequency": 20000})

    def test_validate_wire_params_type(self):
        with pytest.raises(ValueError, match="Reverse"):
            fp.validate_wire_params("P35_AppDSPSplitXOver", {"Reverse": 0.5})

    def test_validate_wire_params_unknown_model_is_permissive(self):
        # forward-compat: a model we have no schema for passes through
        fp.validate_wire_params("P35_SomeFutureSplit", {"Whatever": 1.0})

    def test_coerce_wire_params_int_to_float(self):
        out = fp.coerce_wire_params("P35_AppDSPSplitXOver", {"Frequency": 800})
        assert out["Frequency"] == 800.0 and isinstance(out["Frequency"], float)
        out = fp.coerce_wire_params(fp.JOIN_MODEL, {"A Level": -5,
                                                    "B Polarity": True})
        assert isinstance(out["A Level"], float)
        assert out["B Polarity"] is True

    def test_coerce_wire_params_unknown_model_passthrough(self):
        assert fp.coerce_wire_params("P35_Future", {"X": 1}) == {"X": 1}


class TestJoinSchema:
    def test_join_param_names_are_wire_names_with_spaces(self):
        assert set(fp.JOIN_PARAM_SCHEMA) == {
            "A Level", "A Pan", "B Level", "B Pan", "B Polarity", "Level"}

    def test_join_validation(self):
        fp.validate_wire_params(fp.JOIN_MODEL, {"A Level": -2.0, "B Polarity": True})
        with pytest.raises(ValueError, match="A Level"):
            fp.validate_wire_params(fp.JOIN_MODEL, {"A Level": 13.0})


class TestOutputSchema:
    def test_output_fields(self):
        assert fp.OUTPUT_FIELD_TO_HSP == {"level": "gain", "pan": "pan"}
        assert fp.OUTPUT_HSP_DEFAULTS == {"gain": 0.0, "pan": 0.5}

    def test_validate_output_field(self):
        fp.validate_output_field("level", -120.0)
        fp.validate_output_field("pan", 1.0)
        with pytest.raises(ValueError, match="level"):
            fp.validate_output_field("level", 21.0)
        with pytest.raises(ValueError, match="pan"):
            fp.validate_output_field("pan", 1.5)
        with pytest.raises(ValueError, match="level"):
            fp.validate_output_field("level", True)


# --- jack helpers ------------------------------------------------------------

class TestJacks:
    def test_jacks_for_mode(self):
        assert fp.jacks_for_mode("inst1") == ("inst1",)
        assert fp.jacks_for_mode("inst2") == ("inst2",)
        assert fp.jacks_for_mode("both") == ("inst1", "inst2")
        assert fp.jacks_for_mode("none") == ()

    def test_trails_capable(self):
        assert fp.trails_capable("delay", "HD2_DelaySimple")
        assert fp.trails_capable("reverb", "HD2_ReverbPlate")
        assert fp.trails_capable("send", "HD2_FXLoopMono1")
        assert fp.trails_capable("send", "HD2_FXLoopStereo1_2")
        assert not fp.trails_capable("send", "HD2_SendMono1")
        assert not fp.trails_capable("drive", "HD2_DrvScream808")
