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
    except (OSError, RuntimeError):
        return False


def _write_synth_wav(
    path: Path,
    n_frames: int = 64,
    *,
    sr: int = 48000,
    channels: int = 1,
    bps: int = 24,
    samples_per_channel: list[list[int]] | None = None,
) -> Path:
    """Write a tiny WAV. Defaults: PCM_24 48 kHz mono, single non-zero sample.

    Synthetic so we never need to ship copyrighted IR fixtures.

    `samples_per_channel`: optional [[ch0_s0, ch0_s1, ...], [ch1_s0, ...]] to
    set specific sample values per channel (used by stereo tests).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    block_align = channels * bps // 8
    byte_rate = sr * block_align
    bytes_per_samp = bps // 8

    data = bytearray(n_frames * block_align)
    if samples_per_channel is None:
        # default: int24 value 0x123456 at channel 0, frame 0
        data[0:bytes_per_samp] = (0x123456).to_bytes(bytes_per_samp, "little", signed=True)
    else:
        for frame_i in range(n_frames):
            for ch_i in range(channels):
                v = samples_per_channel[ch_i][frame_i] if frame_i < len(samples_per_channel[ch_i]) else 0
                off = frame_i * block_align + ch_i * bytes_per_samp
                data[off:off + bytes_per_samp] = v.to_bytes(bytes_per_samp, "little", signed=True)

    fmt_chunk = (
        b"fmt " + struct.pack("<I", 16) +
        struct.pack("<HHIIHH", 1, channels, sr, byte_rate, block_align, bps)
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


@pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_compute_stadium_irhash_stereo_reduces_to_left_channel(tmp_path):
    """Stereo input: only left channel is hashed (matches Stadium import)."""
    from helixgen.ir import compute_stadium_irhash

    # stereo: L = [0x123456, 0, 0, ...], R = [0x654321, 0, 0, ...]
    # if Stadium picked R or summed L+R, the hash would differ
    stereo = _write_synth_wav(
        tmp_path / "stereo.wav",
        n_frames=64,
        channels=2,
        samples_per_channel=[[0x123456], [0x654321]],
    )
    mono_left = _write_synth_wav(
        tmp_path / "mono_left.wav",
        n_frames=64,
        channels=1,
        samples_per_channel=[[0x123456]],
    )
    assert compute_stadium_irhash(stereo) == compute_stadium_irhash(mono_left)


@pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_compute_stadium_irhash_truncates_to_8192_with_fade(tmp_path):
    """For >8191-frame source: output is 8192 samples with exp fade on tail."""
    from helixgen.ir import compute_stadium_irhash

    # 10000-frame input → truncated to 8192. Make samples 8064..8191 non-zero
    # so the fade actually changes them — that proves the fade ran.
    samples = [0] * 8064 + [0x400000] * (10000 - 8064)
    long_wav = _write_synth_wav(
        tmp_path / "long.wav",
        n_frames=10000,
        samples_per_channel=[samples],
    )
    # And a control file with same first 8064 samples but zeros in the fade
    # region — its hash should differ because fade was applied above.
    samples_no_tail = [0] * 8064 + [0] * (10000 - 8064)
    control = _write_synth_wav(
        tmp_path / "control.wav",
        n_frames=10000,
        samples_per_channel=[samples_no_tail],
    )
    assert compute_stadium_irhash(long_wav) != compute_stadium_irhash(control)


@pytest.mark.skipif(not _libsndfile_available(), reason="libsndfile not installed")
def test_compute_stadium_irhash_rejects_non_48k(tmp_path):
    """Non-48k sources raise NotImplementedError with an actionable message."""
    from helixgen.ir import compute_stadium_irhash

    wav = _write_synth_wav(tmp_path / "wrong_sr.wav", n_frames=64, sr=44100)
    with pytest.raises(NotImplementedError, match="48 kHz"):
        compute_stadium_irhash(wav)


def test_register_irs_rejects_non_wav_after_first(tmp_path, monkeypatch):
    """Auto-compute branch rejects non-.wav args with a friendly error."""
    irs_dir = tmp_path / "irs"
    monkeypatch.setenv("HELIXGEN_IRS", str(irs_dir))
    wav = _write_wav(tmp_path / "a.wav")
    weird = _write_wav(tmp_path / "b.hsp")  # has hsp suffix but raw bytes
    result = CliRunner().invoke(cli, ["register-irs", str(wav), str(weird)])
    assert result.exit_code != 0
    assert "non-wav arg" in result.output


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
