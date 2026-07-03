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


def test_expression_target_inverted_range_accepted():
    # FIX A: Real presets use inverted min/max (heel=high, toe=low). Must be accepted.
    spec = _spec({
        "pedal": "EXP1",
        "targets": [{"block": "X", "param": "Y", "min": 0.85, "max": 0.67}],
    })
    t = spec.expression[0].targets[0]
    assert t.min == 0.85
    assert t.max == 0.67


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


def test_expression_inverted_range_with_wide_native_units_accepted():
    # FIX A: Inverted native-unit range (e.g. ms / Hz sweeps reversed). Must be accepted.
    spec = parse_spec({"name": "n", "paths": [{"blocks": [{"block": "X"}]}],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "X", "param": "Time", "min": 1800.0, "max": 5.0}]}]})
    t = spec.expression[0].targets[0]
    assert t.min == 1800.0 and t.max == 5.0


# ---------------------------------------------------------------------------
# FIX C — coordinate-aware duplicate-target check
# ---------------------------------------------------------------------------

def test_expression_same_name_different_pos_accepted():
    """FIX C: two same-name blocks at different positions can each have an EXP target."""
    spec = parse_spec({
        "name": "dup-pos",
        "paths": [{"blocks": [
            {"block": "Tube Drive", "pos": 1},
            {"block": "Tube Drive", "pos": 2},
        ]}],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "Tube Drive", "param": "Gain", "pos": 1},
            {"block": "Tube Drive", "param": "Gain", "pos": 2},
        ]}],
    })
    assert len(spec.expression[0].targets) == 2


def test_expression_exact_same_coordinate_still_rejected():
    """FIX C: exact same (block, param, pos) across the spec is still rejected."""
    with pytest.raises(SpecError, match="duplicate"):
        parse_spec({
            "name": "dup-pos",
            "paths": [{"blocks": [{"block": "Tube Drive", "pos": 1}]}],
            "expression": [
                {"pedal": "EXP1", "targets": [{"block": "Tube Drive", "param": "Gain", "pos": 1}]},
                {"pedal": "EXP2", "targets": [{"block": "Tube Drive", "param": "Gain", "pos": 1}]},
            ],
        })
