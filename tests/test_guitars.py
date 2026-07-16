"""Tests for helixgen.guitars: guitar profiles (Task 11, design §5.2).

Uses the `tmp_home` conftest fixture so `home.guitars_dir()` /
`home.library_dir()` resolve under a tmp dir rather than the real
`~/.helixgen`. Git-touching tests additionally isolate git identity/config
(mirrors tests/test_tone_meta.py's `_isolated_git_env`) and skip if git is
absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from helixgen import guitars, home


@pytest.fixture
def _isolated_git_env(tmp_path, monkeypatch):
    """Prevent any real user git config / global gitignore from leaking in."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _sample_profile() -> guitars.GuitarProfile:
    return guitars.GuitarProfile(
        name="Gibson Les Paul Junior",
        short_name="Les Paul Jr",
        type="guitar",
        active=False,
        pickups="one bridge P-90 (soapbar single-coil)",
        construction=None,
        character_md="P-90 grind; raw rock rhythm.",
        genres=["punk", "garage"],
        controls=[
            guitars.Control(name="volume", kind="knob"),
            guitars.Control(name="tone", kind="knob", notes="no coil split"),
        ],
    )


# ---------------------------------------------------------------------------
# slug property + round-trip
# ---------------------------------------------------------------------------


def test_slug_is_slugified_name():
    p = _sample_profile()
    assert p.slug == "gibson-les-paul-junior"


def test_save_load_round_trip_preserves_all_fields(tmp_home):
    p = _sample_profile()
    guitars.save_profile(p)

    loaded = guitars.load_profile("gibson-les-paul-junior")
    assert loaded.name == "Gibson Les Paul Junior"
    assert loaded.short_name == "Les Paul Jr"
    assert loaded.type == "guitar"
    assert loaded.active is False
    assert loaded.pickups == "one bridge P-90 (soapbar single-coil)"
    assert loaded.construction is None
    assert loaded.character_md == "P-90 grind; raw rock rhythm."
    assert loaded.genres == ["punk", "garage"]
    assert [c.name for c in loaded.controls] == ["volume", "tone"]
    assert loaded.controls[1].notes == "no coil split"
    assert loaded.schema == 1


def test_save_profile_writes_expected_path_and_json_shape(tmp_home):
    guitars.save_profile(_sample_profile())
    path = home.guitars_dir() / "gibson-les-paul-junior.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema"] == 1
    assert data["name"] == "Gibson Les Paul Junior"
    assert data["short_name"] == "Les Paul Jr"
    assert data["controls"][0] == {
        "name": "volume", "kind": "knob", "positions": None, "notes": None,
    }


def test_save_profile_leaves_no_tmp_file(tmp_home):
    guitars.save_profile(_sample_profile())
    assert not list(home.guitars_dir().glob("*.tmp"))


def test_load_all_profiles_empty_when_dir_absent(tmp_home):
    assert guitars.load_all_profiles() == []


def test_load_all_profiles_skips_malformed(tmp_home):
    guitars.save_profile(_sample_profile())
    home.guitars_dir().mkdir(parents=True, exist_ok=True)
    (home.guitars_dir() / "broken.json").write_text("{not json")
    slugs = {p.slug for p in guitars.load_all_profiles()}
    assert slugs == {"gibson-les-paul-junior"}


# ---------------------------------------------------------------------------
# find_profile
# ---------------------------------------------------------------------------


def test_find_profile_by_slug(tmp_home):
    guitars.save_profile(_sample_profile())
    assert guitars.find_profile("gibson-les-paul-junior").short_name == "Les Paul Jr"


def test_find_profile_by_name_case_insensitive(tmp_home):
    guitars.save_profile(_sample_profile())
    assert guitars.find_profile("gibson les paul JUNIOR").slug == "gibson-les-paul-junior"


def test_find_profile_by_short_name_case_insensitive(tmp_home):
    guitars.save_profile(_sample_profile())
    assert guitars.find_profile("les paul jr").slug == "gibson-les-paul-junior"


def test_find_profile_returns_none_for_unknown(tmp_home):
    guitars.save_profile(_sample_profile())
    assert guitars.find_profile("Stratocaster") is None


def test_find_profile_blank_label_is_none(tmp_home):
    guitars.save_profile(_sample_profile())
    assert guitars.find_profile("") is None
    assert guitars.find_profile("   ") is None


