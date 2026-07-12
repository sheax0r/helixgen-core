"""CLI tests for the `ir-cache` maintenance verb (--stats/--clear/--prune)."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.irhash_cache import IrHashCache


def _warm_cache(cache_path: Path, files: dict[Path, str]) -> None:
    cache = IrHashCache.load(cache_path)
    for wav, h in files.items():
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"payload")
        cache.put(wav, h)
    cache.save()


def test_ir_cache_stats_reports_count_and_path(tmp_path, monkeypatch):
    cache_path = tmp_path / "irhash.json"
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(cache_path))
    _warm_cache(cache_path, {tmp_path / "a.wav": "a" * 32, tmp_path / "b.wav": "b" * 32})

    result = CliRunner().invoke(cli, ["ir-cache", "--stats"])
    assert result.exit_code == 0, result.output
    assert "2" in result.output  # entry count
    assert str(cache_path) in result.output


def test_ir_cache_stats_on_empty_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "irhash.json"
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(cache_path))
    result = CliRunner().invoke(cli, ["ir-cache", "--stats"])
    assert result.exit_code == 0, result.output
    assert "0" in result.output


def test_ir_cache_clear_removes_file(tmp_path, monkeypatch):
    cache_path = tmp_path / "irhash.json"
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(cache_path))
    _warm_cache(cache_path, {tmp_path / "a.wav": "a" * 32})
    assert cache_path.exists()

    result = CliRunner().invoke(cli, ["ir-cache", "--clear"])
    assert result.exit_code == 0, result.output
    assert not cache_path.exists()


def test_ir_cache_prune_drops_missing_files(tmp_path, monkeypatch):
    cache_path = tmp_path / "irhash.json"
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(cache_path))
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _warm_cache(cache_path, {a: "a" * 32, b: "b" * 32})
    b.unlink()

    result = CliRunner().invoke(cli, ["ir-cache", "--prune"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output  # one pruned

    remaining = json.loads(cache_path.read_text())["entries"]
    assert str(a.resolve()) in remaining
    assert str(b.resolve()) not in remaining


def test_ir_cache_requires_an_action(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRHASH_CACHE", str(tmp_path / "irhash.json"))
    result = CliRunner().invoke(cli, ["ir-cache"])
    assert result.exit_code != 0
