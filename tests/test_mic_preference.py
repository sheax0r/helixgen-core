"""The stored mic-input preference: parsing, and its injection into a recipe.

The preference is a DEFAULT. Every test that asserts it backs off is guarding
the same thing — a user who sings should not silently lose a dual-amp rig.
"""
import json

import pytest

from helixgen.preferences import (
    MicInput, PreferencesError, _parse_mic_input, load_preferences,
)
from helixgen.recipe import apply_mic_preference


# --- parsing ---------------------------------------------------------------

def test_absent_block_is_disabled():
    assert _parse_mic_input(None) == MicInput()
    assert _parse_mic_input(None).enabled is False


def test_full_block_parses():
    m = _parse_mic_input({"enabled": True, "path": 1, "lowcut": 80.0,
                          "trim": -2.0, "gate": True, "threshold": -45.0,
                          "decay": 0.35, "level": 3.5})
    assert (m.enabled, m.path, m.lowcut, m.threshold, m.level) == (
        True, 1, 80.0, -45.0, 3.5)


def test_unknown_key_is_refused_by_name():
    with pytest.raises(PreferencesError, match="phantom"):
        _parse_mic_input({"enabled": True, "phantom": True})


@pytest.mark.parametrize("field,value", [
    ("lowcut", 500.0),      # > 400 Hz
    ("lowcut", 10.0),       # < 19.9 Hz
    ("trim", 12.0),         # > +6 dB
    ("threshold", 5.0),     # > 0 dB
    ("decay", 2.0),         # > 1
    ("level", 40.0),        # > +20 dB
])
def test_out_of_range_values_are_refused(field, value):
    with pytest.raises(PreferencesError, match=field):
        _parse_mic_input({"enabled": True, field: value})


def test_path_must_be_a_non_negative_int():
    with pytest.raises(PreferencesError, match="path"):
        _parse_mic_input({"enabled": True, "path": -1})
    with pytest.raises(PreferencesError, match="path"):
        _parse_mic_input({"enabled": True, "path": "1"})


def test_loads_from_a_prefs_file(tmp_path):
    p = tmp_path / "preferences.json"
    p.write_text(json.dumps({"mic_input": {"enabled": True, "level": 3.5}}))
    assert load_preferences(p).mic_input.level == 3.5


def test_input_field_omits_unset_params():
    """An unset param must stay absent so the model default applies, rather
    than being written as a zero."""
    assert MicInput(enabled=True).input_field() == {"source": "mic"}


# --- injection -------------------------------------------------------------

def _mic(**kw):
    return MicInput(enabled=True, **kw)


def test_injects_into_a_free_path():
    r = {"name": "t", "paths": [{"blocks": []}]}
    assert apply_mic_preference(r, _mic(level=3.5)) == []
    assert r["paths"][1]["input"] == {"source": "mic"}
    assert r["paths"][1]["output"]["level"] == 3.5


def test_disabled_preference_changes_nothing():
    r = {"name": "t", "paths": [{"blocks": []}]}
    assert apply_mic_preference(r, MicInput()) == []
    assert len(r["paths"]) == 1


def test_none_preference_changes_nothing():
    r = {"name": "t", "paths": [{"blocks": []}]}
    assert apply_mic_preference(r, None) == []
    assert len(r["paths"]) == 1


def test_an_explicit_recipe_input_wins():
    r = {"paths": [{"blocks": []}, {"blocks": [], "input": "inst2"}]}
    why = apply_mic_preference(r, _mic())
    assert why and "recipe wins" in why[0]
    assert r["paths"][1]["input"] == "inst2"


def test_an_occupied_path_is_left_alone():
    """The dual-amp case: re-jacking this path's input silences the amp."""
    r = {"paths": [{"blocks": []}, {"blocks": [{"block": "Brit 2204"}]}]}
    why = apply_mic_preference(r, _mic())
    assert why and "in use" in why[0]
    assert "input" not in r["paths"][1]


def test_a_path_fed_by_another_path_is_not_free():
    r = {"paths": [{"blocks": [], "output": {"to": "path2a"}}, {"blocks": []}]}
    why = apply_mic_preference(r, _mic())
    assert why and "routes its output" in why[0]
    assert "input" not in r["paths"][1]


def test_an_explicit_output_level_is_not_overwritten():
    r = {"paths": [{"blocks": []}, {"blocks": [], "output": {"level": -6.0}}]}
    apply_mic_preference(r, _mic(level=3.5))
    assert r["paths"][1]["output"]["level"] == -6.0


def test_a_short_recipe_grows_to_reach_the_mic_path():
    r = {"paths": [{"blocks": []}]}
    apply_mic_preference(r, _mic())
    assert len(r["paths"]) == 2
    assert r["paths"][1]["input"]["source"] == "mic"


def test_params_reach_the_recipe_input_object():
    r = {"paths": [{"blocks": []}]}
    apply_mic_preference(r, _mic(lowcut=80.0, gate=True, threshold=-45.0,
                                 decay=0.35))
    assert r["paths"][1]["input"] == {
        "source": "mic", "lowcut": 80.0,
        "gate": {"enabled": True, "threshold": -45.0, "decay": 0.35},
    }
