"""Tests for helixgen.preferences: load/env-precedence/scaffold."""
import json
import sys
from pathlib import Path

import pytest

from helixgen.preferences import (
    Instrument,
    Preferences,
    PreferencesError,
    default_prefs_path,
    load_preferences,
    scaffold_default,
)


# ---------------------------------------------------------------------------
# default_prefs_path
# ---------------------------------------------------------------------------


def test_default_prefs_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    assert default_prefs_path() == Path("/tmp/fake-home/.helixgen/preferences.json")


def test_default_prefs_path_honors_env_var(monkeypatch):
    monkeypatch.setenv("HELIXGEN_PREFS", "/custom/prefs.json")
    assert default_prefs_path() == Path("/custom/prefs.json")


# ---------------------------------------------------------------------------
# load_preferences: defaults, no file
# ---------------------------------------------------------------------------


def test_load_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    for key in (
        "HELIXGEN_DEVICE_MODEL",
        "HELIXGEN_FAVOR_IRS",
        "HELIXGEN_REVEAL_IN_FINDER",
        "HELIXGEN_GUARD_PAID_IRS",
        "HELIXGEN_PRESET_DIR",
        "HELIXGEN_AUTHOR",
        "HELIXGEN_DEFAULT_GUITAR",
    ):
        monkeypatch.delenv(key, raising=False)

    missing = tmp_path / "does-not-exist.json"
    prefs = load_preferences(missing)

    assert prefs.schema_version == 1
    assert prefs.device_model is None
    assert prefs.favor_irs is False
    assert prefs.guard_paid_irs_in_git is True
    assert prefs.preset_output_dir is None
    assert prefs.author is None
    assert prefs.default_guitar is None
    assert prefs.instruments == []


def test_load_reveal_in_finder_platform_derived_default_darwin(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_REVEAL_IN_FINDER", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    missing = tmp_path / "does-not-exist.json"
    prefs = load_preferences(missing)
    assert prefs.reveal_in_finder is True


def test_load_reveal_in_finder_platform_derived_default_linux(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_REVEAL_IN_FINDER", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    missing = tmp_path / "does-not-exist.json"
    prefs = load_preferences(missing)
    assert prefs.reveal_in_finder is False


# ---------------------------------------------------------------------------
# load_preferences: file values
# ---------------------------------------------------------------------------


def test_load_reads_file_values(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_FAVOR_IRS", raising=False)
    monkeypatch.delenv("HELIXGEN_DEVICE_MODEL", raising=False)
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "device": {"model": "Stadium XL"},
                "favor_irs": True,
                "reveal_in_finder": False,
                "guard_paid_irs_in_git": False,
                "preset_output_dir": "~/presets",
                "author": "mike",
                "instruments": [],
            }
        )
    )
    prefs = load_preferences(path)
    assert prefs.device_model == "Stadium XL"
    assert prefs.favor_irs is True
    assert prefs.reveal_in_finder is False
    assert prefs.guard_paid_irs_in_git is False
    assert prefs.preset_output_dir == "~/presets"
    assert prefs.author == "mike"


def test_load_reads_default_guitar(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_GUITAR", raising=False)
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"default_guitar": "Les Paul Jr"}))
    prefs = load_preferences(path)
    assert prefs.default_guitar == "Les Paul Jr"


def test_default_guitar_non_string_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEFAULT_GUITAR", raising=False)
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"default_guitar": 123}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


def test_env_default_guitar_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEFAULT_GUITAR", "EC-1000")
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"default_guitar": "Les Paul Jr"}))
    prefs = load_preferences(path)
    assert prefs.default_guitar == "EC-1000"


def test_default_guitar_in_scaffold(tmp_path):
    path = scaffold_default(tmp_path / "preferences.json")
    data = json.loads(path.read_text())
    assert data["default_guitar"] is None


def test_load_unknown_key_tolerated(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"schema_version": 1, "some_future_key": "x"}))
    prefs = load_preferences(path)
    assert prefs.schema_version == 1


# ---------------------------------------------------------------------------
# env override beats file
# ---------------------------------------------------------------------------


def test_env_override_beats_file(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"favor_irs": False}))
    monkeypatch.setenv("HELIXGEN_FAVOR_IRS", "1")
    prefs = load_preferences(path)
    assert prefs.favor_irs is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("0", False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("False", False),
        ("yes", True),
        ("no", False),
        ("YES", True),
        ("NO", False),
    ],
)
def test_env_bool_parsing_valid(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("HELIXGEN_FAVOR_IRS", raw)
    prefs = load_preferences(tmp_path / "missing.json")
    assert prefs.favor_irs is expected


def test_env_bool_parsing_typo_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_FAVOR_IRS", "ture")
    with pytest.raises(PreferencesError):
        load_preferences(tmp_path / "missing.json")


def test_env_device_model_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_DEVICE_MODEL", "Stadium")
    prefs = load_preferences(tmp_path / "missing.json")
    assert prefs.device_model == "Stadium"


def test_env_author_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_AUTHOR", "env-author")
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"author": "file-author"}))
    prefs = load_preferences(path)
    assert prefs.author == "env-author"


