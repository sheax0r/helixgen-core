"""Tests for helixgen.tone_meta: per-tone JSON metadata (Task 6, design §5.1).

Uses the `tmp_home` conftest fixture so `home.tones_dir()` / `home.library_dir()`
resolve under a tmp dir rather than the real `~/.helixgen`. Git-touching tests
additionally isolate git identity/config (mirrors `tests/test_gitops.py`'s
`_isolated_git_env`) and skip the whole test if git isn't on PATH.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from helixgen import guitars, home, tone_meta
from helixgen.device.manifest import SetlistManifest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_git_env(tmp_path, monkeypatch):
    """Prevent any real user git config / global gitignore from leaking in."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _valid_meta_and_manifest(tmp_home: Path):
    """A ToneMeta with one variant whose .hsp actually exists on disk, plus a
    manifest that already has the variant's preset_name registered -- i.e. a
    fully-valid pairing for `validate_tone_meta`."""
    hsp_rel = "tones/a-b-g1.hsp"
    hsp_abs = home.library_dir() / hsp_rel
    hsp_abs.parent.mkdir(parents=True, exist_ok=True)
    hsp_abs.write_text("fake hsp content")

    meta = tone_meta.ToneMeta(
        artist="A", song="B", descriptor=None,
        tags=[], description_md=None,
        variants={"g1": tone_meta.Variant(hsp=hsp_rel, preset_name="A - B - G1")},
        created="2020-01-01", updated="2020-01-01",
    )
    manifest = SetlistManifest(home.manifest_path())
    manifest.tones["A - B - G1"] = {
        "path": None, "content_hash": None, "source": "authored", "slot": None,
    }
    return meta, manifest


# ---------------------------------------------------------------------------
# ToneMeta / Variant round-trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_all_fields(tmp_home):
    meta = tone_meta.ToneMeta(
        artist="Foo Fighters", song="White Limo", descriptor=None,
        tags=["hard rock", "lead"],
        description_md="the full companion markdown, folded in",
        variants={
            "gibson-les-paul-junior": tone_meta.Variant(
                hsp="tones/foo-fighters-white-limo-les-paul-jr.hsp",
                preset_name="Foo Fighters - White Limo - Les Paul Jr",
                guitar_settings={"pickup": "bridge", "tone": "7"},
                notes_md="bridge pickup, tone rolled off",
            )
        },
        created="2026-01-01", updated="2026-01-01", schema=1,
    )

    tone_meta.save_tone_meta(meta)
    loaded = tone_meta.load_tone_meta(meta.logical_slug)

    assert loaded.schema == 1
    assert loaded.artist == "Foo Fighters"
    assert loaded.song == "White Limo"
    assert loaded.descriptor is None
    assert loaded.tags == ["hard rock", "lead"]
    assert loaded.description_md == "the full companion markdown, folded in"
    assert loaded.created == "2026-01-01"  # preserved from the object passed in
    assert loaded.updated == date.today().isoformat()  # bumped

    assert set(loaded.variants) == {"gibson-les-paul-junior"}
    v = loaded.variants["gibson-les-paul-junior"]
    assert v.hsp == "tones/foo-fighters-white-limo-les-paul-jr.hsp"
    assert v.preset_name == "Foo Fighters - White Limo - Les Paul Jr"
    assert v.guitar_settings == {"pickup": "bridge", "tone": "7"}
    assert v.notes_md == "bridge pickup, tone rolled off"


def test_save_tone_meta_writes_under_tones_dir(tmp_home):
    meta = tone_meta.ToneMeta(
        artist="A", song="B", descriptor=None, tags=[], description_md=None,
        variants={}, created="2020-01-01", updated="2020-01-01",
    )
    tone_meta.save_tone_meta(meta)
    expected = home.tones_dir() / "a-b.json"
    assert expected.exists()
    on_disk = json.loads(expected.read_text())
    assert on_disk["schema"] == 1
    assert on_disk["artist"] == "A"


