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

    def test_true_peak_of_sine_truncated_at_a_peak(self):
        # A --record capture stops mid-waveform. FFT oversampling treats the
        # buffer as periodic, so the end->start discontinuity used to ring
        # (Gibbs) and over-read by >1 dB. The true peak of a sine IS its
        # amplitude: a -6.02 dBFS sine must read ~-6.02 dBTP, however it is
        # truncated.
        x = sine(997.0, 2.0, amp=0.5)[:, 0]
        peak_idx = int(np.argmax(x[:RATE]))
        truncated = x[: peak_idx + 1][:, None]  # ends exactly on a peak
        m = am.analyze(truncated, RATE)
        assert m.true_peak_dbtp == pytest.approx(-6.02, abs=0.1)

    def test_true_peak_still_sees_intersample_peaks_mid_signal(self):
        # The edge fix must not suppress genuine inter-sample overs: a
        # 12 kHz sine at 48 kHz samples at 45 degree offsets, so the sample
        # peak under-reads by 3.01 dB while the true peak is the amplitude.
        t = np.arange(RATE) / RATE
        x = 0.5 * np.sin(2 * np.pi * 12000.0 * t + np.pi / 4)
        m = am.analyze(x[:, None], RATE)
        assert m.peak_dbfs == pytest.approx(-9.03, abs=0.05)
        assert m.true_peak_dbtp == pytest.approx(-6.02, abs=0.15)

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

    def test_two_sample_buffer_with_clipped_sample_does_not_crash(self):
        # n < _CLIP_RUN made the shifted-slice AND raise a ValueError.
        m = am.analyze(np.array([[1.0], [0.0]]), RATE)
        assert m.clipped is False  # can't hold a 4-sample run in 2 samples
        assert m.clipped_samples == 1


# ---------------------------------------------------------- non-finite ----

class TestNonFiniteSamples:
    def _with_nonfinite(self) -> np.ndarray:
        x = sine(1000.0, 2.0, amp=0.5)
        x[100, 0] = np.nan
        x[200, 0] = np.inf
        x[300, 0] = -np.inf
        return x

    def test_metrics_are_finite_and_noted(self):
        m = am.analyze(self._with_nonfinite(), RATE)
        assert any("non-finite" in n for n in m.notes)
        # a handful of zeroed samples barely moves a 2 s sine's metrics
        assert m.peak_dbfs == pytest.approx(-6.02, abs=0.05)
        assert m.lufs_integrated == pytest.approx(SINE_LUFS_0DBFS - 6.02,
                                                  abs=0.15)
        assert not any("silence" in n for n in m.notes)

    def test_to_dict_is_strict_json_safe(self):
        import json
        d = am.analyze(self._with_nonfinite(), RATE).to_dict()
        # allow_nan=False raises on NaN/Infinity — the pinned contract is
        # "Undefined metrics are null, never NaN/-inf"
        json.dumps(d, allow_nan=False)

    def test_all_nonfinite_is_reported_not_silence(self):
        x = np.full((RATE, 1), np.nan)
        m = am.analyze(x, RATE)
        assert any("non-finite" in n for n in m.notes)
        import json
        json.dumps(m.to_dict(), allow_nan=False)

    def test_nonfinite_float32_wav_end_to_end(self, tmp_path: Path):
        path = am.write_wav_float32(tmp_path / "nf.wav",
                                    self._with_nonfinite(), RATE)
        m = am.analyze_wav(path)
        assert any("non-finite" in n for n in m.notes)
        import json
        json.dumps(m.to_dict(), allow_nan=False)


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

    def test_multiple_data_chunks_first_wins(self, tmp_path: Path):
        # RIFF convention: the first `data` chunk is the audio; the reader
        # used to take the last one.
        first = np.full(100, 0.25, dtype="<f4").tobytes()
        second = np.full(100, 0.75, dtype="<f4").tobytes()
        fmt = struct.pack("<HHIIHH", 3, 1, RATE, RATE * 4, 4, 32)
        body = (b"WAVE"
                + b"fmt " + struct.pack("<I", len(fmt)) + fmt
                + b"data" + struct.pack("<I", len(first)) + first
                + b"data" + struct.pack("<I", len(second)) + second)
        path = tmp_path / "two-data.wav"
        path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        samples, rate = am.load_wav(path)
        assert rate == RATE
        assert samples.shape == (100, 1)
        assert np.allclose(samples, 0.25)

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


# --------------------------------------------------------------- record ----

class TestRecordValidation:
    def _fake_sounddevice(self, calls: list):
        import types
        mod = types.ModuleType("sounddevice")

        class PortAudioError(Exception):
            pass

        def rec(frames, samplerate, channels, dtype, device=None):
            calls.append((frames, samplerate, channels))
            return np.zeros((frames, channels), dtype=np.float32)

        mod.PortAudioError = PortAudioError
        mod.rec = rec
        mod.wait = lambda: None
        return mod

    @pytest.mark.parametrize("kwargs,fragment", [
        ({"rate": 0}, "rate"),
        ({"rate": -48000}, "rate"),
        ({"channels": 0}, "channel"),
        ({"channels": -1}, "channel"),
    ])
    def test_rejects_nonpositive_rate_and_channels(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            kwargs: dict, fragment: str):
        calls: list = []
        monkeypatch.setitem(sys.modules, "sounddevice",
                            self._fake_sounddevice(calls))
        out = tmp_path / "cap.wav"
        with pytest.raises(am.AudioMetricsError, match=fragment):
            am.record_wav(out, 1.0, **kwargs)
        assert calls == []           # rejected before touching the device
        assert not out.exists()      # ... and before writing anything

    def test_portaudio_error_becomes_audio_metrics_error(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        calls: list = []
        mod = self._fake_sounddevice(calls)

        def rec(*a, **k):
            raise mod.PortAudioError("Invalid device")

        mod.rec = rec
        monkeypatch.setitem(sys.modules, "sounddevice", mod)
        with pytest.raises(am.AudioMetricsError, match="Invalid device"):
            am.record_wav(tmp_path / "cap.wav", 1.0)


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
