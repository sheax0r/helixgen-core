"""Tests for `helixgen generate` writing into the library (Task 7).

Two flows:
  * `-o OUT`  -> legacy behavior: write there, auto-register, NO metadata JSON.
  * no `-o`   -> default library write: resolve naming (flags win; else recipe
                `name` -> descriptor), write `tones_dir()/<variant>.hsp` with
                `meta.name = preset_name`, upsert the logical tone JSON, register
                in the manifest, advisory git-commit.

Uses the `tmp_home` conftest fixture so `home.tones_dir()` / `home.library_dir()`
/ `home.manifest_path()` resolve under a tmp dir rather than the real
`~/.helixgen`, and the `hsp_library` fixture (a Stadium `.hsp` chassis + two
synthetic blocks). Git-touching assertions additionally isolate git identity and
skip when git is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen import guitars, home, tone_meta
from helixgen.cli import cli
from helixgen.device.manifest import SetlistManifest
from helixgen.hsp import read_hsp


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path, monkeypatch):
    """Prevent any real user git config / global gitignore from leaking in."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _write_recipe(tmp_path: Path, name: str = "Recipe Title", block: str = "Brit Amp") -> Path:
    spec = tmp_path / "recipe.json"
    spec.write_text(json.dumps(
        {"name": name, "paths": [{"blocks": [{"block": block}]}]}))
    return spec


def _run(hsp_library, *args) -> object:
    return CliRunner().invoke(
        cli, ["generate", *args, "--library", str(hsp_library.root)])


# ---------------------------------------------------------------------------
# default library write (no -o)
# ---------------------------------------------------------------------------


def test_default_write_creates_hsp_json_and_manifest_entry(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec),
               "--descriptor", "Warm Jazz Clean", "--guitar", "Les Paul Jr")
    assert res.exit_code == 0, res.output

    variant_hsp = home.tones_dir() / "warm-jazz-clean-les-paul-jr.hsp"
    assert variant_hsp.exists()
    assert read_hsp(variant_hsp)["meta"]["name"] == "Warm Jazz Clean - Les Paul Jr"

    # logical JSON keyed by the logical slug, one variant keyed by the guitar slug
    logical_json = home.tones_dir() / "warm-jazz-clean.json"
    assert logical_json.exists()
    meta = tone_meta.load_tone_meta("warm-jazz-clean")
    assert set(meta.variants) == {"les-paul-jr"}
    assert meta.descriptor == "Warm Jazz Clean"
    assert meta.variants["les-paul-jr"].preset_name == "Warm Jazz Clean - Les Paul Jr"

    # registered in the manifest under the preset display name
    assert "Warm Jazz Clean - Les Paul Jr" in SetlistManifest.load().tones

    # stdout reports path + preset name + logical slug
    assert "warm-jazz-clean-les-paul-jr.hsp" in res.output
    assert "Warm Jazz Clean - Les Paul Jr" in res.output
    assert "warm-jazz-clean" in res.output


def test_default_write_produces_a_commit(tmp_home, hsp_library, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available on PATH")
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec),
               "--descriptor", "Warm Jazz Clean", "--guitar", "Les Paul Jr")
    assert res.exit_code == 0, res.output

    log = subprocess.run(
        ["git", "-C", str(home.helixgen_home()), "log", "--oneline"],
        capture_output=True, text=True)
    assert log.returncode == 0, log.stderr
    assert log.stdout.strip(), "expected at least one commit in the home repo"
    # nothing left dangling: the hsp + json + manifest are all committed
    status = subprocess.run(
        ["git", "-C", str(home.helixgen_home()), "status", "--porcelain"],
        capture_output=True, text=True)
    assert status.stdout.strip() == "", f"uncommitted files: {status.stdout!r}"


def test_recipe_name_derives_descriptor_when_no_flags(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path, name="Recipe Title")
    res = _run(hsp_library, str(spec))
    assert res.exit_code == 0, res.output

    variant_hsp = home.tones_dir() / "recipe-title.hsp"
    assert variant_hsp.exists()
    assert read_hsp(variant_hsp)["meta"]["name"] == "Recipe Title"

    meta = tone_meta.load_tone_meta("recipe-title")
    assert meta.descriptor == "Recipe Title"
    assert set(meta.variants) == {"generic"}


def test_second_variant_adds_to_same_logical_json(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path, name="R")
    r1 = _run(hsp_library, str(spec),
              "--artist", "A", "--song", "S", "--guitar", "Les Paul Jr")
    assert r1.exit_code == 0, r1.output
    r2 = _run(hsp_library, str(spec),
              "--artist", "A", "--song", "S", "--guitar", "Other Guitar")
    assert r2.exit_code == 0, r2.output

    # one logical file, two variant keys
    logical = "a-s"
    metas = list(home.tones_dir().glob("*.json"))
    assert [p.name for p in metas] == [f"{logical}.json"]
    meta = tone_meta.load_tone_meta(logical)
    assert set(meta.variants) == {"les-paul-jr", "other-guitar"}
    assert (home.tones_dir() / "a-s-les-paul-jr.hsp").exists()
    assert (home.tones_dir() / "a-s-other-guitar.hsp").exists()