def test_save_tone_meta_preserves_created_across_resave(tmp_home):
    meta = tone_meta.ToneMeta(
        artist="A", song="B", descriptor=None, tags=[], description_md=None,
        variants={}, created="2020-01-01", updated="2020-01-01",
    )
    tone_meta.save_tone_meta(meta)
    first_created = meta.created

    # Re-save via a freshly constructed object with a bogus `created` -- the
    # on-disk `created` must win.
    reloaded = tone_meta.load_tone_meta(meta.logical_slug)
    reloaded.created = "1999-01-01"  # simulate stale/incorrect in-memory value
    tone_meta.save_tone_meta(reloaded)

    final = tone_meta.load_tone_meta(meta.logical_slug)
    assert final.created == first_created
    assert final.updated == date.today().isoformat()


def test_load_all_tone_metas_empty_when_dir_missing(tmp_home):
    assert tone_meta.load_all_tone_metas() == []


def test_load_all_tone_metas_returns_every_saved_meta(tmp_home):
    m1 = tone_meta.ToneMeta(artist="A", song="B", descriptor=None, tags=[],
                             description_md=None, variants={},
                             created="2020-01-01", updated="2020-01-01")
    m2 = tone_meta.ToneMeta(artist=None, song=None, descriptor="Warm Clean",
                             tags=[], description_md=None, variants={},
                             created="2020-01-01", updated="2020-01-01")
    tone_meta.save_tone_meta(m1)
    tone_meta.save_tone_meta(m2)
    slugs = {m.logical_slug for m in tone_meta.load_all_tone_metas()}
    assert slugs == {"a-b", "warm-clean"}


# ---------------------------------------------------------------------------
# logical_slug / display_base properties
# ---------------------------------------------------------------------------


def test_logical_slug_and_display_base_artist_song():
    meta = tone_meta.ToneMeta(
        artist="Foo Fighters", song="White Limo", descriptor=None,
        tags=[], description_md=None, variants={},
        created="x", updated="x",
    )
    assert meta.logical_slug == "foo-fighters-white-limo"
    assert meta.display_base == "Foo Fighters - White Limo"


def test_logical_slug_and_display_base_descriptor():
    meta = tone_meta.ToneMeta(
        artist=None, song=None, descriptor="Warm Jazz Clean",
        tags=[], description_md=None, variants={},
        created="x", updated="x",
    )
    assert meta.logical_slug == "warm-jazz-clean"
    assert meta.display_base == "Warm Jazz Clean"


# ---------------------------------------------------------------------------
# upsert_variant
# ---------------------------------------------------------------------------


def test_upsert_variant_creates_new_meta_when_none():
    meta = tone_meta.upsert_variant(
        None,
        artist="Foo Fighters", song="White Limo", descriptor=None,
        guitar_slug="gibson-les-paul-junior", guitar_short="Les Paul Jr",
        hsp_path="tones/foo-fighters-white-limo-les-paul-jr.hsp",
        tags=["hard rock"],
    )
    assert isinstance(meta, tone_meta.ToneMeta)
    assert meta.artist == "Foo Fighters"
    assert meta.song == "White Limo"
    assert meta.tags == ["hard rock"]
    assert set(meta.variants) == {"gibson-les-paul-junior"}
    v = meta.variants["gibson-les-paul-junior"]
    assert v.hsp == "tones/foo-fighters-white-limo-les-paul-jr.hsp"
    assert v.preset_name == "Foo Fighters - White Limo - Les Paul Jr"
    assert meta.created == meta.updated == date.today().isoformat()


def test_upsert_variant_replaces_existing_variant_same_guitar():
    meta = tone_meta.upsert_variant(
        None, artist="A", song="B", descriptor=None,
        guitar_slug="g1", guitar_short="G1",
        hsp_path="tones/a-b-g1.hsp", tags=[],
    )
    meta2 = tone_meta.upsert_variant(
        meta, artist="A", song="B", descriptor=None,
        guitar_slug="g1", guitar_short="G1 v2",
        hsp_path="tones/a-b-g1-v2.hsp", tags=[],
    )
    assert len(meta2.variants) == 1
    assert meta2.variants["g1"].hsp == "tones/a-b-g1-v2.hsp"
    assert meta2.variants["g1"].preset_name == "A - B - G1 v2"


