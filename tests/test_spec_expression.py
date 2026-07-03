"""Parse-level tests for the spec expression section."""
import pytest

from helixgen.spec import SpecError, parse_spec


def _spec(*expression_entries):
    return parse_spec({
        "name": "exp-test",
        "paths": [{"blocks": [{"block": "Brit Plexi Brt"}]}],
        "expression": list(expression_entries),
    })


def test_no_expression_field_yields_empty_list():
    spec = parse_spec({"name": "t", "paths": [{"blocks": []}]})
    assert spec.expression == []


def test_minimal_expression_entry():
    spec = _spec({
        "pedal": "EXP1",
        "targets": [{"block": "Brit Plexi Brt", "param": "Master"}],
    })
    assert len(spec.expression) == 1
    e = spec.expression[0]
    assert e.pedal == "EXP1"
    assert len(e.targets) == 1
    t = e.targets[0]
    assert t.block == "Brit Plexi Brt"
    assert t.param == "Master"
    assert t.min == 0.0
    assert t.max == 1.0


def test_expression_target_with_custom_min_max():
    spec = _spec({
        "pedal": "EXP1",
        "targets": [{"block": "Brit Plexi Brt", "param": "Master", "min": 0.2, "max": 0.8}],
    })
    t = spec.expression[0].targets[0]
    assert t.min == 0.2
    assert t.max == 0.8


def test_expression_target_min_greater_than_max_rejected():
    with pytest.raises(SpecError, match='"min" must be <='):
        _spec({
            "pedal": "EXP1",
            "targets": [{"block": "X", "param": "Y", "min": 0.9, "max": 0.1}],
        })


def test_expression_multi_target():
    spec = _spec({
        "pedal": "EXP1",
        "targets": [
            {"block": "Brit Plexi Brt", "param": "Master"},
            {"block": "Brit Plexi Brt", "param": "Drive"},
        ],
    })
    assert len(spec.expression[0].targets) == 2


def test_expression_duplicate_pedal_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        _spec(
            {"pedal": "EXP1", "targets": [{"block": "A", "param": "P"}]},
            {"pedal": "EXP1", "targets": [{"block": "B", "param": "Q"}]},
        )


def test_expression_duplicate_block_param_across_pedals_rejected():
    with pytest.raises(SpecError, match="duplicate"):
        _spec(
            {"pedal": "EXP1", "targets": [{"block": "A", "param": "P"}]},
            {"pedal": "EXP2", "targets": [{"block": "A", "param": "P"}]},
        )


def test_expression_empty_targets_rejected():
    with pytest.raises(SpecError, match="targets.*non-empty"):
        _spec({"pedal": "EXP1", "targets": []})


def test_expression_missing_pedal_rejected():
    with pytest.raises(SpecError, match='"pedal" is required'):
        _spec({"targets": [{"block": "A", "param": "B"}]})


def test_expression_accepts_native_unit_range():
    spec = parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "X", "param": "Time", "min": -120.0, "max": 1800.0}]}]})
    t = spec.expression[0].targets[0]
    assert t.min == -120.0 and t.max == 1800.0


def test_expression_still_requires_min_le_max():
    with pytest.raises(SpecError):
        parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}],
            "expression": [{"pedal": "EXP1", "targets": [
                {"block": "X", "param": "Time", "min": 5.0, "max": 1.0}]}]})
