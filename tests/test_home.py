"""Tests for helixgen.home: canonical path resolution for ~/.helixgen."""
from pathlib import Path

import helixgen.home as home


def test_home_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HELIXGEN_HOME", raising=False)
    assert home.helixgen_home() == Path.home() / ".helixgen"


def test_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.delenv("HELIXGEN_LIBRARY", raising=False)
    monkeypatch.delenv("HELIXGEN_IRS", raising=False)
    monkeypatch.delenv("HELIXGEN_SETLISTS", raising=False)
    assert home.helixgen_home() == tmp_path
    assert home.library_dir() == tmp_path / "library"
    assert home.tones_dir() == tmp_path / "library" / "tones"
    assert home.guitars_dir() == tmp_path / "library" / "guitars"
    assert home.manifest_path() == tmp_path / "setlists" / "manifest.json"
    assert home.legacy_manifest_path() == tmp_path / "setlists.json"
    assert home.library_irs_dir() == tmp_path / "library" / "irs"
    assert home.legacy_irs_dir() == tmp_path / "irs"
    assert home.devices_dir() == tmp_path / "devices"


def test_area_env_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_HOME", str(tmp_path))
    monkeypatch.setenv("HELIXGEN_LIBRARY", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "m.json"))
    monkeypatch.setenv("HELIXGEN_IRS", str(tmp_path / "myirs"))
    assert home.library_dir() == tmp_path / "elsewhere"
    assert home.manifest_path() == tmp_path / "m.json"
    assert home.library_irs_dir() == tmp_path / "myirs"
    # legacy paths are always home-derived, never area-overridden
    assert home.legacy_manifest_path() == tmp_path / "setlists.json"
    assert home.legacy_irs_dir() == tmp_path / "irs"


def test_env_vars_expanduser(monkeypatch):
    """Every env-var branch expands a leading ``~`` — a user setting e.g.
    ``HELIXGEN_HOME=~/elsewhere`` (or the per-area overrides) must resolve
    against the real home directory, not be treated as a literal ``~`` path
    component."""
    monkeypatch.setenv("HELIXGEN_HOME", "~/elsewhere-home")
    monkeypatch.setenv("HELIXGEN_LIBRARY", "~/elsewhere-library")
    monkeypatch.setenv("HELIXGEN_SETLISTS", "~/elsewhere-setlists.json")
    monkeypatch.setenv("HELIXGEN_IRS", "~/elsewhere-irs")

    real_home = Path.home()
    assert home.helixgen_home() == real_home / "elsewhere-home"
    assert home.library_dir() == real_home / "elsewhere-library"
    assert home.manifest_path() == real_home / "elsewhere-setlists.json"
    assert home.library_irs_dir() == real_home / "elsewhere-irs"