def test_upsert_variant_adds_second_variant_for_different_guitar():
    meta = tone_meta.upsert_variant(
        None, artist="A", song="B", descriptor=None,
        guitar_slug="g1", guitar_short="G1",
        hsp_path="tones/a-b-g1.hsp", tags=[],
    )
    meta = tone_meta.upsert_variant(
        meta, artist="A", song="B", descriptor=None,
        guitar_slug="g2", guitar_short="G2",
        hsp_path="tones/a-b-g2.hsp", tags=[],
    )
    assert set(meta.variants) == {"g1", "g2"}
    assert meta.variants["g1"].hsp == "tones/a-b-g1.hsp"
    assert meta.variants["g2"].hsp == "tones/a-b-g2.hsp"
    # both variants belong to the SAME logical tone
    assert meta.logical_slug == "a-b"


def test_upsert_variant_generic_key_omits_guitar_segment():
    meta = tone_meta.upsert_variant(
        None, artist="A", song="B", descriptor=None,
        guitar_slug=None, guitar_short=None,
        hsp_path="tones/a-b.hsp", tags=[],
    )
    assert set(meta.variants) == {"generic"}
    assert meta.variants["generic"].preset_name == "A - B"


# ---------------------------------------------------------------------------
# validate_tone_meta
# ---------------------------------------------------------------------------


