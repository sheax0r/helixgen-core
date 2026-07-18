"""Tests for `helixgen library` (list/show/doc/validate) and the top-level
`describe` command (Task 8).

Tone metadata fixtures are built by driving the real `generate` CLI verb
(same pattern as tests/test_generate_library.py: `tmp_home` + the `hsp_library`
conftest fixture, writing into a tmp library) rather than hand-rolling JSON,
so these tests exercise the actual on-disk shape `tone_meta` produces.

NOTE: tests/test_cli_library.py already exists and covers the pre-existing
`register` / `device add` manifest verbs -- this file is purely additive and
does not touch that file or its tests.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen import guitars, home, naming, tone_meta
from helixgen.cli import cli
from helixgen.device.manifest import SetlistManifest


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path, monkeypatch):
    """Prevent any real user git config / global gitignore from leaking in
    (mirrors tests/test_generate_library.py) -- `doc`'s save_tone_meta call
    advisory-commits under the tmp home, keep it fully hermetic."""
    monkeypatch.delenv("HELIXGEN_PREFS", raising=False)
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _write_recipe(tmp_path: Path, name: str, block: str = "Brit Amp") -> Path:
    spec = tmp_path / f"{name}.recipe.json"
    spec.write_text(json.dumps(
        {"name": name, "paths": [{"blocks": [{"block": block}]}]}))
    return spec


def _make_tone(hsp_library, tmp_path, *, descriptor, guitar=None, tags=None):
    """Generate one tone into the tmp library via the real `generate` verb.

    Returns (logical_slug, guitar_variant_key, preset_name).
    """
    spec = _write_recipe(tmp_path, descriptor)
    args = [str(spec), "--descriptor", descriptor, "--library", str(hsp_library.root)]
    if guitar:
        args += ["--guitar", guitar]
    res = CliRunner().invoke(cli, ["generate", *args])
    assert res.exit_code == 0, res.output

    logical = naming.logical_slug(descriptor=descriptor)
    variant_key = naming.slugify(guitar) if guitar else "generic"
    if tags:
        meta = tone_meta.load_tone_meta(logical)
        meta.tags = list(tags)
        tone_meta.save_tone_meta(meta)
    meta = tone_meta.load_tone_meta(logical)
    return logical, variant_key, meta.variants[variant_key].preset_name


def _make_song_tone(hsp_library, tmp_path, *, artist, song, guitar=None, spec_stem=None):
    """Like `_make_tone` but for an artist+song identity (rather than a
    descriptor) -- used to build tones whose preset_name legitimately
    contains characters like "/" or ".." (e.g. artist "AC/DC"). The recipe
    file itself is named from a filesystem-safe `spec_stem` (defaults to a
    slugified artist-song), never from the raw artist/song text.

    Returns (logical_slug, guitar_variant_key, preset_name).
    """
    stem = spec_stem or naming.slugify(f"{artist}-{song}") or "song-tone"
    spec = _write_recipe(tmp_path, stem, block="Brit Amp")
    args = [str(spec), "--artist", artist, "--song", song,
            "--library", str(hsp_library.root)]
    if guitar:
        args += ["--guitar", guitar]
    res = CliRunner().invoke(cli, ["generate", *args])
    assert res.exit_code == 0, res.output

    logical = naming.logical_slug(artist=artist, song=song)
    variant_key = naming.slugify(guitar) if guitar else "generic"
    meta = tone_meta.load_tone_meta(logical)
    return logical, variant_key, meta.variants[variant_key].preset_name


# ---------------------------------------------------------------------------
# library list
# ---------------------------------------------------------------------------


def test_library_list_json_empty(tmp_home):
    res = CliRunner().invoke(cli, ["library", "list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"tones": [], "guitars": [], "irs": []}


def test_library_list_human_empty(tmp_home):
    res = CliRunner().invoke(cli, ["library", "list"])
    assert res.exit_code == 0, res.output
    assert "Tones (0):" in res.output
    assert "(none)" in res.output


def test_library_list_json_includes_generated_tones(tmp_home, hsp_library, tmp_path):
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    _make_tone(hsp_library, tmp_path, descriptor="Thrash Rhythm")

    res = CliRunner().invoke(cli, ["library", "list", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["guitars"] == []
    assert data["irs"] == []
    slugs = {t["slug"] for t in data["tones"]}
    assert slugs == {"warm-jazz-clean", "thrash-rhythm"}


def test_library_list_human_shows_generated_tones(tmp_home, hsp_library, tmp_path):
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    res = CliRunner().invoke(cli, ["library", "list"])
    assert res.exit_code == 0, res.output
    assert "warm-jazz-clean" in res.output


# ---------------------------------------------------------------------------
# library show
# ---------------------------------------------------------------------------


def test_library_show_by_logical_slug(tmp_home, hsp_library, tmp_path):
    slug, _, preset_name = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    res = CliRunner().invoke(cli, ["library", "show", slug])
    assert res.exit_code == 0, res.output
    assert preset_name in res.output


def test_library_show_by_variant_preset_name(tmp_home, hsp_library, tmp_path):
    slug, _, preset_name = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    res = CliRunner().invoke(cli, ["library", "show", preset_name])
    assert res.exit_code == 0, res.output
    assert slug in res.output


def test_library_show_by_json_filename_stem(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "show", f"{slug}.json"])
    assert res.exit_code == 0, res.output


def test_library_show_json_dumps_raw_metadata(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "show", slug, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["descriptor"] == "Warm Jazz Clean"
    assert data["schema"] == 1


def test_library_show_unknown_name_exits_1(tmp_home):
    res = CliRunner().invoke(cli, ["library", "show", "no-such-tone"])
    assert res.exit_code == 1


# ---------------------------------------------------------------------------
# describe (top-level)
# ---------------------------------------------------------------------------


def test_describe_prints_summary_variants_and_description(tmp_home, hsp_library, tmp_path):
    slug, variant_key, preset_name = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr",
        tags=["jazz", "clean"])
    meta = tone_meta.load_tone_meta(slug)
    meta.variants[variant_key].guitar_settings = {"pickup": "bridge", "tone": "7"}
    meta.description_md = "# Warm Jazz Clean\n\nA smooth, dark tone for cleans."
    tone_meta.save_tone_meta(meta)

    res = CliRunner().invoke(cli, ["describe", slug])
    assert res.exit_code == 0, res.output
    assert "Warm Jazz Clean" in res.output
    assert "jazz" in res.output
    assert "clean" in res.output
    assert preset_name in res.output
    assert variant_key in res.output
    assert "pickup" in res.output and "bridge" in res.output
    assert "# Warm Jazz Clean\n\nA smooth, dark tone for cleans." in res.output


def test_describe_resolves_by_preset_name(tmp_home, hsp_library, tmp_path):
    slug, _, preset_name = _make_tone(hsp_library, tmp_path, descriptor="Thrash Rhythm")
    res = CliRunner().invoke(cli, ["describe", preset_name])
    assert res.exit_code == 0, res.output
    assert slug in res.output or "Thrash Rhythm" in res.output


def test_describe_unknown_tone_exits_1(tmp_home):
    res = CliRunner().invoke(cli, ["describe", "does-not-exist"])
    assert res.exit_code == 1


# ---------------------------------------------------------------------------
# library doc
# ---------------------------------------------------------------------------


def test_doc_from_file_sets_description_md(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    md = tmp_path / "desc.md"
    md.write_text("# Warm Jazz Clean\n\nSmooth and dark.")
    res = CliRunner().invoke(cli, ["library", "doc", slug, "--from-file", str(md)])
    assert res.exit_code == 0, res.output
    assert tone_meta.load_tone_meta(slug).description_md == "# Warm Jazz Clean\n\nSmooth and dark."


def test_doc_stdin_sets_description_md(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Thrash Rhythm")
    res = CliRunner().invoke(cli, ["library", "doc", slug, "-"], input="Heavy palm-muted crunch.")
    assert res.exit_code == 0, res.output
    assert tone_meta.load_tone_meta(slug).description_md == "Heavy palm-muted crunch."


def test_doc_variant_sets_notes_md(tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    res = CliRunner().invoke(
        cli, ["library", "doc", slug, "--variant", variant_key, "-"],
        input="Roll the tone knob back to 6.")
    assert res.exit_code == 0, res.output
    meta = tone_meta.load_tone_meta(slug)
    assert meta.variants[variant_key].notes_md == "Roll the tone knob back to 6."
    # the logical description is untouched
    assert meta.description_md is None


def test_doc_missing_variant_exits_1(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(
        cli, ["library", "doc", slug, "--variant", "nonexistent-guitar", "-"], input="x")
    assert res.exit_code == 1


def test_doc_no_source_given_exits_nonzero(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "doc", slug])
    assert res.exit_code != 0


def test_doc_bumps_updated(tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    path = tone_meta.meta_path(slug)
    data = json.loads(path.read_text())
    data["updated"] = "2020-01-01"
    path.write_text(json.dumps(data))

    res = CliRunner().invoke(cli, ["library", "doc", slug, "-"], input="text")
    assert res.exit_code == 0, res.output
    assert tone_meta.load_tone_meta(slug).updated == date.today().isoformat()


# ---------------------------------------------------------------------------
# library validate
# ---------------------------------------------------------------------------


def test_validate_exit_0_when_clean(tmp_home, hsp_library, tmp_path):
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 0, res.output


def test_validate_json_shape_when_clean(tmp_home, hsp_library, tmp_path):
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "validate", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"problems": [], "warnings": []}


def test_validate_reports_missing_hsp_on_disk(tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    meta = tone_meta.load_tone_meta(slug)
    (home.library_dir() / meta.variants[variant_key].hsp).unlink()

    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 1
    assert slug in res.output

    res_json = CliRunner().invoke(cli, ["library", "validate", "--json"])
    assert res_json.exit_code == 1
    data = json.loads(res_json.output)
    assert data["problems"]
    assert any(slug in p for p in data["problems"])


def test_validate_reports_preset_name_not_in_manifest(tmp_home, hsp_library, tmp_path):
    slug, _, preset_name = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    m = SetlistManifest.load()
    del m.tones[preset_name]
    m.save()

    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 1
    assert "manifest" in res.output.lower()


def test_validate_reports_malformed_json_and_exits_1(tmp_home):
    """`library validate` is documented (design §8) as the safety net for
    hand/skill-edited JSON -- but it was built on `load_all_tone_metas()`,
    which silently skips any file that fails to parse. A syntactically
    broken tones/*.json must be surfaced as a problem and force exit 1, both
    in human output and --json, naming the broken file."""
    _write_broken_json("broken")

    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 1
    assert "broken.json" in res.output

    res_json = CliRunner().invoke(cli, ["library", "validate", "--json"])
    assert res_json.exit_code == 1
    data = json.loads(res_json.output)
    assert data["problems"]
    assert any("broken.json" in p for p in data["problems"])


def test_validate_still_exits_0_with_only_valid_metas(tmp_home, hsp_library, tmp_path):
    """Regression: a library containing only valid metadata is unaffected by
    the malformed-JSON check -- validate stays clean/exit 0."""
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 0, res.output

    res_json = CliRunner().invoke(cli, ["library", "validate", "--json"])
    assert res_json.exit_code == 0, res_json.output
    assert json.loads(res_json.output) == {"problems": [], "warnings": []}


def test_validate_accepts_guitar_targeted_variant_key_when_no_guitar_profiles(
    tmp_home, hsp_library, tmp_path
):
    """A tone made via `generate --guitar ...` uses the guitar slug (not
    "generic") as its variant key. Since PR 2 ships no guitar-profile
    library yet, `validate` must not flag that key as unknown -- see
    review finding I-2."""
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 0, res.output

    res_json = CliRunner().invoke(cli, ["library", "validate", "--json"])
    assert res_json.exit_code == 0, res_json.output
    assert json.loads(res_json.output) == {"problems": [], "warnings": []}


# ---------------------------------------------------------------------------
# review-finding regression tests (I-1..I-4, minor path-traversal guard)
# ---------------------------------------------------------------------------


def _write_broken_json(name: str) -> None:
    home.tones_dir().mkdir(parents=True, exist_ok=True)
    (home.tones_dir() / f"{name}.json").write_text("{not valid json")


def test_show_human_malformed_json_exits_1_clean(tmp_home):
    _write_broken_json("broken")
    res = CliRunner().invoke(cli, ["library", "show", "broken"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "could not read metadata" in res.output.lower()


def test_describe_malformed_json_exits_1_clean(tmp_home):
    _write_broken_json("broken")
    res = CliRunner().invoke(cli, ["describe", "broken"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "could not read metadata" in res.output.lower()


def test_doc_malformed_json_exits_1_clean(tmp_home):
    _write_broken_json("broken")
    res = CliRunner().invoke(cli, ["library", "doc", "broken", "-"], input="x")
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "could not read metadata" in res.output.lower()


def test_show_json_raw_dump_still_fine_on_malformed_json(tmp_home):
    """`show --json` dumps raw on-disk bytes -- it never parses, so a
    malformed file is not this verb's problem to guard (matches existing
    graceful posture)."""
    _write_broken_json("broken")
    res = CliRunner().invoke(cli, ["library", "show", "broken", "--json"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "{not valid json"


def test_show_ambiguous_file_match_and_other_preset_name_exits_1(tmp_home):
    """Tone A's variant preset_name is literally "beta"; Tone B's own
    logical slug is "beta" (its metadata file is beta.json). NAME "beta"
    resolves via BOTH mechanisms to two different logical tones -- this must
    raise ambiguous, not silently pick the filename match (I-3)."""
    meta_a = tone_meta.ToneMeta(
        artist=None, song=None, descriptor="Alpha Tone", tags=[],
        description_md=None,
        variants={"generic": tone_meta.Variant(hsp="tones/alpha.hsp", preset_name="beta")},
        created="2026-01-01", updated="2026-01-01", schema=1,
    )
    tone_meta.save_tone_meta(meta_a)
    assert meta_a.logical_slug == "alpha-tone"

    meta_b = tone_meta.ToneMeta(
        artist=None, song=None, descriptor="beta", tags=[],
        description_md=None,
        variants={"generic": tone_meta.Variant(hsp="tones/beta.hsp", preset_name="Beta Tone")},
        created="2026-01-01", updated="2026-01-01", schema=1,
    )
    tone_meta.save_tone_meta(meta_b)
    assert meta_b.logical_slug == "beta"

    res = CliRunner().invoke(cli, ["library", "show", "beta"])
    assert res.exit_code == 1
    assert "ambiguous" in res.output.lower()


def test_doc_metadata_filename_diverges_from_identity_slug_exits_1(tmp_home):
    """tones/mytone.json's content computes logical_slug "my-tone" (a
    hand-rename/edit divergence). `library doc` must refuse rather than
    write to the divergent path meta_path(meta.logical_slug) (I-4)."""
    home.tones_dir().mkdir(parents=True, exist_ok=True)
    data = {
        "schema": 1, "artist": None, "song": None, "descriptor": "My Tone",
        "tags": [], "description_md": None,
        "variants": {"generic": {"hsp": "tones/mytone.hsp", "preset_name": "My Tone",
                                  "guitar_settings": {}, "notes_md": None}},
        "created": "2026-01-01", "updated": "2026-01-01",
    }
    mismatched_path = home.tones_dir() / "mytone.json"
    mismatched_path.write_text(json.dumps(data))

    res = CliRunner().invoke(cli, ["library", "doc", "mytone", "-"], input="hello")
    assert res.exit_code == 1
    assert "my-tone" in res.output.lower()
    assert not (home.tones_dir() / "my-tone.json").exists()
    # original mismatched file is untouched
    assert json.loads(mismatched_path.read_text())["description_md"] is None


def test_show_resolves_preset_name_containing_slash(tmp_home, hsp_library, tmp_path):
    """A real preset_name legitimately containing "/" (artist "AC/DC") must
    resolve via `library show`, `describe`, and `library doc` -- the
    path-traversal guard only gates the filename-lookup branch, never the
    in-memory preset_name match (regression for the fix on top of ceec06c,
    which had made `_reject_unsafe_name` reject the raw NAME up front for
    ALL resolution, including this legitimate preset_name branch)."""
    logical, variant_key, preset_name = _make_song_tone(
        hsp_library, tmp_path, artist="AC/DC", song="Thunderstruck", guitar="Strat"
    )
    assert preset_name == "AC/DC - Thunderstruck - Strat"

    res_show = CliRunner().invoke(cli, ["library", "show", preset_name])
    assert res_show.exit_code == 0, res_show.output
    assert logical in res_show.output

    res_describe = CliRunner().invoke(cli, ["describe", preset_name])
    assert res_describe.exit_code == 0, res_describe.output
    assert preset_name in res_describe.output

    res_doc = CliRunner().invoke(
        cli, ["library", "doc", preset_name, "-"], input="AC/DC notes"
    )
    assert res_doc.exit_code == 0, res_doc.output
    assert tone_meta.load_tone_meta(logical).description_md == "AC/DC notes"


def test_show_resolves_preset_name_containing_dotdot(tmp_home, hsp_library, tmp_path):
    """A preset_name containing ".." (an ellipsis in a descriptor) must
    resolve via preset_name matching, not be hard-rejected."""
    logical, variant_key, preset_name = _make_tone(
        hsp_library, tmp_path, descriptor="To Be...", guitar="Strat"
    )
    assert preset_name == "To Be... - Strat"

    res_show = CliRunner().invoke(cli, ["library", "show", preset_name])
    assert res_show.exit_code == 0, res_show.output
    assert logical in res_show.output

    res_describe = CliRunner().invoke(cli, ["describe", preset_name])
    assert res_describe.exit_code == 0, res_describe.output
    assert preset_name in res_describe.output


def test_show_traversal_name_not_file_or_preset_name_is_clean_not_found(tmp_home):
    """A NAME that is neither a metadata file nor any preset_name, and looks
    like a path (e.g. "../etc/passwd"), must be a clean "not found" exit 1
    -- no traceback, and it must never actually resolve/read outside
    tones_dir() (there is nothing there to match in a fresh tmp_home, so a
    'no tone found' message -- not an ambiguous/other match -- proves no
    escape happened)."""
    res = CliRunner().invoke(cli, ["library", "show", "../etc/passwd"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "no tone found" in res.output.lower()


def test_show_traversal_name_with_embedded_dotdot_is_clean_not_found(tmp_home):
    res = CliRunner().invoke(cli, ["library", "show", "foo/../../bar"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "no tone found" in res.output.lower()


def test_show_embedded_null_byte_name_is_clean_not_found(tmp_home):
    """A NAME containing an embedded null byte makes Path.resolve() raise
    ValueError (not OSError). _is_safe_slug_candidate must treat that the
    same as any other unsafe candidate -- a clean "not found" exit 1, never
    an uncaught ValueError traceback."""
    res = CliRunner().invoke(cli, ["library", "show", "foo\x00bar"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "no tone found" in res.output.lower()
    assert "ValueError" not in res.output
    assert "Traceback" not in res.output


def test_describe_embedded_null_byte_name_is_clean_not_found(tmp_home):
    res = CliRunner().invoke(cli, ["describe", "foo\x00bar"])
    assert res.exit_code == 1
    assert isinstance(res.exception, SystemExit)
    assert "ValueError" not in res.output
    assert "Traceback" not in res.output


# ---------------------------------------------------------------------------
# library list --guitars / library show <guitar> (Task 11)
# ---------------------------------------------------------------------------


def _save_profile():
    guitars.save_profile(guitars.GuitarProfile(
        name="Gibson Les Paul Junior", short_name="Les Paul Jr", type="guitar",
        active=False, pickups="one bridge P-90", construction=None,
        character_md="P-90 grind", genres=["punk"],
        controls=[guitars.Control(name="volume", kind="knob"),
                  guitars.Control(name="tone", kind="knob", notes="no split")],
    ))


def test_library_list_json_includes_guitars(tmp_home):
    _save_profile()
    res = CliRunner().invoke(cli, ["library", "list", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["guitars"] == [{
        "slug": "gibson-les-paul-junior", "name": "Gibson Les Paul Junior",
        "short_name": "Les Paul Jr", "type": "guitar"}]


def test_library_list_guitars_human(tmp_home):
    _save_profile()
    res = CliRunner().invoke(cli, ["library", "list", "--guitars"])
    assert res.exit_code == 0, res.output
    assert "Guitars (1):" in res.output
    assert "gibson-les-paul-junior" in res.output
    assert "Les Paul Jr" in res.output


def test_library_show_resolves_guitar_by_short_name(tmp_home):
    _save_profile()
    res = CliRunner().invoke(cli, ["library", "show", "Les Paul Jr"])
    assert res.exit_code == 0, res.output
    assert "gibson-les-paul-junior" in res.output
    assert "Controls (2):" in res.output
    assert "volume [knob]" in res.output


def test_library_show_guitar_json_dumps_profile(tmp_home):
    _save_profile()
    res = CliRunner().invoke(cli, ["library", "show", "gibson-les-paul-junior", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["short_name"] == "Les Paul Jr"
    assert data["controls"][0]["name"] == "volume"


def test_library_show_unknown_name_still_errors(tmp_home):
    _save_profile()
    res = CliRunner().invoke(cli, ["library", "show", "Nonexistent Thing"])
    assert res.exit_code != 0


def test_library_show_prefers_tone_over_guitar(tmp_home, hsp_library, tmp_path):
    # A tone and a guitar could share a name; the tone wins (resolved first).
    _save_profile()
    _make_tone(hsp_library, tmp_path, descriptor="Les Paul Jr")
    res = CliRunner().invoke(cli, ["library", "show", "les-paul-jr"])
    assert res.exit_code == 0, res.output
    # tone output has a "Variants (" line; guitar output has "Controls ("
    assert "Variants (" in res.output


# ---------------------------------------------------------------------------
# normalized record surfacing (device normalize --yes writes it)
# ---------------------------------------------------------------------------


def _target(name, snapshot, *, output_db, trim_db=0.0, applied=False):
    return {"snapshot": snapshot, "name": name, "ok": True, "reason": None,
            "gain_db": 27.96, "output_db": output_db, "playing_seconds": 5.0,
            "output_level_db": 0.0, "total_db": 27.96, "trim_db": trim_db,
            "applied": applied}


def _set_normalized(slug, variant_key, **overrides):
    rec = {
        "at": "2026-07-16T12:00:00-07:00",
        "scope": "snapshots",
        "target_total_db": 27.96,
        "tolerance_db": 1.0,
        "seconds": 20.0,
        "helixgen_version": "0.25.0",
        "targets": [
            _target("Rhythm", 0, output_db=-6.02),
            _target("Lead", 1, output_db=-0.2, trim_db=-6.0, applied=True),
            _target("Clean", 2, output_db=-5.8),
        ],
    }
    rec.update(overrides)
    meta = tone_meta.load_tone_meta(slug)
    meta.variants[variant_key].normalized = rec
    tone_meta.save_tone_meta(meta)
    return rec


def test_library_show_displays_normalized_record(tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    _set_normalized(slug, variant_key)
    res = CliRunner().invoke(cli, ["library", "show", slug])
    assert res.exit_code == 0, res.output
    assert "normalized 2026-07-16" in res.output
    assert "1 trim" in res.output          # one non-zero trim (Lead)


def test_library_show_all_in_band_normalized_reads_in_band(
        tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    _set_normalized(slug, variant_key,
                    targets=[_target("Rhythm", 0, output_db=-6.02),
                             _target("Lead", 1, output_db=-0.2)])
    res = CliRunner().invoke(cli, ["library", "show", slug])
    assert res.exit_code == 0, res.output
    assert "normalized 2026-07-16" in res.output
    assert "in band" in res.output


def test_library_show_without_normalized_says_nothing(
        tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "show", slug])
    assert res.exit_code == 0, res.output
    assert "normalized" not in res.output


def test_library_show_json_carries_normalized_record(
        tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    rec = _set_normalized(slug, variant_key)
    res = CliRunner().invoke(cli, ["library", "show", slug, "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["variants"][variant_key]["normalized"] == rec


def test_describe_mentions_normalized_briefly(tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    _set_normalized(slug, variant_key)
    res = CliRunner().invoke(cli, ["describe", slug])
    assert res.exit_code == 0, res.output
    # summarized, not the full telemetry: date, target count, trims, the
    # hottest chain-out (in-chain clipping tell), scope
    assert "normalized 2026-07-16" in res.output
    assert "3 targets" in res.output
    assert "1 trim" in res.output
    assert "max chain-out -0.2 dBFS" in res.output
    assert "snapshots" in res.output


def test_describe_setlist_scope_normalized(tmp_home, hsp_library, tmp_path):
    slug, variant_key, _ = _make_tone(
        hsp_library, tmp_path, descriptor="Warm Jazz Clean", guitar="Les Paul Jr")
    _set_normalized(
        slug, variant_key, scope="setlist",
        targets=[{"tone": "Warm Jazz Clean - Les Paul Jr", "ok": True,
                  "reason": None, "gain_db": 27.96, "output_db": 1.2,
                  "playing_seconds": 5.0, "output_level_db": 0.0,
                  "total_db": 27.96, "trim_db": -1.5, "applied": True}])
    res = CliRunner().invoke(cli, ["describe", slug])
    assert res.exit_code == 0, res.output
    assert "normalized 2026-07-16" in res.output
    assert "1 target" in res.output
    assert "max chain-out +1.2 dBFS" in res.output   # OVER full scale
    assert "setlist" in res.output


def test_describe_without_normalized_says_nothing(
        tmp_home, hsp_library, tmp_path):
    slug, _, _ = _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["describe", slug])
    assert res.exit_code == 0, res.output
    assert "normalized" not in res.output


# ---------------------------------------------------------------------------
# residual batches #79/#83: list --json narrowing, validate shape checks,
# show tone/guitar shadow note
# ---------------------------------------------------------------------------


def test_library_list_json_honors_tones_flag(tmp_home, hsp_library, tmp_path):
    # 79d: a narrowing flag applies to the --json shape too.
    _save_profile()
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "list", "--tones", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert set(data) == {"tones"}
    assert {t["slug"] for t in data["tones"]} == {"warm-jazz-clean"}


def test_library_list_json_honors_guitars_and_irs_flags(tmp_home):
    _save_profile()
    res = CliRunner().invoke(cli, ["library", "list", "--guitars", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert set(data) == {"guitars"}
    assert data["guitars"][0]["slug"] == "gibson-les-paul-junior"

    res = CliRunner().invoke(cli, ["library", "list", "--irs", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"irs": []}

    # two flags together narrow to exactly those two sections
    res = CliRunner().invoke(
        cli, ["library", "list", "--tones", "--guitars", "--json"])
    assert res.exit_code == 0, res.output
    assert set(json.loads(res.output)) == {"tones", "guitars"}


def test_library_list_json_no_flags_keeps_full_shape(tmp_home):
    # the unnarrowed shape is unchanged (agent contract).
    res = CliRunner().invoke(cli, ["library", "list", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"tones": [], "guitars": [], "irs": []}


def test_validate_flags_shape_invalid_parseable_file(tmp_home):
    # 83d: a tones/*.json that parses but fails deserialization (the file
    # load_all_tone_metas warns-and-skips) must be a validate PROBLEM.
    tones = home.tones_dir()
    tones.mkdir(parents=True, exist_ok=True)
    (tones / "broken-shape.json").write_text(json.dumps(
        {"descriptor": "Broken", "variants": {"g": {"preset_name": "no hsp"}}}))

    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 1
    assert "broken-shape.json" in res.output
    assert "shape-invalid" in res.output

    res = CliRunner().invoke(cli, ["library", "validate", "--json"])
    assert res.exit_code == 1
    # res.stdout: the loaders' skip warning goes to stderr, JSON to stdout
    data = json.loads(res.stdout)
    assert any("shape-invalid" in p for p in data["problems"])


def test_validate_shape_valid_files_still_pass(tmp_home, hsp_library, tmp_path):
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "validate"])
    assert res.exit_code == 0, res.output


def test_library_show_notes_shadowed_guitar_profile(tmp_home, hsp_library, tmp_path):
    # 79h: NAME resolving as a tone while ALSO matching a guitar profile
    # must not silently mask the guitar -- a stderr note names it.
    _save_profile()  # short_name "Les Paul Jr"
    _make_tone(hsp_library, tmp_path, descriptor="Les Paul Jr")
    res = CliRunner().invoke(cli, ["library", "show", "Les Paul Jr"])
    assert res.exit_code == 0, res.output
    assert "Variants (" in res.output          # the tone is shown
    assert "also matches" in res.stderr
    assert "gibson-les-paul-junior" in res.stderr


def test_library_show_no_note_without_guitar_collision(tmp_home, hsp_library, tmp_path):
    _make_tone(hsp_library, tmp_path, descriptor="Warm Jazz Clean")
    res = CliRunner().invoke(cli, ["library", "show", "warm-jazz-clean"])
    assert res.exit_code == 0, res.output
    assert "also matches" not in res.stderr


# ---------------------------------------------------------------------------
# 79j: library add-guitar
# ---------------------------------------------------------------------------


def test_add_guitar_scaffolds_full_schema(tmp_home):
    res = CliRunner().invoke(
        cli, ["library", "add-guitar", "Gibson Les Paul Junior",
              "--short-name", "Les Paul Jr"])
    assert res.exit_code == 0, res.output
    path = guitars.profile_path("gibson-les-paul-junior")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data == {
        "schema": 1,
        "name": "Gibson Les Paul Junior",
        "short_name": "Les Paul Jr",
        "type": "guitar",
        "active": None,
        "pickups": None,
        "construction": None,
        "character_md": None,
        "genres": [],
        "controls": [],
    }
    assert str(path) in res.output


def test_add_guitar_defaults_short_name_and_type(tmp_home):
    res = CliRunner().invoke(cli, ["library", "add-guitar", "P Bass",
                                   "--type", "bass"])
    assert res.exit_code == 0, res.output
    data = json.loads(guitars.profile_path("p-bass").read_text())
    assert data["short_name"] == "P Bass"
    assert data["type"] == "bass"


def test_add_guitar_refuses_existing_slug(tmp_home):
    r1 = CliRunner().invoke(cli, ["library", "add-guitar", "Jazzmaster"])
    assert r1.exit_code == 0, r1.output
    r2 = CliRunner().invoke(cli, ["library", "add-guitar", "Jazzmaster"])
    assert r2.exit_code != 0
    assert "already exists" in (r2.output + r2.stderr)


def test_add_guitar_rejects_unsluggable_name(tmp_home):
    res = CliRunner().invoke(cli, ["library", "add-guitar", "--", "---"])
    assert res.exit_code != 0
    assert "slug-able" in (res.output + res.stderr)


def test_add_guitar_profile_resolves_in_show_and_generate(tmp_home):
    CliRunner().invoke(cli, ["library", "add-guitar", "Ibanez Prestige",
                             "--short-name", "Prestige"])
    res = CliRunner().invoke(cli, ["library", "show", "Prestige"])
    assert res.exit_code == 0, res.output
    assert "ibanez-prestige" in res.output


def test_add_guitar_auto_commits_home(tmp_home):
    import shutil as _shutil
    import subprocess
    if _shutil.which("git") is None:
        pytest.skip("git not available on PATH")
    res = CliRunner().invoke(cli, ["library", "add-guitar", "SG Special"])
    assert res.exit_code == 0, res.output
    log = subprocess.run(
        ["git", "-C", str(tmp_home), "log", "--oneline"],
        capture_output=True, text=True).stdout
    assert "guitar profile (sg-special)" in log
