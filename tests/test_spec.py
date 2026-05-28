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
    with pytest.raises(SpecError, match='"parallel" entries not supported in v1'):
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
    assert s.params == {}


def test_snapshot_with_disable_and_params_parses():
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
    assert s.disable == ["Compulsive Drive"]
    assert s.params == {"Brit 2204": {"Drive": 0.85, "Master": 0.7}}


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
