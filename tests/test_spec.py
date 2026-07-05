import pytest

from helixgen.spec import Spec, SpecError, parse_spec


VALID = {
    "name": "Test Preset",
    "paths": [
        {
            "blocks": [
                {"block": "Brit 2204", "params": {"Drive": 0.6}},
            ],
        }
    ],
}


def test_parse_minimal_valid():
    spec = parse_spec(VALID, source="test.json")
    assert isinstance(spec, Spec)
    assert spec.name == "Test Preset"
    assert spec.author is None
    assert len(spec.paths) == 1
    assert spec.paths[0].blocks[0].block == "Brit 2204"
    assert spec.paths[0].blocks[0].params == {"Drive": 0.6}


def test_parse_with_author_and_io():
    data = {
        "name": "X",
        "author": "mike",
        "paths": [
            {"input": "inst1", "output": "Multi", "blocks": [{"block": "Y"}]}
        ],
    }
    spec = parse_spec(data, source="t.json")
    assert spec.author == "mike"
    assert spec.paths[0].input == "inst1"
    assert spec.paths[0].output == "Multi"


def test_missing_name_raises():
    bad = {k: v for k, v in VALID.items() if k != "name"}
    with pytest.raises(SpecError, match="name"):
        parse_spec(bad, source="t.json")


def test_paths_not_array_raises():
    bad = {"name": "X", "paths": {}}
    with pytest.raises(SpecError, match='"paths" must be an array'):
        parse_spec(bad, source="t.json")


def test_paths_too_long_raises():
    bad = {"name": "X", "paths": [{"blocks": []}, {"blocks": []}, {"blocks": []}]}
    with pytest.raises(SpecError, match="length 3 not supported"):
        parse_spec(bad, source="t.json")


def test_paths_empty_raises():
    bad = {"name": "X", "paths": []}
    with pytest.raises(SpecError, match="at least one"):
        parse_spec(bad, source="t.json")


def test_block_missing_block_field_raises():
    bad = {
        "name": "X",
        "paths": [{"blocks": [{"params": {}}]}],
    }
    with pytest.raises(SpecError, match='"block"'):
        parse_spec(bad, source="t.json")


def test_params_must_be_dict():
    bad = {
        "name": "X",
        "paths": [{"blocks": [{"block": "Y", "params": []}]}],
    }
    with pytest.raises(SpecError, match='"params" must be an object'):
        parse_spec(bad, source="t.json")


def test_parallel_entry_rejected():
    bad = {
        "name": "X",
        "paths": [
            {
                "blocks": [
                    {"parallel": [[{"block": "A"}], [{"block": "B"}]]},
                ]
            }
        ],
    }
    with pytest.raises(SpecError, match='"parallel" entries not supported'):
        parse_spec(bad, source="t.json")


# ---------------------------------------------------------------------------
# Snapshots — Stadium has 8 snapshot slots per preset; the spec lets the user
# define up to 8 named scenes, each a delta from the path's base values.
# ---------------------------------------------------------------------------


def test_snapshots_default_empty():
    spec = parse_spec(VALID, source="t.json")
    assert spec.snapshots == []


def test_snapshot_with_name_only_parses():
    data = {
        **VALID,
        "snapshots": [{"name": "Rhythm"}],
    }
    spec = parse_spec(data, source="t.json")
    assert len(spec.snapshots) == 1
    s = spec.snapshots[0]
    assert s.name == "Rhythm"
    assert s.disable == []
    assert s.params == []


def test_snapshot_with_disable_and_params_parses():
    from helixgen.spec import SnapshotBlockRef, SnapshotParamOverride

    data = {
        **VALID,
        "snapshots": [
            {
                "name": "Lead",
                "disable": ["Compulsive Drive"],
                "params": {"Brit 2204": {"Drive": 0.85, "Master": 0.7}},
            },
        ],
    }
    spec = parse_spec(data, source="t.json")
    s = spec.snapshots[0]
    assert s.disable == [SnapshotBlockRef(block="Compulsive Drive")]
    assert s.params == [SnapshotParamOverride(
        ref=SnapshotBlockRef(block="Brit 2204"),
        params={"Drive": 0.85, "Master": 0.7},
    )]


def test_snapshots_max_eight():
    bad = {**VALID, "snapshots": [{"name": f"S{i}"} for i in range(9)]}
    with pytest.raises(SpecError, match="at most 8"):
        parse_spec(bad, source="t.json")


def test_snapshot_missing_name_raises():
    bad = {**VALID, "snapshots": [{}]}
    with pytest.raises(SpecError, match='"name"'):
        parse_spec(bad, source="t.json")


def test_snapshot_disable_must_be_string_list():
    bad = {**VALID, "snapshots": [{"name": "X", "disable": "not-a-list"}]}
    with pytest.raises(SpecError, match='"disable" must be a list'):
        parse_spec(bad, source="t.json")


def test_snapshot_params_must_be_dict_of_dicts():
    bad = {**VALID, "snapshots": [{"name": "X", "params": "no"}]}
    with pytest.raises(SpecError, match='"params" must be an object'):
        parse_spec(bad, source="t.json")

    bad2 = {**VALID, "snapshots": [{"name": "X", "params": {"Brit 2204": "no"}}]}
    with pytest.raises(SpecError, match="must be an object"):
        parse_spec(bad2, source="t.json")


