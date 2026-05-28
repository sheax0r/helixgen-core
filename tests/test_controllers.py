"""Sanity tests for the per-chassis controllers table."""
import pytest

from helixgen import controllers


def test_input_models_has_stadium_xl_with_all_four_modes():
    table = controllers.INPUT_MODELS["stadium_xl"]
    assert set(table.keys()) == {"inst1", "inst2", "both", "none"}


def test_input_models_stadium_xl_model_ids_are_p35():
    table = controllers.INPUT_MODELS["stadium_xl"]
    for mode, model_id in table.items():
        assert model_id.startswith("P35_Input"), (
            f"mode {mode!r} maps to {model_id!r}, expected P35_Input* prefix"
        )


def test_resolve_input_model_returns_known_model():
    assert controllers.resolve_input_model("stadium_xl", "both") == "P35_InputInst1_2"


def test_resolve_input_model_unknown_mode_raises_with_valid_list():
    with pytest.raises(controllers.ControllerError) as exc_info:
        controllers.resolve_input_model("stadium_xl", "stereo_only")
    msg = str(exc_info.value)
    assert "stereo_only" in msg
    assert "inst1" in msg and "both" in msg


def test_resolve_input_model_unknown_device_falls_back_to_stadium_xl():
    # Unknown device_id falls back; should resolve "both" via the XL table.
    assert controllers.resolve_input_model("future_device", "both") == "P35_InputInst1_2"