# ---------------------------------------------------------------------------
# identity validation errors
# ---------------------------------------------------------------------------


def test_song_and_descriptor_together_errors(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec),
               "--artist", "A", "--song", "S", "--descriptor", "D")
    assert res.exit_code != 0
    if home.tones_dir().exists():
        assert not list(home.tones_dir().glob("*.hsp"))


def test_artist_without_song_errors(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec), "--artist", "A")
    assert res.exit_code != 0


# ---------------------------------------------------------------------------
# slug collision
# ---------------------------------------------------------------------------


def test_slug_collision_errors_and_does_not_overwrite(tmp_home, hsp_library, tmp_path):
    spec1 = _write_recipe(tmp_path, name="R", block="Brit Amp")
    r1 = _run(hsp_library, str(spec1), "--descriptor", "Warm Jazz Clean")
    assert r1.exit_code == 0, r1.output
    target = home.tones_dir() / "warm-jazz-clean.hsp"
    original = target.read_bytes()

    # a different recipe, same naming -> collides
    spec2 = _write_recipe(tmp_path, name="R", block="Tube Drive")
    r2 = _run(hsp_library, str(spec2), "--descriptor", "Warm Jazz Clean")
    assert r2.exit_code != 0
    assert "exists" in r2.output.lower() or "rename" in r2.output.lower()
    # the existing file is untouched
    assert target.read_bytes() == original


# ---------------------------------------------------------------------------
# -o legacy path unchanged: writes there, no metadata JSON
# ---------------------------------------------------------------------------


def test_dash_o_writes_exactly_there_and_no_metadata(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    out = tmp_path / "explicit_out.hsp"
    res = _run(hsp_library, str(spec), "-o", str(out))
    assert res.exit_code == 0, res.output
    assert out.exists()
    # auto-registered in the manifest (keyed by the recipe name)
    assert "Recipe Title" in SetlistManifest.load().tones
    # NO metadata JSON written under tones_dir
    if home.tones_dir().exists():
        assert list(home.tones_dir().glob("*.json")) == []


def test_dash_o_with_naming_flags_still_writes_no_metadata(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    out = tmp_path / "explicit_out.hsp"
    res = _run(hsp_library, str(spec), "-o", str(out),
               "--descriptor", "Warm Jazz Clean", "--guitar", "Les Paul Jr")
    assert res.exit_code == 0, res.output
    assert out.exists()
    if home.tones_dir().exists():
        assert list(home.tones_dir().glob("*.json")) == []


# ---------------------------------------------------------------------------
# adversarial-review fixes (PR 2)
# ---------------------------------------------------------------------------


def test_empty_slug_guitar_errors_and_writes_no_orphan(tmp_home, hsp_library, tmp_path):
    """I-1: a --guitar label that slugs to nothing (punctuation-only) must be
    rejected as a clean ClickException (exit 1) BEFORE any .hsp is written --
    previously it wrote the file then raised an uncaught ValueError."""
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec), "--descriptor", "Warm Jazz Clean",
               "--guitar", "---")
    assert res.exit_code == 1, res.output
    # clean error, not a traceback
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "slug" in res.output.lower()
    # no orphan .hsp left behind in the tones dir
    if home.tones_dir().exists():
        assert list(home.tones_dir().glob("*.hsp")) == []
        assert list(home.tones_dir().glob("*.json")) == []


def test_logical_slug_collision_between_distinct_identities_errors(
    tmp_home, hsp_library, tmp_path
):
    """I-2: two DISTINCT identities can share a logical slug but differ by
    guitar variant. The second generate must error (exit 1), must NOT mutate
    the first tone's JSON, and must leave no orphan .hsp."""
    spec = _write_recipe(tmp_path, name="R")
    # First identity: artist="A-B", song="C" -> logical slug "a-b-c"
    r1 = _run(hsp_library, str(spec),
              "--artist", "A-B", "--song", "C", "--guitar", "Strat")
    assert r1.exit_code == 0, r1.output
    logical_json = home.tones_dir() / "a-b-c.json"
    assert logical_json.exists()
    before = logical_json.read_text()

    # Second, DISTINCT identity: artist="A", song="B-C" -> same slug "a-b-c",
    # different guitar variant "tele" so the .hsp collision guard won't fire.
    r2 = _run(hsp_library, str(spec),
              "--artist", "A", "--song", "B-C", "--guitar", "Tele")
    assert r2.exit_code == 1, r2.output
    assert "collision" in r2.output.lower() or "different" in r2.output.lower()

    # the first tone's JSON is unchanged: still only the strat variant, identity intact
    assert logical_json.read_text() == before
    meta = tone_meta.load_tone_meta("a-b-c")
    assert set(meta.variants) == {"strat"}
    assert meta.artist == "A-B" and meta.song == "C"
    # no -tele.hsp orphan
    assert not (home.tones_dir() / "a-b-c-tele.hsp").exists()


