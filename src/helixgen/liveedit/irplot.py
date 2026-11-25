"""Frequency-response of a user IR, with ZERO new dependencies.

The Stadium editor plots a loaded IR's magnitude response. For a user IR
(an ``"ir"``-category loader block) the impulse lives in the user's local
IR library, so helixgen can read it and plot it; built-in cab IRs are DSP
internal and not extractable (see parity bead 81v.23).

Deliberately stdlib-only: the ``helixgen[device]`` extra the live editor runs
under has no numpy and no libsndfile. Samples come through stdlib ``wave`` and
a small iterative radix-2 FFT lives here. The input IR is short (a cab IR is a
few thousand samples), so a pure-Python FFT is comfortably fast.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import List, Tuple

# Cap the transform size: a cab IR's meaningful part is short, and this bounds
# the pure-Python FFT's cost regardless of an oversized source file.
_MAX_N = 16384
_MIN_HZ = 20.0
_MAX_HZ = 20000.0
_SMOOTH_OCT = 1.0 / 6.0   # fractional-octave smoothing width (each side)


class IrReadError(ValueError):
    """The IR .wav could not be read as PCM (e.g. IEEE-float, or corrupt)."""


def read_wav_mono(path: Path | str) -> Tuple[List[float], int]:
    """Read a PCM WAV as mono float samples in roughly [-1, 1] + its rate.

    Handles 8/16/24/32-bit PCM (stdlib ``wave`` plus a manual 24-bit unpack),
    mixing multiple channels down to mono. Raises :class:`IrReadError` for
    formats ``wave`` cannot read (notably IEEE-float wavs).
    """
    try:
        with wave.open(str(path), "rb") as w:
            ch = w.getnchannels()
            sw = w.getsampwidth()
            rate = w.getframerate()
            n = w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise IrReadError(f"not a readable PCM wav: {e}") from e
    if ch < 1 or rate <= 0 or sw not in (1, 2, 3, 4):
        raise IrReadError(f"unsupported wav: {ch}ch {sw*8}bit {rate}Hz")
    samples = _decode_pcm(raw, sw)
    if ch > 1:                       # interleaved -> mono average
        mono = [0.0] * (len(samples) // ch)
        for i in range(len(mono)):
            acc = 0.0
            base = i * ch
            for c in range(ch):
                acc += samples[base + c]
            mono[i] = acc / ch
        samples = mono
    return samples, rate


def _decode_pcm(raw: bytes, sampwidth: int) -> List[float]:
    """Interleaved PCM bytes -> floats in [-1, 1). 8-bit PCM is unsigned."""
    if sampwidth == 1:               # 8-bit PCM is unsigned (offset 128)
        return [(b - 128) / 128.0 for b in raw]
    if sampwidth == 2:
        count = len(raw) // 2
        ints = struct.unpack("<%dh" % count, raw[: count * 2])
        return [v / 32768.0 for v in ints]
    if sampwidth == 3:               # 24-bit: no struct code, unpack by hand
        out = []
        for i in range(0, len(raw) - 2, 3):
            v = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            out.append(v / 8388608.0)
        return out
    count = len(raw) // 4            # 32-bit PCM
    ints = struct.unpack("<%di" % count, raw[: count * 4])
    return [v / 2147483648.0 for v in ints]


def fft(x: List[complex]) -> List[complex]:
    """Iterative radix-2 Cooley-Tukey FFT. ``len(x)`` must be a power of two."""
    n = len(x)
    if n & (n - 1):
        raise ValueError("fft length must be a power of two")
    if n <= 1:
        return list(x)
    a = list(x)
    # bit-reversal permutation
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        ang = -2j * math.pi / length
        step = [math.e ** (ang * k) for k in range(length // 2)]
        half = length // 2
        for start in range(0, n, length):
            for k in range(half):
                u = a[start + k]
                v = a[start + k + half] * step[k]
                a[start + k] = u + v
                a[start + k + half] = u - v
        length <<= 1
    return a


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


def frequency_response(samples: List[float], rate: int, *, points: int = 160) -> dict:
    """Magnitude response as log-spaced ``{"freq": [...Hz], "db": [...dB]}``.

    Fractional-octave smoothed (so a raw IR's spiky spectrum reads as a
    response curve) and normalised so the peak sits at 0 dB. Returns empty
    lists if there is no signal.
    """
    if rate <= 0 or not samples:
        return {"freq": [], "db": [], "rate": rate}
    n = min(_next_pow2(len(samples)), _MAX_N)
    buf = samples[:n] + [0.0] * (n - len(samples)) if len(samples) < n else samples[:n]
    spec = fft([complex(v, 0.0) for v in buf])
    half = n // 2
    mag = [abs(spec[k]) for k in range(half)]          # bin k -> k*rate/n Hz
    bin_hz = rate / n
    hi = min(_MAX_HZ, rate / 2.0)
    lo = max(_MIN_HZ, bin_hz)
    if hi <= lo:
        return {"freq": [], "db": [], "rate": rate}
    ratio = math.log(hi / lo)
    freqs, mags = [], []
    up = 2.0 ** _SMOOTH_OCT
    for i in range(points):
        f = lo * math.exp(ratio * i / (points - 1))
        k0 = int(math.floor((f / up) / bin_hz))
        k1 = int(math.ceil((f * up) / bin_hz))
        k0 = max(1, k0)
        k1 = min(half - 1, max(k0, k1))
        band = mag[k0:k1 + 1]
        if band:
            m = sum(band) / len(band)                  # 1/6-oct average
        else:                                          # sparse bins at low f
            m = mag[min(max(1, int(round(f / bin_hz))), half - 1)]
        freqs.append(f)
        mags.append(m)
    peak = max(mags) or 1.0
    floor_db = -60.0
    db = [max(floor_db, 20.0 * math.log10(m / peak)) if m > 0 else floor_db for m in mags]
    return {"freq": freqs, "db": db, "rate": rate}


def ir_response(wav_path: Path | str, *, points: int = 160) -> dict:
    """Read an IR .wav and return its smoothed log-spaced magnitude response."""
    samples, rate = read_wav_mono(wav_path)
    return frequency_response(samples, rate, points=points)
