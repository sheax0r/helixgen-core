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
    assert "0" in res.output


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
