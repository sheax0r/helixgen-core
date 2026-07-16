"""Tests for helixgen.audio_metrics (backlog #62 phase 3, offline half).

Ground truth is synthesized: sine waves at known amplitudes have analytically
known LUFS / crest / band placement, so the metrics are pinned against math,
not fixtures:

  * ITU-R BS.1770 calibration point: a 0 dBFS 997 Hz/1 kHz sine measured on
    one channel reads -3.01 LKFS/LUFS (the -0.691 offset exactly cancels the
    K-weighting gain at 1 kHz).
  * crest factor of a sine is 20*log10(sqrt(2)) = 3.0103 dB; of a square
    wave, 0 dB.
  * a pure tone puts (essentially) all its energy in the one band that
    contains it.
"""
from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from helixgen import audio_metrics as am


RATE = 48000

SINE_LUFS_0DBFS = -3.0103  # BS.1770: 0 dBFS 1 kHz sine, single channel


def sine(freq: float, seconds: float, amp: float = 1.0, rate: int = RATE,
         channels: int = 1) -> np.ndarray:
    t = np.arange(int(round(seconds * rate))) / rate
    x = amp * np.sin(2 * np.pi * freq * t)
    return np.tile(x[:, None], (1, channels))


def write_pcm16(path: Path, samples: np.ndarray, rate: int = RATE) -> Path:
    ints = np.clip(np.round(samples * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(samples.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(ints.tobytes())
    return path


# ---------------------------------------------------------------- LUFS ----

class TestIntegratedLufs:
    def test_full_scale_1khz_sine_mono_reads_minus_3(self):
        m = am.analyze(sine(1000.0, 5.0), RATE)
        assert m.lufs_integrated == pytest.approx(SINE_LUFS_0DBFS, abs=0.1)

    def test_amplitude_scales_linearly_in_db(self):
        m = am.analyze(sine(1000.0, 5.0, amp=0.1), RATE)
        assert m.lufs_integrated == pytest.approx(SINE_LUFS_0DBFS - 20.0,
                                                  abs=0.1)

    def test_stereo_sums_channel_energy_plus_3db(self):
        m = am.analyze(sine(1000.0, 5.0, channels=2), RATE)
        assert m.lufs_integrated == pytest.approx(SINE_LUFS_0DBFS + 3.0103,
                                                  abs=0.15)

    def test_44100_hz_coefficients_rederived(self):
        m = am.analyze(sine(1000.0, 5.0, rate=44100), 44100)
        assert m.lufs_integrated == pytest.approx(SINE_LUFS_0DBFS, abs=0.2)

    def test_k_weighting_shelf_boosts_high_frequencies(self):
        lo = am.analyze(sine(1000.0, 5.0, amp=0.25), RATE).lufs_integrated
        hi = am.analyze(sine(8000.0, 5.0, amp=0.25), RATE).lufs_integrated
        # BS.1770 high shelf is ~ +4 dB in the top octaves.
        assert hi - lo == pytest.approx(4.0, abs=1.0)

    def test_absolute_gate_ignores_trailing_silence(self):
        loud = sine(1000.0, 3.0, amp=0.25)
        padded = np.concatenate([loud, np.zeros((3 * RATE, 1))])
        m_loud = am.analyze(loud, RATE)
        m_padded = am.analyze(padded, RATE)
        assert m_padded.lufs_integrated == pytest.approx(
            m_loud.lufs_integrated, abs=0.25)

    def test_relative_gate_ignores_quiet_passage(self):
        loud = sine(1000.0, 3.0, amp=0.5)
        quiet = sine(1000.0, 6.0, amp=0.005)  # 40 dB down: relative-gated
        m = am.analyze(np.concatenate([loud, quiet]), RATE)
        expected = SINE_LUFS_0DBFS + 20 * np.log10(0.5)
        assert m.lufs_integrated == pytest.approx(expected, abs=0.3)

    def test_silence_has_no_integrated_loudness(self):
        m = am.analyze(np.zeros((RATE, 1)), RATE)
        assert m.lufs_integrated is None

    def test_too_short_for_a_single_block_returns_none(self):
        m = am.analyze(sine(1000.0, 0.2), RATE)  # < 400 ms
        assert m.lufs_integrated is None
        assert any("short" in n for n in m.notes)

    def test_momentary_and_short_term_track_steady_signal(self):
        m = am.analyze(sine(1000.0, 5.0, amp=0.5), RATE)
        assert m.lufs_momentary_max == pytest.approx(m.lufs_integrated,
                                                     abs=0.3)
        assert m.lufs_short_term_max == pytest.approx(m.lufs_integrated,
                                                      abs=0.3)

    def test_short_term_none_when_under_3_seconds(self):
        m = am.analyze(sine(1000.0, 1.0), RATE)
        assert m.lufs_short_term_max is None
        assert m.lufs_momentary_max is not None


# ------------------------------------------------------- crest / levels ----

class TestCrestAndLevels:
    def test_sine_crest_is_3db(self):
        m = am.analyze(sine(1000.0, 2.0, amp=0.5), RATE)
        assert m.crest_db == pytest.approx(3.0103, abs=0.05)

    def test_square_wave_crest_is_0db(self):
        t = np.arange(2 * RATE) / RATE
        sq = 0.5 * np.where(np.sin(2 * np.pi * 100.0 * t) >= 0, 1.0, -1.0)
        m = am.analyze(sq[:, None], RATE)
        assert m.crest_db == pytest.approx(0.0, abs=0.05)

    def test_peak_and_rms_dbfs(self):
        m = am.analyze(sine(1000.0, 2.0, amp=0.5), RATE)
        assert m.peak_dbfs == pytest.approx(-6.02, abs=0.05)
        assert m.rms_dbfs == pytest.approx(-9.03, abs=0.05)

    def test_true_peak_of_997hz_sine(self):
        m = am.analyze(sine(997.0, 2.0, amp=0.5), RATE)
        assert m.true_peak_dbtp == pytest.approx(-6.02, abs=0.1)

    def test_silence_levels_are_none(self):
        m = am.analyze(np.zeros((RATE, 1)), RATE)
        assert m.crest_db is None
        assert m.peak_dbfs is None
        assert m.rms_dbfs is None
        assert m.clipped is False


class TestClipping:
    def test_hard_clipped_sine_is_flagged(self):
        clipped = np.clip(sine(1000.0, 2.0, amp=1.6), -1.0, 1.0)
        m = am.analyze(clipped, RATE)
        assert m.clipped is True
        assert m.clipped_samples > 100

    def test_clean_full_scale_sine_is_not_flagged(self):
        m = am.analyze(sine(1000.0, 2.0, amp=0.98), RATE)
        assert m.clipped is False


# ---------------------------------------------------------------- bands ----

class TestBandEnergies:
    @pytest.mark.parametrize("freq,band", [
        (100.0, "low"),
        (300.0, "low_mid"),
        (800.0, "mid"),
        (2500.0, "high_mid"),
        (6000.0, "high"),
    ])
    def test_pure_tone_lands_in_its_band(self, freq: float, band: str):
        m = am.analyze(sine(freq, 3.0, amp=0.5), RATE)
        by_name = {b["band"]: b for b in m.bands}
        assert set(by_name) == {"low", "low_mid", "mid", "high_mid", "high"}
        assert by_name[band]["fraction"] > 0.9

    def test_band_edges_are_documented_vocabulary(self):
        names = [b[0] for b in am.GUITAR_BANDS]
        assert names == ["low", "low_mid", "mid", "high_mid", "high"]
        edges = [(b[1], b[2]) for b in am.GUITAR_BANDS]
        # contiguous coverage of the guitar spectrum
        for (_, hi), (lo, _) in zip(edges, edges[1:]):
            assert hi == lo
        assert edges[0][0] == 60.0
        assert edges[-1][1] == 10000.0

    def test_spectral_centroid_of_pure_tone(self):
        m = am.analyze(sine(1000.0, 3.0, amp=0.5), RATE)
        assert m.spectral_centroid_hz == pytest.approx(1000.0, abs=30.0)

    def test_bands_none_for_silence(self):
        m = am.analyze(np.zeros((RATE, 1)), RATE)
        assert m.bands is None


# --------------------------------------------------------------- wav io ----

class TestWavIo:
    def test_reads_pcm16(self, tmp_path: Path):
        path = write_pcm16(tmp_path / "s.wav", sine(1000.0, 1.0, amp=0.5))
        samples, rate = am.load_wav(path)
        assert rate == RATE
        assert samples.shape == (RATE, 1)
        assert float(np.abs(samples).max()) == pytest.approx(0.5, abs=1e-3)

    def test_reads_pcm24(self, tmp_path: Path):
        x = sine(1000.0, 0.5, amp=0.5)
        ints = np.round(x[:, 0] * 8388607.0).astype("<i4")
        frames = b"".join(struct.pack("<i", int(v))[:3] for v in ints)
        with wave.open(str(tmp_path / "s24.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(3)
            w.setframerate(RATE)
            w.writeframes(frames)
        samples, rate = am.load_wav(tmp_path / "s24.wav")
        assert rate == RATE
        assert np.allclose(samples[:, 0], x[:, 0], atol=1e-6)

    def test_reads_pcm32(self, tmp_path: Path):
        x = sine(500.0, 0.25, amp=0.25)
        ints = np.round(x * 2147483647.0).astype("<i4")
        with wave.open(str(tmp_path / "s32.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(4)
            w.setframerate(RATE)
            w.writeframes(ints.tobytes())
        samples, _ = am.load_wav(tmp_path / "s32.wav")
        assert np.allclose(samples, x, atol=1e-8)

    def test_float32_roundtrip_via_write_wav(self, tmp_path: Path):
        x = sine(1000.0, 0.5, amp=0.5, channels=2).astype(np.float32)
        path = am.write_wav_float32(tmp_path / "f32.wav", x, RATE)
        samples, rate = am.load_wav(path)
        assert rate == RATE
        assert samples.shape == x.shape
        assert np.allclose(samples, x, atol=1e-7)

    def test_stereo_pcm16_shape(self, tmp_path: Path):
        path = write_pcm16(tmp_path / "st.wav",
                           sine(1000.0, 0.5, amp=0.5, channels=2))
        samples, _ = am.load_wav(path)
        assert samples.shape == (RATE // 2, 2)

    def test_garbage_file_raises(self, tmp_path: Path):
        bad = tmp_path / "bad.wav"
        bad.write_bytes(b"not a riff file at all" * 10)
        with pytest.raises(am.AudioMetricsError):
            am.load_wav(bad)

    def test_analyze_wav_end_to_end(self, tmp_path: Path):
        path = write_pcm16(tmp_path / "e2e.wav", sine(1000.0, 5.0, amp=0.5))
        m = am.analyze_wav(path)
        assert m.lufs_integrated == pytest.approx(SINE_LUFS_0DBFS - 6.02,
                                                  abs=0.1)
        assert m.rate == RATE
        assert m.channels == 1
        assert m.seconds == pytest.approx(5.0, abs=0.01)


# --------------------------------------------------------- import guard ----

class TestOptionalDependency:
    def test_missing_numpy_hint(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(sys.modules, "numpy", None)
        with pytest.raises(am.AudioMetricsError, match=r"helixgen\[analyze\]"):
            am._np()

    def test_missing_sounddevice_hint(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(sys.modules, "sounddevice", None)
        with pytest.raises(am.AudioMetricsError, match=r"helixgen\[capture\]"):
            am._sounddevice()


# ---------------------------------------------------------------- dict ----

class TestToDict:
    def test_json_safe_dict(self):
        m = am.analyze(sine(1000.0, 1.0, amp=0.5), RATE)
        d = m.to_dict()
        import json
        json.dumps(d)  # must not raise (no -inf / nan / ndarray leakage)
        assert d["lufs_integrated"] == pytest.approx(m.lufs_integrated,
                                                     abs=1e-6)
        assert isinstance(d["bands"], list)
        assert d["channels"] == 1

    def test_silence_dict_has_nulls(self):
        d = am.analyze(np.zeros((RATE, 1)), RATE).to_dict()
        assert d["lufs_integrated"] is None
        assert d["crest_db"] is None
