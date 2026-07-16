import json
import os
from pathlib import Path

import pytest

from helixgen.ir import IrMapping, IrMappingError, default_irs_path


def test_default_irs_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_IRS", raising=False)
    monkeypatch.delenv("HELIXGEN_HOME", raising=False)
    monkeypatch.delenv("HELIXGEN_LIBRARY", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    # flipped to the library location (still honors HELIXGEN_IRS first)
    assert default_irs_path() == Path("/tmp/fake-home/.helixgen/library/irs")


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


def test_save_leaves_no_tmp_file_on_success(tmp_path):
    """A successful save must not leave mapping.json.tmp on disk."""
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


def test_register_records_new_hash_relative_when_inside_irs_dir(tmp_path):
    wav = tmp_path / "sub" / "foo.wav"
    wav.parent.mkdir()
    wav.write_bytes(b"riff")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc123", wav)
    assert m.entries == {"abc123": "sub/foo.wav"}


def test_register_records_absolute_when_outside_irs_dir(tmp_path):
    irs = tmp_path / "irs"
    irs.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_wav = outside_dir / "outside.wav"
    outside_wav.write_bytes(b"riff")
    m = IrMapping(irs_dir=irs)
    m.register("abc123", outside_wav)
    assert m.entries == {"abc123": str(outside_wav.resolve())}


def test_register_same_hash_same_file_is_idempotent(tmp_path):
    wav = tmp_path / "foo.wav"
    wav.write_bytes(b"riff")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc123", wav)
    m.register("abc123", wav)  # no-op
    assert m.entries == {"abc123": "foo.wav"}


def test_register_validates_wav_exists(tmp_path):
    m = IrMapping(irs_dir=tmp_path)
    with pytest.raises(FileNotFoundError, match="missing.wav"):
        m.register("abc123", tmp_path / "missing.wav")


def test_register_conflict_raises(tmp_path):
    wav1 = tmp_path / "a.wav"
    wav2 = tmp_path / "b.wav"
    wav1.write_bytes(b"a")
    wav2.write_bytes(b"b")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav1)
    with pytest.raises(IrMappingError, match="already mapped"):
        m.register("abc", wav2)


def test_register_force_overwrites(tmp_path):
    wav1 = tmp_path / "a.wav"
    wav2 = tmp_path / "b.wav"
    wav1.write_bytes(b"a")
    wav2.write_bytes(b"b")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav1)
    m.register("abc", wav2, force=True)
    assert m.entries == {"abc": "b.wav"}


def test_resolve_by_hash_returns_absolute_path(tmp_path):
    wav = tmp_path / "foo.wav"
    wav.write_bytes(b"r")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav)
    resolved = m.resolve_by_hash("abc")
    assert resolved == wav.resolve()


def test_resolve_by_hash_unknown_raises(tmp_path):
    m = IrMapping(irs_dir=tmp_path)
    with pytest.raises(IrMappingError, match="unknown IR hash"):
        m.resolve_by_hash("does-not-exist")


def test_resolve_by_basename_unique_returns_hash_and_path(tmp_path):
    sub = tmp_path / "packA"
    sub.mkdir()
    wav = sub / "foo.wav"
    wav.write_bytes(b"r")
    m = IrMapping(irs_dir=tmp_path)
    m.register("abc", wav)
    hash_, path = m.resolve_by_basename("foo.wav")
    assert hash_ == "abc"
    assert path == wav.resolve()


def test_resolve_by_basename_ambiguous_raises(tmp_path):
    a = tmp_path / "packA"
    b = tmp_path / "packB"
    a.mkdir()
    b.mkdir()
    wav_a = a / "foo.wav"
    wav_b = b / "foo.wav"
    wav_a.write_bytes(b"a")
    wav_b.write_bytes(b"b")
    m = IrMapping(irs_dir=tmp_path)
    m.register("h_a", wav_a)
    m.register("h_b", wav_b)
    with pytest.raises(IrMappingError, match="ambiguous"):
        m.resolve_by_basename("foo.wav")


def test_resolve_by_basename_missing_raises(tmp_path):
    m = IrMapping(irs_dir=tmp_path)
    with pytest.raises(IrMappingError, match="no registered IR matches"):
        m.resolve_by_basename("nope.wav")