def _second_les_paul_jr() -> guitars.GuitarProfile:
    """A DISTINCT profile (different name/slug) that shares the short_name
    "Les Paul Jr" with ``_sample_profile()`` -- the ambiguity trigger."""
    return guitars.GuitarProfile(
        name="Epiphone Les Paul Junior",
        short_name="Les Paul Jr",
        type="guitar",
        active=False,
        pickups="one bridge P-90",
        construction=None,
        character_md="budget P-90.",
        genres=["punk"],
        controls=[guitars.Control(name="volume", kind="knob")],
    )


def test_find_profile_ambiguous_short_name_raises(tmp_home):
    guitars.save_profile(_sample_profile())        # short_name "Les Paul Jr"
    guitars.save_profile(_second_les_paul_jr())    # short_name "Les Paul Jr"
    with pytest.raises(guitars.AmbiguousGuitarError) as exc:
        guitars.find_profile("Les Paul Jr")
    # names the colliding profiles by their (unique) slugs
    assert "epiphone-les-paul-junior" in str(exc.value)
    assert "gibson-les-paul-junior" in str(exc.value)


def test_find_profile_exact_slug_resolves_despite_short_name_collision(tmp_home):
    guitars.save_profile(_sample_profile())
    guitars.save_profile(_second_les_paul_jr())
    # An exact slug is unique by construction -> must still resolve deterministically.
    assert guitars.find_profile("gibson-les-paul-junior").name == "Gibson Les Paul Junior"
    assert guitars.find_profile("epiphone-les-paul-junior").name == "Epiphone Les Paul Junior"


def test_find_profile_exact_name_wins_over_short_name_collision(tmp_home):
    """Most-specific-wins: an exact NAME match resolves even when the label also
    collides with another profile's short_name."""
    guitars.save_profile(_sample_profile())        # short_name "Les Paul Jr"
    # A profile whose *name* is literally "Les Paul Jr".
    guitars.save_profile(guitars.GuitarProfile(
        name="Les Paul Jr", short_name="LPJ", type="guitar", active=None,
        pickups=None, construction=None, character_md=None, genres=[], controls=[]))
    # Name-tier match is unique (only the second profile) -> resolves, no raise.
    assert guitars.find_profile("Les Paul Jr").slug == "les-paul-jr"


def test_find_profile_ambiguous_error_is_valueerror(tmp_home):
    guitars.save_profile(_sample_profile())
    guitars.save_profile(_second_les_paul_jr())
    with pytest.raises(ValueError):
        guitars.find_profile("les paul jr")


# ---------------------------------------------------------------------------
# profile_from_instrument
# ---------------------------------------------------------------------------


def test_profile_from_instrument_maps_fields():
    d = {
        "name": "Fender Stratocaster",
        "type": "guitar",
        "pickups": "three single-coils",
        "selector": "5-way blade",
        "active": True,
        "genres": ["blues", "funk"],
        "notes": "glassy quack; bright single-coils",
    }
    p = guitars.profile_from_instrument(d)
    assert p.name == "Fender Stratocaster"
    assert p.short_name == "Fender Stratocaster"  # YAGNI: default to name
    assert p.type == "guitar"
    assert p.active is True
    assert p.pickups == "three single-coils"
    assert p.construction is None
    assert p.character_md == "glassy quack; bright single-coils"  # notes -> character_md
    assert p.genres == ["blues", "funk"]
    # selector -> synthesized control
    assert p.controls == [
        guitars.Control(name="pickup selector", kind="switch", notes="5-way blade")
    ]


def test_profile_from_instrument_honors_explicit_short_name():
    d = {"name": "Gibson Les Paul Junior", "short_name": "Les Paul Jr", "type": "guitar"}
    p = guitars.profile_from_instrument(d)
    assert p.short_name == "Les Paul Jr"


def test_profile_from_instrument_no_selector_no_controls():
    d = {"name": "Some Guitar", "type": "guitar"}
    p = guitars.profile_from_instrument(d)
    assert p.controls == []
    assert p.character_md is None


# ---------------------------------------------------------------------------
# git integration (advisory auto-commit)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available on PATH")
def test_save_profile_produces_a_commit(tmp_home, _isolated_git_env):
    guitars.save_profile(_sample_profile())
    log = subprocess.run(
        ["git", "-C", str(home.helixgen_home()), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert log.strip() != ""
