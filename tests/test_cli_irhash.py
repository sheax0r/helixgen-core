"""`helixgen irhash` — stateless Stadium-hash computation for WAVs.

CLI replacement for the removed MCP `compute_irhash` (single file) and
`discover_irs` (directory walk) tools: computes hashes WITHOUT writing to
mapping.json (use `register-irs` / `ir-scan` to persist).
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from helixgen.cli import cli

FAKE_HASH = "0f" * 16


def _fake_hash(monkeypatch):
    calls = []

    def fake(path, cache=None):
        calls.append(str(path))
        return FAKE_HASH

    monkeypatch.setattr("helixgen.cli.cached_irhash", fake)
    return calls


def _wav(tmp_path, name="a.wav"):
    p = tmp_path / name
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")
    return p


def test_irhash_single_wav(tmp_path, monkeypatch):
    _fake_hash(monkeypatch)
    wav = _wav(tmp_path)
    res = CliRunner().invoke(cli, ["irhash", str(wav)])
    assert res.exit_code == 0, res.output
    assert f"{FAKE_HASH}  {wav}" in res.output


def test_irhash_json(tmp_path, monkeypatch):
    _fake_hash(monkeypatch)
    wav = _wav(tmp_path)
    res = CliRunner().invoke(cli, ["irhash", "--json", str(wav)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data == [{"hash": FAKE_HASH, "path": str(wav), "basename": "a.wav"}]


def test_irhash_directory_walk(tmp_path, monkeypatch):
    calls = _fake_hash(monkeypatch)
    d = tmp_path / "irs"
    (d / "sub").mkdir(parents=True)
    _wav(d, "b.wav")
    _wav(d / "sub", "c.wav")
    (d / "notes.txt").write_text("not a wav")
    res = CliRunner().invoke(cli, ["irhash", "--json", str(d)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert [e["basename"] for e in data] == ["b.wav", "c.wav"]
    assert len(calls) == 2


def test_irhash_directory_skips_failures_with_warning(tmp_path, monkeypatch):
    def fake(path, cache=None):
        if path.name == "bad.wav":
            raise NotImplementedError("only 48 kHz supported")
        return FAKE_HASH

    monkeypatch.setattr("helixgen.cli.cached_irhash", fake)
    d = tmp_path / "irs"
    d.mkdir()
    _wav(d, "bad.wav")
    _wav(d, "good.wav")
    res = CliRunner().invoke(cli, ["irhash", "--json", str(d)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [e["basename"] for e in data] == ["good.wav"]
    assert "bad.wav" in res.stderr


def test_irhash_explicit_file_failure_is_fatal(tmp_path, monkeypatch):
    def fake(path, cache=None):
        raise NotImplementedError("only 48 kHz supported")

    monkeypatch.setattr("helixgen.cli.cached_irhash", fake)
    wav = _wav(tmp_path)
    res = CliRunner().invoke(cli, ["irhash", str(wav)])
    assert res.exit_code != 0
    assert "48 kHz" in res.output


def test_irhash_does_not_touch_mapping(tmp_path, monkeypatch):
    _fake_hash(monkeypatch)
    irs_dir = tmp_path / "irs-mapping"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))
    wav = _wav(tmp_path)
    res = CliRunner().invoke(cli, ["irhash", str(wav)])
    assert res.exit_code == 0, res.output
    assert not (irs_dir / "mapping.json").exists()


def test_irhash_directory_skips_valueerror_files(tmp_path, monkeypatch):
    """Bad-magic/oversized WAVs raise ValueError from the front-door check;
    a directory walk must skip them with a warning, not crash (review #1)."""
    def fake(path, cache=None):
        if path.name == "notwav.wav":
            raise ValueError(f"{path} is not a RIFF/WAVE file (bad magic)")
        return FAKE_HASH

    monkeypatch.setattr("helixgen.cli.cached_irhash", fake)
    d = tmp_path / "irs"
    d.mkdir()
    _wav(d, "notwav.wav")
    _wav(d, "good.wav")
    res = CliRunner().invoke(cli, ["irhash", "--json", str(d)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [e["basename"] for e in data] == ["good.wav"]
    assert "bad magic" in res.stderr


def test_irhash_explicit_file_valueerror_is_clean_error(tmp_path, monkeypatch):
    def fake(path, cache=None):
        raise ValueError("x is not a RIFF/WAVE file (bad magic)")

    monkeypatch.setattr("helixgen.cli.cached_irhash", fake)
    wav = _wav(tmp_path)
    res = CliRunner().invoke(cli, ["irhash", str(wav)])
    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "bad magic" in res.output


def test_irhash_dedupes_repeated_paths(tmp_path, monkeypatch):
    _fake_hash(monkeypatch)
    wav = _wav(tmp_path)
    res = CliRunner().invoke(cli, ["irhash", "--json", str(wav), str(wav)])
    assert res.exit_code == 0, res.output
    assert len(json.loads(res.stdout)) == 1