def test_empty_logical_slug_errors_no_dotfiles(tmp_home, hsp_library, tmp_path):
    """M-2: an identity that slugs to nothing (emoji-only descriptor) must
    error instead of writing dotfiles literally named '.hsp'/'.json'."""
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec), "--descriptor", "\U0001f3b8")
    assert res.exit_code == 1, res.output
    assert "slug" in res.output.lower()
    if home.tones_dir().exists():
        names = [p.name for p in home.tones_dir().iterdir()]
        assert ".hsp" not in names
        assert ".json" not in names
        assert not any(n.startswith(".") for n in names)


def test_second_variant_same_identity_still_succeeds(tmp_home, hsp_library, tmp_path):
    """Regression: the legitimate case (SAME artist/song, different guitar)
    must still add a variant to the same logical JSON."""
    spec = _write_recipe(tmp_path, name="R")
    r1 = _run(hsp_library, str(spec),
              "--artist", "A", "--song", "S", "--guitar", "Les Paul Jr")
    assert r1.exit_code == 0, r1.output
    r2 = _run(hsp_library, str(spec),
              "--artist", "A", "--song", "S", "--guitar", "Other Guitar")
    assert r2.exit_code == 0, r2.output
    meta = tone_meta.load_tone_meta("a-s")
    assert set(meta.variants) == {"les-paul-jr", "other-guitar"}


# ---------------------------------------------------------------------------
# --guitar resolves against guitar profiles (Task 11)
# ---------------------------------------------------------------------------


def _save_lp_profile():
    guitars.save_profile(guitars.GuitarProfile(
        name="Gibson Les Paul Junior", short_name="Les Paul Jr", type="guitar",
        active=None, pickups=None, construction=None, character_md=None,
        genres=[], controls=[],
    ))


def test_generate_guitar_resolves_profile_short_name(tmp_home, hsp_library, tmp_path):
    _save_lp_profile()
    spec = _write_recipe(tmp_path)
    # match by profile NAME; the display name must use the profile's short_name
    res = _run(hsp_library, str(spec), "--descriptor", "Warm Jazz Clean",
               "--guitar", "Gibson Les Paul Junior")
    assert res.exit_code == 0, res.output

    variant_hsp = home.tones_dir() / "warm-jazz-clean-gibson-les-paul-junior.hsp"
    assert variant_hsp.exists()
    meta = tone_meta.load_tone_meta("warm-jazz-clean")
    assert set(meta.variants) == {"gibson-les-paul-junior"}
    assert meta.variants["gibson-les-paul-junior"].preset_name == "Warm Jazz Clean - Les Paul Jr"


def test_generate_unknown_guitar_errors_when_profiles_exist(tmp_home, hsp_library, tmp_path):
    _save_lp_profile()
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec), "--descriptor", "Warm Jazz Clean",
               "--guitar", "Stratocaster")
    assert res.exit_code != 0
    assert "unknown guitar" in (res.output + res.stderr).lower()
    # nothing written
    assert not list(home.tones_dir().glob("*.hsp")) if home.tones_dir().exists() else True


def test_generate_ambiguous_guitar_errors_exit1_no_traceback(tmp_home, hsp_library, tmp_path):
    """Two profiles sharing a short_name -> --guitar by that short_name is a
    clean ClickException (exit 1), never a silently-picked wrong guitar."""
    _save_lp_profile()  # short_name "Les Paul Jr"
    guitars.save_profile(guitars.GuitarProfile(
        name="Epiphone Les Paul Junior", short_name="Les Paul Jr", type="guitar",
        active=None, pickups=None, construction=None, character_md=None,
        genres=[], controls=[]))
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec), "--descriptor", "Warm Jazz Clean",
               "--guitar", "Les Paul Jr")
    assert res.exit_code == 1
    combined = (res.output + res.stderr).lower()
    assert "ambiguous" in combined
    assert "exact guitar slug" in combined
    assert res.exception is None or isinstance(res.exception, SystemExit)
    # nothing written
    assert not list(home.tones_dir().glob("*.hsp")) if home.tones_dir().exists() else True


def test_generate_guitar_literal_fallback_when_no_profiles(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    res = _run(hsp_library, str(spec), "--descriptor", "Warm Jazz Clean",
               "--guitar", "Les Paul Jr")
    assert res.exit_code == 0, res.output
    # literal slugify fallback used; a one-line notice went to stderr
    assert (home.tones_dir() / "warm-jazz-clean-les-paul-jr.hsp").exists()
    assert "no guitar profiles exist yet" in res.stderr
