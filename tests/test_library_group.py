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

from helixgen import home, naming, tone_meta
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
    assert json.loads(res.output) == {"problems": []}


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
    assert json.loads(res_json.output) == {"problems": []}


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


def test_show_rejects_name_with_path_separator(tmp_home):
    res = CliRunner().invoke(cli, ["library", "show", "../etc/passwd"])
    assert res.exit_code == 1


def test_show_rejects_name_with_dotdot(tmp_home):
    res = CliRunner().invoke(cli, ["library", "show", "foo/../bar"])
    assert res.exit_code == 1
