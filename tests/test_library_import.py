"""Tests for ``helixgen library import`` (external .hsp -> tone library).

Import is the single-tone sibling of migration: it MOVES the source ``.hsp``
into ``tones_dir()`` (``--keep-source`` copies), folds a sibling ``.md`` into
``description_md`` (missing -> null + a warning), rewrites ``meta.name`` to the
resolved display name, writes the ToneMeta JSON, and advisory-commits.
Placing the .hsp in ``tones_dir()`` IS its registration -- the library
DIRECTORY is the index (2026-09-09 file-copy design), there is no manifest.
Naming flags drive identity with the SAME
validation + collision rules as ``generate`` (a bad combo or an existing target
slug is a ``ClickException`` / exit 1).

Driven through the real CLI (``CliRunner``) so the click wiring + help contract
are exercised. Git identity is isolated so a dev machine's config can't leak.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen import home, tone_meta
from helixgen.cli import cli
from helixgen.hsp import read_hsp, write_hsp

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


def _library_names() -> dict:
    """``meta.name -> path`` for every .hsp in the library -- the directory IS
    the tone registry now, so this is what "registered" means."""
    d = home.tones_dir()
    if not d.is_dir():
        return {}
    return {read_hsp(f)["meta"]["name"]: f for f in d.glob("*.hsp")}


# ---------------------------------------------------------------------------
# default: MOVE
# ---------------------------------------------------------------------------


def test_import_moves_source_into_the_library(tmp_home):
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

    # "registered" == the .hsp is in the library under that name.
    assert _library_names() == {"Warm Jazz Clean - Les Paul Jr": dest}


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



# ---------------------------------------------------------------------------
# C1: directory import is atomic on a mid-batch collision + always persists
# ---------------------------------------------------------------------------


def test_import_directory_name_collision_moves_nothing(tmp_home):
    """Two exports in the dir sharing meta.name collide on slug: the whole batch
    is refused BEFORE anything is moved (atomic refusal), and the on-disk
    manifest stays empty -- no unreconcilable partial state."""
    ext = tmp_home / "batch"
    ext.mkdir()
    _write_hsp(ext / "one.hsp", "Same Tone")
    _write_hsp(ext / "two.hsp", "Same Tone")  # same meta.name -> same slug

    res = CliRunner().invoke(cli, ["library", "import", str(ext)],
                             catch_exceptions=False)
    assert res.exit_code != 0
    assert "collision" in res.output.lower()

    # NOTHING moved: both sources still present
    assert (ext / "one.hsp").exists()
    assert (ext / "two.hsp").exists()
    # library empty -> nothing registered (the directory IS the registry)
    assert _library_names() == {}


def test_import_directory_lands_every_tone_in_the_library(tmp_home):
    """A clean 3-tone dir import moves every tone into ``tones_dir()`` -- which
    IS the registration, since the directory is the index."""
    ext = tmp_home / "batch"
    ext.mkdir()
    _write_hsp(ext / "a.hsp", "Alpha Tone")
    _write_hsp(ext / "b.hsp", "Beta Tone")
    _write_hsp(ext / "c.hsp", "Gamma Tone")

    res = _run(["library", "import", str(ext)])
    assert res.exit_code == 0, res.output
    for slug in ("alpha-tone", "beta-tone", "gamma-tone"):
        assert (home.tones_dir() / f"{slug}.hsp").exists()

    assert set(_library_names()) == {"Alpha Tone", "Beta Tone", "Gamma Tone"}


def test_import_directory_midbatch_failure_keeps_progress_and_reconciles(
        tmp_home, monkeypatch):
    """An UNEXPECTED mid-batch error is recorded and the loop CONTINUES; the
    tones already moved into the library stay imported (a placed file IS the
    registration -- nothing to strand) and a re-run reconciles the failed one."""
    ext = tmp_home / "batch"
    ext.mkdir()
    _write_hsp(ext / "a.hsp", "Alpha Tone")
    _write_hsp(ext / "b.hsp", "Beta Tone")
    _write_hsp(ext / "c.hsp", "Gamma Tone")

    from helixgen import migrate as _migrate
    real_place = _migrate.place_tone
    calls = {"n": 0}

    def _flaky_place(src, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # trips exactly once, on the 2nd tone
            raise RuntimeError("boom mid-batch")
        return real_place(src, **kw)

    monkeypatch.setattr("helixgen.migrate.place_tone", _flaky_place)

    res = CliRunner().invoke(cli, ["library", "import", str(ext)],
                             catch_exceptions=False)
    assert res.exit_code != 0  # the batch reported a failure

    # first + third tones landed in the library despite the mid-batch failure
    assert set(_library_names()) == {"Alpha Tone", "Gamma Tone"}
    assert (home.tones_dir() / "alpha-tone.hsp").exists()
    assert (home.tones_dir() / "gamma-tone.hsp").exists()
    # the failed tone's source survived (its place raised BEFORE the move)
    assert (ext / "b.hsp").exists()

    # re-run reconciles: the flaky counter is already spent, so Beta imports now
    res2 = _run(["library", "import", str(ext)])
    assert res2.exit_code == 0, res2.output
    assert set(_library_names()) == {"Alpha Tone", "Beta Tone", "Gamma Tone"}
    assert (home.tones_dir() / "beta-tone.hsp").exists()


# ---------------------------------------------------------------------------
# name collisions against the library DIRECTORY (no registry to consult)
# ---------------------------------------------------------------------------


def test_import_refuses_a_name_another_library_tone_already_carries(tmp_home):
    """The name check is NOT the destination-path check: a library .hsp whose
    ``meta.name`` no longer matches its slug (hand-edited) still owns that
    name. Importing a tone that resolves to it -- differing only in case --
    must be refused, or the library ends up with two tones claiming one
    identity. This is what the retired manifest's unique-name rule bought."""
    first = tmp_home / "first.hsp"
    _write_hsp(first, "ignored")
    assert _run(["library", "import", str(first),
                 "--descriptor", "Other Tone"]).exit_code == 0
    placed = home.tones_dir() / "other-tone.hsp"
    _write_hsp(placed, "Solo Tone")  # renamed in place; slug stays other-tone

    second = tmp_home / "second.hsp"
    _write_hsp(second, "ignored")
    res = CliRunner().invoke(
        cli, ["library", "import", str(second), "--descriptor", "solo TONE"])
    assert res.exit_code != 0
    assert "already named" in (res.output + res.stderr)
    assert second.exists()  # refused BEFORE the move -- source never stranded
    assert set(_library_names()) == {"Solo Tone"}
