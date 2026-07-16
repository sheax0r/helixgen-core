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

from helixgen import home, tone_meta
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
