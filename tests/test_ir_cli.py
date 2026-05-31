"""CLI tests for register-irs and list-irs."""
import json
import struct
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli

HSP_MAGIC = b"rpshnosj"


def _libsndfile_available() -> bool:
    """Return True iff the system has a usable libsndfile shared library."""
    try:
        from helixgen.ir import _load_libsndfile
        _load_libsndfile()
        return True
    except Exception:
        return False


def _write_synth_wav(path: Path, n_frames: int = 64) -> Path:
    """Write a tiny PCM_24 48 kHz mono WAV with a single non-zero sample.

    Synthetic so we never need to ship copyrighted IR fixtures.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sr, ch, bps = 48000, 1, 24
    block_align = ch * bps // 8
    byte_rate = sr * block_align
    data = bytearray(n_frames * block_align)
    # one non-zero sample at index 0: int24 value 0x123456 (= 1193046)
    data[0:3] = b"\x56\x34\x12"
    fmt_chunk = (
        b"fmt " + struct.pack("<I", 16) +
        struct.pack("<HHIIHH", 1, ch, sr, byte_rate, block_align, bps)
    )
    data_chunk = b"data" + struct.pack("<I", len(data)) + bytes(data)
    body = b"WAVE" + fmt_chunk + data_chunk
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _write_hsp(path: Path, body: dict) -> None:
    path.write_bytes(HSP_MAGIC + json.dumps(body).encode())


def _ir_block(path: int, position: int, irhash: str) -> dict:
    return {
        "path": path,
        "position": position,
        "slot": [{"model": "HX2_ImpulseResponseWithPan", "irhash": irhash, "params": {}}],
    }


def _preset_with_irs(hashes: list[str]) -> dict:
    flow = {}
    for i, h in enumerate(hashes, start=1):
        flow[f"b{i:02d}"] = _ir_block(0, i, h)
    return {"meta": {"name": "t"}, "preset": {"flow": [flow]}}


def _write_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF\0\0\0\0WAVE")
    return path


def test_register_irs_happy_path(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["hash1", "hash2"]))

    wav1 = _write_wav(irs_dir / "a.wav")
    wav2 = _write_wav(irs_dir / "b.wav")

    result = CliRunner().invoke(cli, ["register-irs", str(preset), str(wav1), str(wav2)])
    assert result.exit_code == 0, result.output
    mapping = json.loads((irs_dir / "mapping.json").read_text())
    assert mapping == {"hash1": "a.wav", "hash2": "b.wav"}


def test_register_irs_count_mismatch_errors(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["h1", "h2"]))
    wav1 = _write_wav(irs_dir / "a.wav")

    result = CliRunner().invoke(cli, ["register-irs", str(preset), str(wav1)])
    assert result.exit_code != 0
    assert "2 IR blocks" in result.output and "1 wav" in result.output


def test_register_irs_conflict_errors_without_force(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["hX"]))
    wav_old = _write_wav(irs_dir / "old.wav")
    wav_new = _write_wav(irs_dir / "new.wav")

    CliRunner().invoke(cli, ["register-irs", str(preset), str(wav_old)])
    result = CliRunner().invoke(cli, ["register-irs", str(preset), str(wav_new)])
    assert result.exit_code != 0
    assert "already mapped" in result.output


def test_register_irs_force_overwrites(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    preset = tmp_path / "reg.hsp"
    _write_hsp(preset, _preset_with_irs(["hX"]))
    wav_old = _write_wav(irs_dir / "old.wav")
    wav_new = _write_wav(irs_dir / "new.wav")

    CliRunner().invoke(cli, ["register-irs", str(preset), str(wav_old)])
    result = CliRunner().invoke(
        cli, ["register-irs", "--force", str(preset), str(wav_new)]
    )
    assert result.exit_code == 0, result.output
    mapping = json.loads((irs_dir / "mapping.json").read_text())
    assert mapping == {"hX": "new.wav"}


@pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_auto_computes_hash_from_wav(tmp_path, monkeypatch):
    """register-irs called with wav-only args (no preset) auto-computes hashes."""
    from helixgen.ir import compute_stadium_irhash

    irs_dir = tmp_path / "irs"
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    wav = _write_synth_wav(tmp_path / "synth.wav")
    expected_hash = compute_stadium_irhash(wav)

    result = CliRunner().invoke(cli, ["register-irs", str(wav)])
    assert result.exit_code == 0, result.output
    mapping = json.loads((irs_dir / "mapping.json").read_text())
    assert expected_hash in mapping
    assert mapping[expected_hash] == str(wav)


@pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_register_irs_auto_handles_multiple_wavs(tmp_path, monkeypatch):
    """Auto-compute mode supports multiple wavs in one invocation."""
    from helixgen.ir import compute_stadium_irhash

    irs_dir = tmp_path / "irs"
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))

    wav1 = _write_synth_wav(tmp_path / "synth_a.wav", n_frames=64)
    wav2 = _write_synth_wav(tmp_path / "synth_b.wav", n_frames=128)
    h1 = compute_stadium_irhash(wav1)
    h2 = compute_stadium_irhash(wav2)

    result = CliRunner().invoke(cli, ["register-irs", str(wav1), str(wav2)])
    assert result.exit_code == 0, result.output
    mapping = json.loads((irs_dir / "mapping.json").read_text())
    assert mapping[h1] == str(wav1)
    assert mapping[h2] == str(wav2)


def test_list_irs_empty_prints_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRS", str(tmp_path))
    result = CliRunner().invoke(cli, ["list-irs"])
    assert result.exit_code == 0
    assert result.output == ""


def test_list_irs_prints_one_per_line_sorted(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIXGEN_IRS", str(tmp_path))
    mapping_file = tmp_path / "mapping.json"
    mapping_file.write_text(json.dumps({
        "bbb": "second.wav",
        "aaa": "first.wav",
    }))
    result = CliRunner().invoke(cli, ["list-irs"])
    assert result.exit_code == 0
    # sorted by hash
    assert result.output == "aaa  first.wav\nbbb  second.wav\n"