def test_validate_empty_for_fully_valid_meta(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert problems == []


def test_validate_flags_both_song_and_descriptor(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.descriptor = "Warm Clean"  # song is already "B"
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert any("song" in p and "descriptor" in p for p in problems)


def test_validate_flags_neither_song_nor_descriptor(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.song = None
    meta.artist = None
    meta.descriptor = None
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert any("song" in p and "descriptor" in p for p in problems)


def test_validate_flags_missing_hsp_on_disk(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].hsp = "tones/does-not-exist.hsp"
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert any("does-not-exist.hsp" in p for p in problems)


def test_validate_flags_unknown_variant_key(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["not-a-known-guitar"] = meta.variants.pop("g1")
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs=set()
    )
    assert any("not-a-known-guitar" in p for p in problems)


def test_validate_flags_unregistered_preset_name(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    manifest.tones.clear()
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert any("A - B - G1" in p for p in problems)


def test_validate_generic_key_always_allowed(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["generic"] = meta.variants.pop("g1")
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs=set()
    )
    assert not any("generic" in p and "known guitar" in p for p in problems)


# ---------------------------------------------------------------------------
# git integration (advisory auto-commit)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available on PATH")
def test_save_tone_meta_produces_a_commit(tmp_home, _isolated_git_env):
    meta = tone_meta.upsert_variant(
        None, artist="A", song="B", descriptor=None,
        guitar_slug=None, guitar_short=None,
        hsp_path="tones/a-b.hsp", tags=[],
    )
    tone_meta.save_tone_meta(meta)

    log = subprocess.run(
        ["git", "-C", str(home.helixgen_home()), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert log.strip() != ""


# ---------------------------------------------------------------------------
# Review fixes: C1 (auto-commit scoping), I2 (blank-aware identity), I3
# (guitar_slug/guitar_short consistency), I4 (schema check), M7 (empty
# guitar_settings / notes_md round-trip).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available on PATH")
def test_save_tone_meta_does_not_commit_when_library_is_outside_home(
    tmp_home, monkeypatch, _isolated_git_env, tmp_path_factory
):
    """CRITICAL 1: when $HELIXGEN_LIBRARY points OUTSIDE $HELIXGEN_HOME, saving
    a tone JSON (which then lives outside home) must NOT auto-commit inside
    home -- that would sweep in unrelated files sitting in the home repo."""
    # home is a git repo (tmp_home / _isolated_git_env set HELIXGEN_HOME);
    # make it a real repo with an untracked scratch file sitting in it.
    home_dir = home.helixgen_home()
    home_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(home_dir), "init", "-q"], check=True)
    scratch = home_dir / "unrelated_scratch_file.txt"
    scratch.write_text("do not commit me")

    # Point the library at a directory OUTSIDE home entirely -- a fresh temp
    # root from tmp_path_factory, a sibling of (never nested under) home_dir.
    external_library = tmp_path_factory.mktemp("external_library")
    assert not external_library.resolve().is_relative_to(home_dir.resolve())
    monkeypatch.setenv("HELIXGEN_LIBRARY", str(external_library))

    meta = tone_meta.upsert_variant(
        None, artist="A", song="B", descriptor=None,
        guitar_slug=None, guitar_short=None,
        hsp_path="tones/a-b.hsp", tags=[],
    )
    tone_meta.save_tone_meta(meta)

    # The tone JSON was written under the external library, not home.
    assert (external_library / "tones" / "a-b.json").exists()

    # No commit was created in home: no .git/refs/heads and no log entries,
    # and the scratch file remains untracked/uncommitted.
    log = subprocess.run(
        ["git", "-C", str(home_dir), "log", "--oneline"],
        capture_output=True, text=True,
    )
    assert log.returncode != 0 or log.stdout.strip() == ""
    status = subprocess.run(
        ["git", "-C", str(home_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "unrelated_scratch_file.txt" in status  # still untracked, not committed


def test_validate_flags_blank_artist_as_missing_identity(tmp_home):
    """IMPORTANT 2: a blank (empty-string) artist must be treated as ABSENT,
    consistent with naming's blank rule -- not as "present but different from
    song", which would let a self-contradictory meta through as clean."""
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.artist = ""
    meta.song = "Some Song"
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert problems  # must NOT be clean
    assert any("artist" in p and "song" in p for p in problems)


def test_upsert_variant_raises_when_only_guitar_slug_given():
    with pytest.raises(ValueError):
        tone_meta.upsert_variant(
            None, artist="A", song="B", descriptor=None,
            guitar_slug="g1", guitar_short=None,
            hsp_path="tones/a-b-g1.hsp", tags=[],
        )


def test_upsert_variant_raises_when_only_guitar_short_given():
    with pytest.raises(ValueError):
        tone_meta.upsert_variant(
            None, artist="A", song="B", descriptor=None,
            guitar_slug=None, guitar_short="G1",
            hsp_path="tones/a-b-g1.hsp", tags=[],
        )


def test_validate_flags_unsupported_schema(tmp_home):
    """IMPORTANT 4: validate_tone_meta must check schema == 1."""
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.schema = 2
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs={"g1"}
    )
    assert any("schema" in p for p in problems)


def test_round_trip_variant_with_empty_guitar_settings_and_no_notes(tmp_home):
    """MINOR 7: a Variant with empty guitar_settings={} and notes_md=None must
    save/load faithfully."""
    meta = tone_meta.ToneMeta(
        artist="A", song="B", descriptor=None, tags=[], description_md=None,
        variants={
            "g1": tone_meta.Variant(
                hsp="tones/a-b-g1.hsp", preset_name="A - B - G1",
                guitar_settings={}, notes_md=None,
            )
        },
        created="2020-01-01", updated="2020-01-01",
    )
    tone_meta.save_tone_meta(meta)
    loaded = tone_meta.load_tone_meta(meta.logical_slug)
    v = loaded.variants["g1"]
    assert v.guitar_settings == {}
    assert v.notes_md is None


# ---------------------------------------------------------------------------
# guitar_settings_warnings (Task 11): a SEPARATE, non-fatal channel
# ---------------------------------------------------------------------------


def _profile(slug_name="G1", controls=("volume", "tone")):
    return guitars.GuitarProfile(
        name=slug_name, short_name=slug_name, type="guitar", active=None,
        pickups=None, construction=None, character_md=None, genres=[],
        controls=[guitars.Control(name=c, kind="knob") for c in controls],
    )


def test_guitar_settings_warnings_none_without_profiles(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].guitar_settings = {"whatever": "x"}
    assert tone_meta.guitar_settings_warnings(meta, guitar_profiles=None) == []
    assert tone_meta.guitar_settings_warnings(meta, guitar_profiles={}) == []


def test_guitar_settings_warnings_flags_unknown_control_key(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].guitar_settings = {"tone": "7", "bogus": "x"}
    profile = _profile(controls=("volume", "tone"))
    warnings = tone_meta.guitar_settings_warnings(
        meta, guitar_profiles={"g1": profile})
    assert len(warnings) == 1
    assert "bogus" in warnings[0]
    assert "tone" not in warnings[0]  # known control -> no warning


def test_guitar_settings_warnings_skips_variant_without_profile(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].guitar_settings = {"bogus": "x"}
    # a profile exists, but not for THIS variant key -> profile may lag -> no warning
    assert tone_meta.guitar_settings_warnings(
        meta, guitar_profiles={"other": _profile()}) == []


def test_guitar_settings_warnings_control_match_is_case_insensitive(tmp_home):
    """A guitar_settings key differing only in case from a real control must NOT
    warn -- the rest of the guitar surface matches case-insensitively (FIX C)."""
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].guitar_settings = {"Volume": "8", "Tone": "7"}
    profile = _profile(controls=("volume", "tone"))
    assert tone_meta.guitar_settings_warnings(
        meta, guitar_profiles={"g1": profile}) == []


def test_guitar_settings_warnings_still_flags_genuinely_unknown_key(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].guitar_settings = {"Volume": "8", "bogus": "x"}
    profile = _profile(controls=("volume", "tone"))
    warnings = tone_meta.guitar_settings_warnings(
        meta, guitar_profiles={"g1": profile})
    assert len(warnings) == 1
    assert "bogus" in warnings[0]


# ---------------------------------------------------------------------------
# Variant.normalized (device normalize's library record) + find_variant_by_hsp
# ---------------------------------------------------------------------------


_NORMALIZED = {
    "at": "2026-07-16T12:00:00-07:00",
    "scope": "snapshots",
    "target_total_db": 27.96,
    "tolerance_db": 1.0,
    "seconds": 6.0,
    "helixgen_version": "0.25.0",
    "targets": [
        {"snapshot": 0, "name": "Rhythm", "ok": True, "reason": None,
         "gain_db": 27.96, "output_db": -6.02, "playing_seconds": 5.2,
         "output_level_db": 0.0, "total_db": 27.96, "trim_db": 0.0,
         "applied": False,
         # unknown per-target keys must round-trip verbatim (open dicts):
         # future per-node stats land here without a schema change
         "future_per_node_stat": {"b5": -1.0}},
        {"snapshot": 1, "name": "Lead", "ok": True, "reason": None,
         "gain_db": 33.98, "output_db": 1.2, "playing_seconds": 5.0,
         "output_level_db": 0.0, "total_db": 33.98, "trim_db": -6.0,
         "applied": True},
    ],
}


def test_variant_normalized_round_trips_through_save_and_load(tmp_home):
    import copy

    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].normalized = copy.deepcopy(_NORMALIZED)
    tone_meta.save_tone_meta(meta)
    loaded = tone_meta.load_tone_meta(meta.logical_slug)
    assert loaded.variants["g1"].normalized == _NORMALIZED
    # target entries are OPEN dicts: unknown per-target keys round-trip
    assert loaded.variants["g1"].normalized["targets"][0][
        "future_per_node_stat"] == {"b5": -1.0}
    # schema stays 1: the field is optional, older readers just drop it
    assert loaded.schema == 1
    on_disk = json.loads(tone_meta.meta_path(meta.logical_slug).read_text())
    assert on_disk["schema"] == 1
    assert on_disk["variants"]["g1"]["normalized"] == _NORMALIZED


def test_variant_normalized_defaults_none_and_absent_key_loads_none(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    assert meta.variants["g1"].normalized is None
    tone_meta.save_tone_meta(meta)
    # a pre-existing metadata JSON without the key (older writer) loads None
    path = tone_meta.meta_path(meta.logical_slug)
    on_disk = json.loads(path.read_text())
    on_disk["variants"]["g1"].pop("normalized", None)
    path.write_text(json.dumps(on_disk))
    loaded = tone_meta.load_tone_meta(meta.logical_slug)
    assert loaded.variants["g1"].normalized is None


def test_variant_normalized_none_still_validates(tmp_home):
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].normalized = dict(_NORMALIZED)
    assert tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest,
        guitar_slugs=["g1"]) == []


def test_find_variant_by_hsp_resolves_library_relative_path(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    hsp_abs = home.library_dir() / meta.variants["g1"].hsp
    found = tone_meta.find_variant_by_hsp(hsp_abs)
    assert found is not None
    found_meta, key = found
    assert key == "g1"
    assert found_meta.logical_slug == meta.logical_slug


def test_find_variant_by_hsp_accepts_str_and_relative_forms(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    hsp_abs = home.library_dir() / meta.variants["g1"].hsp
    assert tone_meta.find_variant_by_hsp(str(hsp_abs)) is not None


def test_find_variant_by_hsp_returns_none_for_unknown_path(tmp_home, tmp_path):
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    stray = tmp_path / "not-in-library.hsp"
    stray.write_text("x")
    assert tone_meta.find_variant_by_hsp(stray) is None


def test_find_variant_by_hsp_matches_absolute_stored_path(tmp_home, tmp_path):
    # a variant whose stored hsp is absolute (outside the library root) is
    # still resolvable -- _to_library_relative stores such paths verbatim
    outside = tmp_path / "elsewhere" / "t.hsp"
    outside.parent.mkdir(parents=True)
    outside.write_text("x")
    meta = tone_meta.upsert_variant(
        None, descriptor="Outside Tone", guitar_slug=None, guitar_short=None,
        hsp_path=outside)
    tone_meta.save_tone_meta(meta)
    found = tone_meta.find_variant_by_hsp(outside)
    assert found is not None
    assert found[1] == "generic"


# ---------------------------------------------------------------------------
# residual batch #79/#83 (library-metadata + normalized-record reviews)
# ---------------------------------------------------------------------------


def test_save_tone_meta_cleans_tmp_file_on_write_failure(tmp_home, monkeypatch):
    # 79a: a failure before the atomic replace must remove the temp file and
    # leave the existing metadata untouched.
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    path = tone_meta.meta_path(meta.logical_slug)
    before = path.read_text()

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(tone_meta.os, "replace", _boom)
    meta.tags.append("new-tag")
    with pytest.raises(OSError):
        tone_meta.save_tone_meta(meta)
    assert path.read_text() == before
    assert list(path.parent.glob("*.tmp")) == []


def test_save_tone_meta_tmp_name_is_process_unique(tmp_home, monkeypatch):
    # 83c: the temp name carries the pid so two processes writing the same
    # meta path can never race on one fixed ".tmp" name.
    import os as _os

    seen = {}
    real_replace = tone_meta.os.replace

    def _spy(src, dst):
        seen["src"] = str(src)
        return real_replace(src, dst)

    monkeypatch.setattr(tone_meta.os, "replace", _spy)
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    assert f".{_os.getpid()}.tmp" in seen["src"]


def test_manifest_save_cleans_tmp_file_on_write_failure(tmp_home, monkeypatch):
    # 79a (shared weakness): SetlistManifest.save gets the same guarantee.
    from helixgen.device import manifest as manifest_mod

    m = SetlistManifest(home.manifest_path())
    m.tones["T"] = {"path": None, "content_hash": None,
                    "source": "authored", "slot": None}
    m.save()
    before = m.path.read_text()

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(manifest_mod.os, "replace", _boom)
    m.tones["U"] = {"path": None, "content_hash": None,
                    "source": "authored", "slot": None}
    with pytest.raises(OSError):
        m.save()
    assert m.path.read_text() == before
    assert list(m.path.parent.glob("*.tmp")) == []


def test_find_variant_by_hsp_matches_differently_cased_path(tmp_home):
    # 83a: on a case-insensitive filesystem (APFS), a differently-cased
    # spelling of a registered variant's path must still match (samestat).
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    hsp_abs = home.library_dir() / meta.variants["g1"].hsp
    alias = hsp_abs.with_name(hsp_abs.name.upper())
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive; alias spelling not reachable")
    found = tone_meta.find_variant_by_hsp(alias)
    assert found is not None
    assert found[1] == "g1"


def test_find_variant_by_hsp_tolerates_blank_hsp_value(tmp_home, tmp_path):
    # 79b: a hand-edited blank hsp value must never match (Path("") would
    # otherwise resolve to the library root) nor crash the walk.
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].hsp = ""
    tone_meta.save_tone_meta(meta)
    probe = tmp_path / "probe.hsp"
    probe.write_text("x")
    assert tone_meta.find_variant_by_hsp(probe) is None
    assert tone_meta.find_variant_by_hsp(home.library_dir()) is None


def test_validate_flags_blank_hsp_value(tmp_home):
    # 79b: a blank hsp is a clear problem, not a silent pass against the
    # library root directory.
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].hsp = ""
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs=["g1"])
    assert any("blank" in p for p in problems)


def test_validate_flags_hsp_pointing_at_directory(tmp_home):
    # 79b: `.exists()` would pass for a directory; the check requires a file.
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].hsp = "tones"  # the tones/ dir itself
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs=["g1"])
    assert any("not found" in p for p in problems)


