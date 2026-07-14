"""Parse-level tests for spec input-mode validation."""
import pytest

from helixgen.spec import SpecError, parse_spec


def _minimal_spec(**path0_extra):
    return {
        "name": "test",
        "paths": [{"blocks": [], **path0_extra}],
    }


def test_input_omitted_leaves_path_input_none():
    spec = parse_spec(_minimal_spec())
    assert spec.paths[0].input is None


def test_input_valid_modes_accepted():
    for mode in ("inst1", "inst2", "both", "none"):
        spec = parse_spec(_minimal_spec(input=mode))
        assert spec.paths[0].input == mode


def test_input_non_string_rejected():
    with pytest.raises(SpecError, match='"input" must be a mode string or an object'):
        parse_spec(_minimal_spec(input=42))


def test_input_unknown_string_rejected_with_valid_list():
    with pytest.raises(SpecError) as exc_info:
        parse_spec(_minimal_spec(input="aux"))
    msg = str(exc_info.value)
    assert "'aux'" in msg
    assert "inst1" in msg and "both" in msg