def test_env_preset_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_PRESET_DIR", "/env/dir")
    prefs = load_preferences(tmp_path / "missing.json")
    assert prefs.preset_output_dir == "/env/dir"


# ---------------------------------------------------------------------------
# malformed JSON
# ---------------------------------------------------------------------------


def test_malformed_json_raises_clear_error(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text("{not valid json")
    with pytest.raises(PreferencesError) as exc_info:
        load_preferences(path)
    assert str(path) in str(exc_info.value)


def test_missing_explicit_helixgen_prefs_file_raises(tmp_path, monkeypatch):
    missing = tmp_path / "explicit-missing.json"
    monkeypatch.setenv("HELIXGEN_PREFS", str(missing))
    with pytest.raises(PreferencesError):
        load_preferences()


def test_missing_default_path_when_path_none_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    prefs = load_preferences()
    assert prefs.favor_irs is False


# ---------------------------------------------------------------------------
# scaffold_default
# ---------------------------------------------------------------------------


def test_scaffold_writes_default_file(tmp_path):
    path = tmp_path / "preferences.json"
    result = scaffold_default(path)
    assert result == path
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert "_comment" in data
    assert data["favor_irs"] is False
    assert data["instruments"] == []


def test_scaffold_is_idempotent(tmp_path):
    path = tmp_path / "preferences.json"
    scaffold_default(path)
    on_disk_first = path.read_text()
    result = scaffold_default(path)
    assert result == path
    assert path.read_text() == on_disk_first


def test_scaffold_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "preferences.json"
    scaffold_default(path)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_scaffold_default_path_honors_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = scaffold_default()
    assert result == tmp_path / ".helixgen" / "preferences.json"
    assert result.exists()


# ---------------------------------------------------------------------------
# instruments round-trip
# ---------------------------------------------------------------------------


def test_instruments_round_trip(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "instruments": [
                    {
                        "name": "Gibson Les Paul Junior",
                        "type": "guitar",
                        "pickups": "one bridge P-90",
                        "selector": "none",
                        "genres": ["punk", "garage"],
                        "notes": "breaks up early",
                    },
                    {
                        "name": "ESP LTD EC-1000",
                        "type": "guitar",
                        "pickups": "2 humbuckers",
                        "selector": "3-way",
                        "active": True,
                        "genres": ["modern metal"],
                    },
                ]
            }
        )
    )
    prefs = load_preferences(path)
    assert len(prefs.instruments) == 2
    assert isinstance(prefs.instruments[0], Instrument)
    assert prefs.instruments[0].name == "Gibson Les Paul Junior"
    assert prefs.instruments[0].type == "guitar"
    assert prefs.instruments[0].selector == "none"
    assert prefs.instruments[0].genres == ["punk", "garage"]
    assert prefs.instruments[0].notes == "breaks up early"
    assert prefs.instruments[1].active is True


def test_instruments_validates_list_of_objects(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"instruments": ["not-an-object"]}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


def test_instruments_requires_name_and_type(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"instruments": [{"pickups": "no name here"}]}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


def test_instruments_not_list_raises(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"instruments": "not-a-list"}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


# ---------------------------------------------------------------------------
# $HELIXGEN_PREFS redirect
# ---------------------------------------------------------------------------


def test_helixgen_prefs_env_redirects_load(tmp_path, monkeypatch):
    custom = tmp_path / "somewhere" / "custom-prefs.json"
    custom.parent.mkdir()
    custom.write_text(json.dumps({"favor_irs": True}))
    monkeypatch.setenv("HELIXGEN_PREFS", str(custom))
    prefs = load_preferences()
    assert prefs.favor_irs is True


def test_helixgen_prefs_env_redirects_scaffold(tmp_path, monkeypatch):
    custom = tmp_path / "somewhere" / "custom-prefs.json"
    monkeypatch.setenv("HELIXGEN_PREFS", str(custom))
    result = scaffold_default()
    assert result == custom
    assert custom.exists()


# ---------------------------------------------------------------------------
# device.model validation
# ---------------------------------------------------------------------------


def test_device_model_invalid_enum_raises(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"device": {"model": "Not A Real Device"}}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


def test_device_model_null_is_unset(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"device": {"model": None}}))
    prefs = load_preferences(path)
    assert prefs.device_model is None


