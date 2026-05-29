import json
import os
from pathlib import Path

import pytest

from helixgen.ir import IrMapping, default_irs_path


def test_default_irs_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_IRS", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    assert default_irs_path() == Path("/tmp/fake-home/.helixgen/irs")


def test_default_irs_path_honors_env_var(monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRS", "/custom/irs")
    assert default_irs_path() == Path("/custom/irs")


def test_load_returns_empty_when_no_file(tmp_path):
    mapping = IrMapping.load(tmp_path)
    assert mapping.irs_dir == tmp_path
    assert mapping.entries == {}


def test_save_then_reload_round_trips(tmp_path):
    m = IrMapping(irs_dir=tmp_path, entries={"abc": "foo.wav"})
    m.save()
    on_disk = json.loads((tmp_path / "mapping.json").read_text())
    assert on_disk == {"abc": "foo.wav"}
    reloaded = IrMapping.load(tmp_path)
    assert reloaded.entries == {"abc": "foo.wav"}


def test_save_is_atomic(tmp_path):
    """If save crashes mid-write, mapping.json must not be partial."""
    m = IrMapping(irs_dir=tmp_path, entries={"abc": "foo.wav"})
    m.save()
    # Verify no .tmp file is left behind on a successful save
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"stray tmp files: {leftovers}"


def test_save_creates_directory(tmp_path):
    target = tmp_path / "nested" / "irs"
    m = IrMapping(irs_dir=target, entries={"abc": "foo.wav"})
    m.save()
    assert (target / "mapping.json").exists()