def test_snapshots_must_be_list():
    bad = {**VALID, "snapshots": {"Rhythm": {}}}
    with pytest.raises(SpecError, match='"snapshots" must be a list'):
        parse_spec(bad, source="t.json")


# ---------------------------------------------------------------------------
# Snapshot coordinate-aware disable/params — normalize both the bare form and
# a new {block, lane?, pos?, path?} form to one internal representation so a
# snapshot can reference a specific placed block among duplicate-named ones.
# ---------------------------------------------------------------------------

from helixgen.spec import SnapshotBlockRef, SnapshotParamOverride

_BASE = {"name": "P", "paths": [{"blocks": [{"block": "Stereo"}]}]}


def _spec(snapshots):
    return parse_spec({**_BASE, "snapshots": snapshots})


def test_disable_bare_string_normalizes_to_ref():
    s = _spec([{"name": "A", "disable": ["Stereo"]}])
    assert s.snapshots[0].disable == [SnapshotBlockRef(block="Stereo")]


def test_disable_coordinate_dict():
    s = _spec([{"name": "A", "disable": [{"block": "Stereo", "lane": 1, "pos": 2}]}])
    assert s.snapshots[0].disable == [SnapshotBlockRef(block="Stereo", lane=1, pos=2)]


def test_params_dict_form_normalizes_to_list():
    s = _spec([{"name": "A", "params": {"Stereo": {"Mix": 0.3}}}])
    ov = s.snapshots[0].params
    assert ov == [SnapshotParamOverride(ref=SnapshotBlockRef(block="Stereo"),
                                        params={"Mix": 0.3})]


def test_params_list_form_with_coordinates():
    s = _spec([{"name": "A", "params": [
        {"block": "Stereo", "lane": 1, "pos": 2, "params": {"Mix": 0.3}}]}])
    ov = s.snapshots[0].params
    assert ov == [SnapshotParamOverride(
        ref=SnapshotBlockRef(block="Stereo", lane=1, pos=2), params={"Mix": 0.3})]


def test_params_list_entry_requires_params_object():
    with pytest.raises(SpecError):
        _spec([{"name": "A", "params": [{"block": "Stereo"}]}])


# ---------------------------------------------------------------------------
# BlockEntry.enabled — optional base-level bypass flag
# ---------------------------------------------------------------------------


def test_block_entry_enabled_parsed():
    spec = parse_spec({"name": "n", "paths": [
        {"blocks": [{"block": "X", "enabled": False}]}]})
    assert spec.paths[0].blocks[0].enabled is False


def test_block_entry_enabled_defaults_none():
    spec = parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}]})
    assert spec.paths[0].blocks[0].enabled is None


def test_block_entry_enabled_must_be_bool():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [
            {"blocks": [{"block": "X", "enabled": "yes"}]}]})


# ---------------------------------------------------------------------------
# lane/pos fields + split/join entries (parallel routing)
# ---------------------------------------------------------------------------

from helixgen.spec import SplitEntry, JoinEntry, BlockEntry


def test_parse_lane_pos_on_block():
    s = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Pitch", "lane": 1, "pos": 1}]}]})
    b = s.paths[0].blocks[0]
    assert isinstance(b, BlockEntry) and b.lane == 1 and b.pos == 1


def test_parse_split_join_entries():
    s = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "Amp"},
        {"split": {"model": "P35_AppDSPSplitY", "params": {}}, "lane": 0, "pos": 6},
        {"block": "Pitch", "lane": 1, "pos": 1},
        {"join": {}, "lane": 0, "pos": 8},
        {"block": "Reverb"}]}]})
    kinds = [type(b).__name__ for b in s.paths[0].blocks]
    assert kinds == ["BlockEntry", "SplitEntry", "BlockEntry", "JoinEntry", "BlockEntry"]
    assert s.paths[0].blocks[1].model == "P35_AppDSPSplitY"


def test_reject_three_splits():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}}, {"join": {}},
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}}, {"join": {}},
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}}, {"join": {}}]}]})


def test_reject_split_without_join():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [
            {"split": {"model": "P35_AppDSPSplitY", "params": {}}},
            {"block": "X", "lane": 1}]}]})


def test_join_list_raises_spec_error():
    from helixgen.spec import parse_spec, SpecError
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [{"join": [1]}]}]})


# ---------------------------------------------------------------------------
# BlockEntry.no_ir — explicit "no IR loaded" marker (Task 6, IR round-trip)
# ---------------------------------------------------------------------------


def test_block_entry_parses_no_ir():
    s = parse_spec({"name": "P", "paths": [{"blocks": [
        {"block": "With Pan", "no_ir": True}]}]})
    assert s.paths[0].blocks[0].no_ir is True


def test_block_entry_no_ir_defaults_false():
    s = parse_spec({"name": "P", "paths": [{"blocks": [
        {"block": "With Pan"}]}]})
    assert s.paths[0].blocks[0].no_ir is False


def test_block_entry_no_ir_must_be_bool():
    with pytest.raises(SpecError):
        parse_spec({"name": "P", "paths": [{"blocks": [
            {"block": "With Pan", "no_ir": "yes"}]}]})


def test_block_entry_rejects_ir_and_no_ir_together():
    with pytest.raises(SpecError, match="at most one"):
        parse_spec({"name": "P", "paths": [{"blocks": [
            {"block": "With Pan", "ir": "foo.wav", "no_ir": True}]}]})
