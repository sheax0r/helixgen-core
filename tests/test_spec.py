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
            {"input": "Multi", "output": "Multi", "blocks": [{"block": "Y"}]}
        ],
    }
    spec = parse_spec(data, source="t.json")
    assert spec.author == "mike"
    assert spec.paths[0].input == "Multi"
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
