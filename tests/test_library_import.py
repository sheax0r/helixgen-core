"""Tests for ``helixgen library import`` (external .hsp -> tone library).

Import is the single-tone sibling of migration: it MOVES the source ``.hsp``
into ``tones_dir()`` (``--keep-source`` copies), folds a sibling ``.md`` into
``description_md`` (missing -> null + a warning), rewrites ``meta.name`` to the
resolved display name, writes the ToneMeta JSON, registers the tone in the
manifest, and advisory-commits. Naming flags drive identity with the SAME
validation + collision rules as ``generate`` (a bad combo or an existing target
slug is a ``ClickException`` / exit 1).

Driven through the real CLI (``CliRunner``) so the click wiring + help contract
are exercised. Git identity is isolated so a dev machine's config can't leak.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen import home, tone_meta
from helixgen.cli import cli
from helixgen.hsp import read_hsp, write_hsp
from helixgen.device.manifest import SetlistManifest

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIXGEN_GIT_COMMIT_TONES", raising=False)
    fake_home = tmp_path / "_fake_home_for_git"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / "gitconfig-does-not-exist"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _write_hsp(path: Path, name: str) -> None:
    write_hsp(path, {"meta": {"name": name}, "preset": {"flow": []}})


def _run(args):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# default: MOVE
# ---------------------------------------------------------------------------


def test_import_moves_source_and_registers(tmp_home):
    ext = tmp_home / "ext"
    ext.mkdir()
    src = ext / "raw.hsp"
    _write_hsp(src, "Raw Export")
    (ext / "raw.md").write_text("The description.")

    res = _run(["library", "import", str(src), "--descriptor", "Warm Jazz Clean",
                "--guitar", "Les Paul Jr"])
    assert res.exit_code == 0, res.output

    dest = home.tones_dir() / "warm-jazz-clean-les-paul-jr.hsp"
    assert dest.exists()
    assert not src.exists()  # moved by default

    assert read_hsp(dest)["meta"]["name"] == "Warm Jazz Clean - Les Paul Jr"
    meta = tone_meta.load_tone_meta("warm-jazz-clean")
    assert "les-paul-jr" in meta.variants
    assert meta.description_md == "The description."

    m = SetlistManifest.load()
    assert "Warm Jazz Clean - Les Paul Jr" in m.tones


def test_import_keep_source_copies(tmp_home):
    ext = tmp_home / "ext"
    ext.mkdir()
    src = ext / "raw.hsp"
    _write_hsp(src, "Keeper")

    res = _run(["library", "import", str(src), "--descriptor", "Keeper Tone",
                "--keep-source"])
    assert res.exit_code == 0, res.output

    dest = home.tones_dir() / "keeper-tone.hsp"
    assert dest.exists()
    assert src.exists()  # kept


def test_import_missing_md_warns_and_null_description(tmp_home):
    ext = tmp_home / "ext"
    ext.mkdir()
    src = ext / "raw.hsp"
    _write_hsp(src, "No Doc")

    res = _run(["library", "import", str(src), "--descriptor", "No Doc Tone"])
    assert res.exit_code == 0, res.output
    assert "warning" in res.output.lower() or "no" in res.output.lower()

    meta = tone_meta.load_tone_meta("no-doc-tone")
    assert meta.description_md is None


def test_import_uses_meta_name_as_descriptor_when_no_flags(tmp_home):
    ext = tmp_home / "ext"
    ext.mkdir()
    src = ext / "raw.hsp"
    _write_hsp(src, "Bright Lead")

    res = _run(["library", "import", str(src)])
    assert res.exit_code == 0, res.output
    assert (home.tones_dir() / "bright-lead.hsp").exists()
    meta = tone_meta.load_tone_meta("bright-lead")
    assert meta.descriptor == "Bright Lead"


# ---------------------------------------------------------------------------
# validation + collision (same rules as generate)
# ---------------------------------------------------------------------------


def test_import_rejects_artist_without_song(tmp_home):
    ext = tmp_home / "ext"
    ext.mkdir()
    src = ext / "raw.hsp"
    _write_hsp(src, "X")
    res = CliRunner().invoke(
        cli, ["library", "import", str(src), "--artist", "Foo"])
    assert res.exit_code != 0
    assert "song" in res.output.lower()
    assert src.exists()  # nothing moved on a bad-combo rejection


def test_import_refuses_to_overwrite_existing_slug(tmp_home):
    ext = tmp_home / "ext"
    ext.mkdir()
    a = ext / "a.hsp"
    _write_hsp(a, "First")
    _run(["library", "import", str(a), "--descriptor", "Same Name"])

    b = ext / "b.hsp"
    _write_hsp(b, "Second")
    res = CliRunner().invoke(
        cli, ["library", "import", str(b), "--descriptor", "Same Name"])
    assert res.exit_code != 0
    assert "already" in res.output.lower()
    assert b.exists()  # refused -> source untouched


# ---------------------------------------------------------------------------
# directory import
# ---------------------------------------------------------------------------


def test_import_directory_imports_each_hsp(tmp_home):
    ext = tmp_home / "batch"
    ext.mkdir()
    _write_hsp(ext / "one.hsp", "Tone One")
    _write_hsp(ext / "two.hsp", "Tone Two")

    res = _run(["library", "import", str(ext)])
    assert res.exit_code == 0, res.output
    assert (home.tones_dir() / "tone-one.hsp").exists()
    assert (home.tones_dir() / "tone-two.hsp").exists()
