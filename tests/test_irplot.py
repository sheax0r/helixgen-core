"""Unit tests for the zero-dep IR frequency-response (liveedit/irplot.py)."""
from __future__ import annotations

import cmath
import math
import struct
import wave

import pytest

from helixgen.liveedit import irplot


def _dft(x):
    n = len(x)
    return [sum(x[t] * cmath.exp(-2j * math.pi * k * t / n) for t in range(n)) for k in range(n)]


def test_fft_rejects_non_power_of_two():
    with pytest.raises(ValueError):
        irplot.fft([1 + 0j] * 3)


def test_fft_matches_naive_dft():
    x = [complex(v, 0.0) for v in (1.0, -2.0, 3.0, 0.5, -1.0, 2.0, 0.0, 4.0)]
    got = irplot.fft(x)
    want = _dft(x)
    for a, b in zip(got, want):
        assert abs(a - b) < 1e-9


def test_fft_impulse_is_flat():
    x = [complex(0.0)] * 8
    x[0] = 1 + 0j
    out = irplot.fft(x)
    assert all(abs(abs(v) - 1.0) < 1e-12 for v in out)   # delta -> flat spectrum


def test_fft_cosine_peaks_at_its_bin():
    n = 16
    k = 3
    x = [complex(math.cos(2 * math.pi * k * t / n), 0.0) for t in range(n)]
    mag = [abs(v) for v in irplot.fft(x)]
    assert mag.index(max(mag[: n // 2 + 1])) == k


def _write_wav(path, samples, rate=48000, sampwidth=2, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        if sampwidth == 2:
            frames = struct.pack("<%dh" % len(samples), *[max(-32768, min(32767, int(round(v * 32767)))) for v in samples])
        elif sampwidth == 3:
            frames = bytearray()
            for v in samples:
                iv = max(-8388608, min(8388607, int(round(v * 8388607))))
                iv &= 0xFFFFFF
                frames += bytes((iv & 0xFF, (iv >> 8) & 0xFF, (iv >> 16) & 0xFF))
            frames = bytes(frames)
        else:
            raise AssertionError("test writer only does 16/24-bit")
        w.writeframes(frames)


def test_read_wav_mono_roundtrip_pcm16(tmp_path):
    p = tmp_path / "ir.wav"
    sig = [0.0, 0.5, -0.5, 0.25, -0.25, 0.0, 0.1, -0.1]
    _write_wav(p, sig)
    samples, rate = irplot.read_wav_mono(p)
    assert rate == 48000
    assert len(samples) == len(sig)
    for a, b in zip(samples, sig):
        assert abs(a - b) < 1e-3


def test_read_wav_mono_24bit(tmp_path):
    p = tmp_path / "ir24.wav"
    sig = [0.0, 0.5, -0.5, 0.9, -0.9]
    _write_wav(p, sig, sampwidth=3)
    samples, _ = irplot.read_wav_mono(p)
    for a, b in zip(samples, sig):
        assert abs(a - b) < 1e-4


def test_read_wav_mono_mixes_stereo(tmp_path):
    p = tmp_path / "stereo.wav"
    # L=[1,0], R=[0,1] interleaved -> mono average [0.5, 0.5]
    inter = [1.0, 0.0, 0.0, 1.0]
    _write_wav(p, inter, channels=2)
    samples, _ = irplot.read_wav_mono(p)
    assert len(samples) == 2
    assert abs(samples[0] - 0.5) < 1e-3 and abs(samples[1] - 0.5) < 1e-3


def test_read_wav_float_is_rejected(tmp_path):
    # a WAVE_FORMAT_IEEE_FLOAT header (fmt code 3) is not readable by stdlib wave
    p = tmp_path / "float.wav"
    data = b"RIFF" + struct.pack("<I", 36 + 4) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 3, 1, 48000, 48000 * 4, 4, 32) + b"data" + struct.pack("<I", 4) + b"\x00\x00\x80?"
    p.write_bytes(data)
    with pytest.raises(irplot.IrReadError):
        irplot.read_wav_mono(p)


def test_frequency_response_impulse_is_flat():
    # a unit impulse has a flat magnitude response -> ~0 dB across the band
    sig = [1.0] + [0.0] * 2047
    resp = irplot.frequency_response(sig, 48000, points=64)
    assert resp["freq"] and len(resp["freq"]) == len(resp["db"]) == 64
    assert resp["freq"][0] < resp["freq"][-1]            # ascending
    assert all(f > 0 for f in resp["freq"])
    assert all(abs(d) < 0.5 for d in resp["db"])         # flat near 0 dB


def test_frequency_response_lowpass_rolls_off_highs():
    # a 2-tap moving average [0.5, 0.5] is a gentle low-pass: highs < lows
    sig = [0.5, 0.5] + [0.0] * 2046
    resp = irplot.frequency_response(sig, 48000, points=80)
    lo_avg = sum(resp["db"][:10]) / 10
    hi_avg = sum(resp["db"][-10:]) / 10
    assert hi_avg < lo_avg - 3.0                          # treble clearly attenuated


def test_frequency_response_empty_is_safe():
    assert irplot.frequency_response([], 48000) == {"freq": [], "db": [], "rate": 48000}
    assert irplot.frequency_response([1.0], 0) == {"freq": [], "db": [], "rate": 0}


def test_ir_response_end_to_end(tmp_path):
    p = tmp_path / "cab.wav"
    _write_wav(p, [1.0] + [0.0] * 1023)                   # impulse
    resp = irplot.ir_response(p, points=48)
    assert len(resp["db"]) == 48 and all(abs(d) < 0.5 for d in resp["db"])