@pytest.mark.parametrize(
    "raw",
    ["stadium_xl", "STADIUM XL", "stadium-xl", "Stadium XL"],
)
def test_device_model_xl_forms_normalize_to_display(tmp_path, raw):
    """Both MCP-token and display forms (any case/separator) load and normalize."""
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"device": {"model": raw}}))
    prefs = load_preferences(path)
    assert prefs.device_model == "Stadium XL"


@pytest.mark.parametrize("raw", ["stadium", "Stadium"])
def test_device_model_base_forms_normalize_to_display(tmp_path, raw):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"device": {"model": raw}}))
    prefs = load_preferences(path)
    assert prefs.device_model == "Stadium"


def test_device_model_genuinely_invalid_still_raises(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"device": {"model": "helix_lt"}}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


# ---------------------------------------------------------------------------
# volume-normalization opt-out keys
# ---------------------------------------------------------------------------


def test_volume_normalize_defaults_true(tmp_path):
    prefs = load_preferences(tmp_path / "nope.json")  # missing file -> defaults
    assert prefs.volume_normalize_snapshots is True
    assert prefs.volume_normalize_baseline is True


def test_volume_normalize_from_file(tmp_path):
    p = tmp_path / "preferences.json"
    p.write_text(json.dumps({
        "volume_normalize_snapshots": False,
        "volume_normalize_baseline": False,
    }))
    prefs = load_preferences(p)
    assert prefs.volume_normalize_snapshots is False
    assert prefs.volume_normalize_baseline is False


def test_volume_normalize_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / "preferences.json"
    p.write_text(json.dumps({"volume_normalize_snapshots": True}))
    monkeypatch.setenv("HELIXGEN_VOLUME_NORMALIZE_SNAPSHOTS", "0")
    monkeypatch.delenv("HELIXGEN_VOLUME_NORMALIZE_BASELINE", raising=False)
    prefs = load_preferences(p)
    assert prefs.volume_normalize_snapshots is False   # env beat the file
    assert prefs.volume_normalize_baseline is True      # default


def test_volume_normalize_in_scaffold(tmp_path):
    path = scaffold_default(tmp_path / "preferences.json")
    data = json.loads(path.read_text())
    assert data["volume_normalize_snapshots"] is True
    assert data["volume_normalize_baseline"] is True


# ---------------------------------------------------------------------------
# git_commit_tones (#26 skill git-commit key)
# ---------------------------------------------------------------------------


def test_git_commit_tones_defaults_auto(tmp_path):
    prefs = load_preferences(tmp_path / "nope.json")  # missing file -> defaults
    assert prefs.git_commit_tones == "auto"


def test_git_commit_tones_in_scaffold(tmp_path):
    path = scaffold_default(tmp_path / "preferences.json")
    data = json.loads(path.read_text())
    assert data["git_commit_tones"] == "auto"


def test_git_commit_tones_null_is_unset(tmp_path):
    """JSON null (explicit "no override") defaults to "auto", matching the
    device.model: null-is-unset convention elsewhere in this module."""
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"git_commit_tones": None}))
    prefs = load_preferences(path)
    assert prefs.git_commit_tones == "auto"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("auto", "auto"),
        ("AUTO", "auto"),
        (True, "true"),
        (False, "false"),
        (1, "true"),
        (0, "false"),
        ("true", "true"),
        ("false", "false"),
        ("1", "true"),
        ("0", "false"),
        ("yes", "true"),
        ("no", "false"),
    ],
)
def test_git_commit_tones_from_file(tmp_path, raw, expected):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"git_commit_tones": raw}))
    prefs = load_preferences(path)
    assert prefs.git_commit_tones == expected


def test_git_commit_tones_invalid_value_raises(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"git_commit_tones": "sometimes"}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


def test_git_commit_tones_out_of_range_int_raises(tmp_path):
    """Bare ints are accepted only as the boolean shorthand 1/0."""
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"git_commit_tones": 123}))
    with pytest.raises(PreferencesError):
        load_preferences(path)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("auto", "auto"),
        ("true", "true"),
        ("false", "false"),
        ("1", "true"),
        ("0", "false"),
    ],
)
def test_env_git_commit_tones_override(tmp_path, monkeypatch, raw, expected):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"git_commit_tones": "false"}))
    monkeypatch.setenv("HELIXGEN_GIT_COMMIT_TONES", raw)
    prefs = load_preferences(path)
    assert prefs.git_commit_tones == expected


def test_env_git_commit_tones_invalid_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_GIT_COMMIT_TONES", "sometimes")
    with pytest.raises(PreferencesError):
        load_preferences(tmp_path / "missing.json")
