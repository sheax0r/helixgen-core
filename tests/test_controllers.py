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


def test_controller_source_ids_has_stadium_xl_fs_1_through_10():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    for n in range(1, 11):
        assert f"FS{n}" in table, f"FS{n} missing from stadium_xl table"
        assert isinstance(table[f"FS{n}"], int)


def test_controller_source_ids_stadium_xl_fs_values_unique():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    fs_values = [table[f"FS{n}"] for n in range(1, 11)]
    assert len(set(fs_values)) == len(fs_values), "FS source IDs are not unique"


def test_resolve_controller_source_known_name():
    sid = controllers.resolve_controller_source("stadium_xl", "FS1")
    assert isinstance(sid, int)


def test_resolve_controller_source_unknown_raises_with_valid_list():
    with pytest.raises(controllers.ControllerError) as exc_info:
        controllers.resolve_controller_source("stadium_xl", "FS99")
    msg = str(exc_info.value)
    assert "FS99" in msg
    assert "FS1" in msg


def test_controller_source_ids_has_exp1_exp2():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    assert "EXP1" in table
    assert "EXP2" in table


def test_exp_source_ids_distinct_from_fs():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    fs_values = {table[f"FS{n}"] for n in range(1, 11)}
    exp_values = {table["EXP1"], table["EXP2"]}
    assert fs_values.isdisjoint(exp_values), (
        "EXP source IDs collide with FS IDs; check the table."
    )


def test_input_mode_for_model_roundtrips():
    for mode in ("inst1", "inst2", "both", "none"):
        model = controllers.resolve_input_model("stadium_xl", mode)
        assert controllers.input_mode_for_model("stadium_xl", model) == mode


def test_input_mode_for_model_unknown_returns_none():
    assert controllers.input_mode_for_model("stadium_xl", "P35_NotAnInput") is None


def test_controller_name_for_source_roundtrips():
    for name in ("FS1", "FS10", "EXP1", "EXP2"):
        sid = controllers.resolve_controller_source("stadium_xl", name)
        assert controllers.controller_name_for_source("stadium_xl", sid) == name


def test_controller_name_for_source_unknown_returns_none():
    assert controllers.controller_name_for_source("stadium_xl", 0xDEADBEEF) is None