def test_validate_accepts_absolute_stored_hsp(tmp_home, tmp_path):
    # 79b: an absolute stored path (outside the library) resolves verbatim.
    outside = tmp_path / "elsewhere" / "t.hsp"
    outside.parent.mkdir(parents=True)
    outside.write_text("x")
    meta, manifest = _valid_meta_and_manifest(tmp_home)
    meta.variants["g1"].hsp = str(outside)
    problems = tone_meta.validate_tone_meta(
        meta, tones_dir=home.tones_dir(), manifest=manifest, guitar_slugs=["g1"])
    assert problems == []


def test_upsert_variant_rejects_identity_mismatch(tmp_home):
    # 79c: appending a variant under a different identity than the existing
    # meta's is refused (one metadata file must describe one tone).
    meta, _ = _valid_meta_and_manifest(tmp_home)  # artist=A song=B
    with pytest.raises(ValueError, match="identity mismatch"):
        tone_meta.upsert_variant(
            meta, descriptor="Something Else",
            guitar_slug=None, guitar_short=None, hsp_path="tones/x.hsp")
    with pytest.raises(ValueError, match="identity mismatch"):
        tone_meta.upsert_variant(
            meta, artist="A", song="Different Song",
            guitar_slug=None, guitar_short=None, hsp_path="tones/x.hsp")


