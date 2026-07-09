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


# Device-accurate assignable footswitch set: FS1–FS5 (top row) and FS7–FS11
# (bottom row). FS6 (MODE) and FS12 (TAP/Tuner) are reserved and NOT assignable.
ASSIGNABLE_FS = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11]


def test_controller_source_ids_has_stadium_xl_assignable_fs():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    for n in ASSIGNABLE_FS:
        assert f"FS{n}" in table, f"FS{n} missing from stadium_xl table"
        assert isinstance(table[f"FS{n}"], int)


def test_reserved_fs_not_in_assignable_table():
    """FS6 (MODE) and FS12 (TAP/Tuner) are reserved — never in the assignable table."""
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    assert "FS6" not in table
    assert "FS12" not in table


def test_fs11_present_with_correct_source():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    assert table["FS11"] == 0x0101010a


def test_controller_source_ids_stadium_xl_fs_values_unique():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    fs_values = [table[f"FS{n}"] for n in ASSIGNABLE_FS]
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
    fs_values = {table[f"FS{n}"] for n in ASSIGNABLE_FS}
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


def test_resolve_exp1_toe_switch_source():
    """The EXP1 toe switch (the position/click switch under the onboard pedal)
    is the standard wah auto-engage. Its source id is 0x01010500, observed on
    ~all real wah exports."""
    sid = controllers.resolve_controller_source("stadium_xl", "EXP1Toe")
    assert sid == 0x01010500


def test_is_position_switch():
    assert controllers.is_position_switch("EXP1Toe") is True
    assert controllers.is_position_switch("FS1") is False
    assert controllers.is_position_switch("EXP1") is False


def test_exp1_toe_switch_roundtrips_and_is_distinct():
    table = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    toe = table["EXP1Toe"]
    assert controllers.controller_name_for_source("stadium_xl", toe) == "EXP1Toe"
    fs_values = {table[f"FS{n}"] for n in ASSIGNABLE_FS}
    exp_values = {table["EXP1"], table["EXP2"]}
    assert toe not in fs_values and toe not in exp_values


# --- metadata model + vocabulary fix (controller identifier ↔ English) --------

CANONICAL_IDS = [f"FS{n}" for n in ASSIGNABLE_FS] + ["EXP1", "EXP2", "EXP1Toe"]


def test_controller_meta_has_all_canonical_ids():
    meta = controllers.CONTROLLER_META["stadium_xl"]
    assert set(meta.keys()) == set(CANONICAL_IDS)


def test_controller_meta_records_are_complete():
    """Every entry carries source_id, kind, canonical_name, position_phrase, ≥1 alias."""
    meta = controllers.CONTROLLER_META["stadium_xl"]
    for cid, rec in meta.items():
        assert isinstance(rec["source_id"], int)
        assert rec["kind"] in ("footswitch", "expression", "toe")
        assert rec["canonical_name"], f"{cid} missing canonical_name"
        assert rec["position_phrase"], f"{cid} missing position_phrase"
        assert len(rec["aliases"]) >= 1, f"{cid} needs ≥1 alias"


def test_controller_meta_source_ids_unique():
    meta = controllers.CONTROLLER_META["stadium_xl"]
    sids = [rec["source_id"] for rec in meta.values()]
    assert len(set(sids)) == len(sids)


def test_controller_source_ids_derived_from_meta():
    """The flat table is derived from CONTROLLER_META (single source of truth)."""
    meta = controllers.CONTROLLER_META["stadium_xl"]
    flat = controllers.CONTROLLER_SOURCE_IDS["stadium_xl"]
    assert flat == {cid: rec["source_id"] for cid, rec in meta.items()}


def test_reserved_table_has_fs6_and_fs12():
    reserved = controllers.RESERVED["stadium_xl"]
    assert reserved["FS6"][0] == 0x01010105
    assert "MODE" in reserved["FS6"][1]
    assert reserved["FS12"][0] == 0x0101010b
    assert "TAP" in reserved["FS12"][1] or "Tuner" in reserved["FS12"][1]


def test_reserved_source_ids_excluded_from_assignable():
    reserved_sids = {sid for sid, _ in controllers.RESERVED["stadium_xl"].values()}
    assignable_sids = set(controllers.CONTROLLER_SOURCE_IDS["stadium_xl"].values())
    assert reserved_sids.isdisjoint(assignable_sids)


def test_resolve_fs6_raises_tailored_mode_error():
    with pytest.raises(controllers.ControllerError) as exc_info:
        controllers.resolve_controller_source("stadium_xl", "FS6")
    msg = str(exc_info.value)
    assert "MODE" in msg
    assert "not assignable" in msg
    assert "FS1" in msg and "FS7" in msg and "FS11" in msg


def test_resolve_fs12_raises_tailored_tap_error():
    with pytest.raises(controllers.ControllerError) as exc_info:
        controllers.resolve_controller_source("stadium_xl", "FS12")
    msg = str(exc_info.value)
    assert "TAP" in msg or "Tuner" in msg
    assert "not assignable" in msg


def test_resolve_fs11_succeeds():
    assert controllers.resolve_controller_source("stadium_xl", "FS11") == 0x0101010a


def test_controller_name_for_source_never_raises_on_untabled():
    """Reverse lookup must stay tolerant — never raise, return None for unknowns.
    This includes reserved source ids (FS6/FS12) and unmapped banks."""
    for sid in (0x01010105, 0x0101010b, 0x010104ff, 0xDEADBEEF):
        assert controllers.controller_name_for_source("stadium_xl", sid) is None


def test_english_for_controller_shows_name_and_position():
    en = controllers.english_for_controller("stadium_xl", "FS5")
    assert en == "Footswitch 5 (top row, 5th from left)"


def test_english_for_controller_bottom_row_and_exp():
    assert controllers.english_for_controller("stadium_xl", "FS11") == (
        "Footswitch 11 (bottom row, 5th from left)"
    )
    en_exp = controllers.english_for_controller("stadium_xl", "EXP1")
    assert "Expression Pedal 1" in en_exp


def test_english_for_controller_stable_across_calls():
    a = controllers.english_for_controller("stadium_xl", "FS1")
    b = controllers.english_for_controller("stadium_xl", "FS1")
    assert a == b


def test_controller_mapping_is_json_serialisable_full_table():
    import json
    mapping = controllers.controller_mapping("stadium_xl")
    assert isinstance(mapping, list)
    ids = {row["id"] for row in mapping}
    assert ids == set(CANONICAL_IDS)
    # JSON round-trip must not raise (source rendered as hex string, ints OK).
    json.dumps(mapping)
    for row in mapping:
        assert "english" in row and "aliases" in row and "source" in row
