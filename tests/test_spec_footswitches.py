"""Parse-level tests for the spec footswitches section."""
import pytest

from helixgen.spec import SpecError, parse_spec


def _spec_with_footswitches(*entries):
    return {
        "name": "fs-test",
        "paths": [{"blocks": [{"block": "Compulsive Drive"}]}],
        "footswitches": list(entries),
    }


def test_no_footswitches_field_yields_empty_list():
    spec = parse_spec({"name": "t", "paths": [{"blocks": []}]})
    assert spec.footswitches == []


def test_single_footswitch_minimal_fields():
    spec = parse_spec(_spec_with_footswitches(
        {"switch": "FS3", "block": "Compulsive Drive"},
    ))
    assert len(spec.footswitches) == 1
    fs = spec.footswitches[0]
    assert fs.switch == "FS3"
    assert fs.block == "Compulsive Drive"
    assert fs.behavior == "latching"  # default


def test_footswitch_with_explicit_behavior_momentary():
    spec = parse_spec(_spec_with_footswitches(
        {"switch": "FS4", "block": "Compulsive Drive", "behavior": "momentary"},
    ))
    assert spec.footswitches[0].behavior == "momentary"


def test_footswitch_invalid_behavior_rejected():
    with pytest.raises(SpecError, match='"behavior" must be'):
        parse_spec(_spec_with_footswitches(
            {"switch": "FS1", "block": "X", "behavior": "weird"},
        ))


def test_footswitch_missing_switch_rejected():
    with pytest.raises(SpecError, match='"switch" is required'):
        parse_spec(_spec_with_footswitches({"block": "X"}))


def test_footswitch_missing_block_rejected():
    with pytest.raises(SpecError, match='"block" is required'):
        parse_spec(_spec_with_footswitches({"switch": "FS1"}))


def test_footswitch_duplicate_block_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec(_spec_with_footswitches(
            {"switch": "FS1", "block": "A"},
            {"switch": "FS2", "block": "A"},
        ))


def test_footswitches_must_be_list():
    with pytest.raises(SpecError, match='"footswitches" must be a list'):
        parse_spec({"name": "t", "paths": [{"blocks": []}], "footswitches": {}})


def test_one_switch_may_drive_multiple_blocks():
    spec = parse_spec({"name": "n", "paths": [{"blocks": [
        {"block": "A"}, {"block": "B"}]}],
        "footswitches": [
            {"switch": "FS1", "block": "A"},
            {"switch": "FS1", "block": "B"}]})
    assert [f.block for f in spec.footswitches] == ["A", "B"]
    assert all(f.switch == "FS1" for f in spec.footswitches)


def test_block_still_limited_to_one_switch():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [{"block": "A"}]}],
            "footswitches": [
                {"switch": "FS1", "block": "A"},
                {"switch": "FS2", "block": "A"}]})