def test_upsert_variant_accepts_matching_identity(tmp_home):
    meta, _ = _valid_meta_and_manifest(tmp_home)  # artist=A song=B
    out = tone_meta.upsert_variant(
        meta, artist="A", song="B",
        guitar_slug="g2", guitar_short="G2", hsp_path="tones/a-b-g2.hsp")
    assert "g2" in out.variants


def test_unknown_keys_round_trip_through_save_and_load(tmp_home):
    # 83b: hand-edited unknown top-level AND per-variant keys survive a
    # load -> save round-trip (the closed serializer must not strip them).
    meta, _ = _valid_meta_and_manifest(tmp_home)
    tone_meta.save_tone_meta(meta)
    path = tone_meta.meta_path(meta.logical_slug)
    data = json.loads(path.read_text())
    data["x_custom_note"] = {"nested": [1, 2, 3]}
    data["variants"]["g1"]["x_variant_flag"] = "hand-edited"
    path.write_text(json.dumps(data))

    loaded = tone_meta.load_tone_meta(meta.logical_slug)
    assert loaded.extra["x_custom_note"] == {"nested": [1, 2, 3]}
    assert loaded.variants["g1"].extra["x_variant_flag"] == "hand-edited"

    loaded.description_md = "an ordinary edit"
    tone_meta.save_tone_meta(loaded)
    after = json.loads(path.read_text())
    assert after["x_custom_note"] == {"nested": [1, 2, 3]}
    assert after["variants"]["g1"]["x_variant_flag"] == "hand-edited"
    assert after["description_md"] == "an ordinary edit"


def test_unknown_keys_never_shadow_known_fields(tmp_home):
    # a hand-added extra key colliding with a real field must not overwrite
    # the real value on save (known fields win).
    meta, _ = _valid_meta_and_manifest(tmp_home)
    meta.extra["artist"] = "Imposter"
    tone_meta.save_tone_meta(meta)
    after = json.loads(tone_meta.meta_path(meta.logical_slug).read_text())
    assert after["artist"] == "A"


def test_parse_tone_meta_raises_on_shape_invalid_data():
    # 83d seam: the public parser applies the same shape rules that
    # load_all_tone_metas warns-and-skips on.
    with pytest.raises(Exception):
        tone_meta.parse_tone_meta({"variants": {"g": {"preset_name": "no hsp"}}})
    with pytest.raises(Exception):
        tone_meta.parse_tone_meta({"variants": ["not", "a", "dict"]})
    ok = tone_meta.parse_tone_meta(
        {"artist": "A", "song": "B",
         "variants": {"g": {"hsp": "tones/x.hsp", "preset_name": "P"}}})
    assert ok.variants["g"].hsp == "tones/x.hsp"
