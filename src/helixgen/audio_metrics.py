"""Offline audio-quality metrics — backlog #62 phase 3 (file-based half).

Given a WAV file (or an in-memory sample buffer) this module computes the
metric set from the loudness-feedback spec
(`docs/superpowers/specs/2026-07-14-loudness-feedback-normalization.md` §4.2):

* **Integrated LUFS** per ITU-R BS.1770-4: K-weighting (high-shelf +
  high-pass biquad cascade), 400 ms blocks with 75 % overlap, the -70 LUFS
  absolute gate and the -10 LU relative gate. Momentary (400 ms) and
  short-term (3 s) maxima ride along for free.
* **Crest factor** in dB (sample peak vs RMS) — how compressed/saturated the
  signal is. Plus peak dBFS, RMS dBFS, and an approximate true peak (dBTP,
  4x FFT oversampling).
* **FFT band energies** over a 5-band guitar vocabulary (see GUITAR_BANDS),
  spectral centroid, and a clipping heuristic.

K-weighting coefficients are derived at the file's actual sample rate by
bilinear transform of the analog prototype (high shelf fc=1681.97 Hz /
G=+3.99984 dB / Q=0.7072, high-pass fc=38.135 Hz / Q=0.5003 — the De Man
reconstruction); at 48 kHz they reproduce the BS.1770 table coefficients. The IIR
cascade is applied as a truncated impulse response via FFT convolution — the
truncation error is < 1e-15 of full scale, and it keeps the hot path in
numpy (there is no stdlib FFT/IIR).

**Band-edge provenance.** The IR cab-pack catalog (`irs/_catalog/`, gitignored
— paid packs stay local) derives its measured bright/dark/beefy/tight tags
from a 5-band FFT pass, but its exact band edges are not recoverable from
this repository. The edges below are therefore *provisional*, anchored on the
spec's trouble-band vocabulary (mud 200-400 Hz, boxiness 400-800 Hz,
harshness 2.5-4 kHz, fizz >= 6 kHz) and guitar practice; they need
reconciliation with the catalog's actual pass (see the PR / backlog residual):

    low       60 -   200 Hz   thump / weight
    low_mid  200 -   500 Hz   beef / mud
    mid      500 -  1200 Hz   body / boxiness
    high_mid 1200 - 4000 Hz   presence / bite / harshness
    high     4000 - 10000 Hz  fizz / air

Dependencies: numpy only, and only at call time — install with
``pip install 'helixgen[analyze]'``. Recording (``record_wav``) additionally
needs the ``sounddevice`` PortAudio binding: ``pip install
'helixgen[capture]'``. Both imports are lazy so a stdlib-only install keeps
working, mirroring the ``[device]`` extra pattern.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

__all__ = [
    "AudioMetrics",
    "AudioMetricsError",
    "GUITAR_BANDS",
    "analyze",
    "analyze_wav",
    "load_wav",
    "record_wav",
    "write_wav_float32",
]


class AudioMetricsError(Exception):
    """Raised for unreadable audio, bad parameters, or missing optional deps."""


# (name, lo_hz, hi_hz) — provisional 5-band guitar vocabulary; see module
# docstring for provenance (needs reconciliation with the IR catalog's pass).
GUITAR_BANDS: tuple[tuple[str, float, float], ...] = (
    ("low", 60.0, 200.0),
    ("low_mid", 200.0, 500.0),
    ("mid", 500.0, 1200.0),
    ("high_mid", 1200.0, 4000.0),
    ("high", 4000.0, 10000.0),
)

# BS.1770 K-weighting analog prototype (the De Man reconstruction — at
# 48 kHz these yield the coefficient table printed in the standard).
_SHELF_FC, _SHELF_GAIN_DB, _SHELF_Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
_SHELF_VB_EXP = 0.4996667741545416
_HIPASS_FC, _HIPASS_Q = 38.13547087602444, 0.5003270373238773

_BLOCK_S = 0.400   # BS.1770 gating block
_HOP_S = 0.100     # 75 % overlap
_SHORT_TERM_S = 3.0
_ABS_GATE_LUFS = -70.0
_REL_GATE_LU = -10.0
_LUFS_OFFSET = -0.691

_CLIP_THRESHOLD = 0.999
_CLIP_RUN = 4  # consecutive samples at/above threshold that flag clipping

_TRUE_PEAK_MAX_SAMPLES = 10_000_000  # skip 4x oversampling above this


def _np():
    """Lazy numpy import with the analyze-extra install hint."""
    try:
        import numpy
    except ImportError as exc:
        raise AudioMetricsError(
            "audio analysis needs numpy; install with "
            "`pip install 'helixgen[analyze]'`"
        ) from exc
    return numpy


def _sounddevice():
    """Lazy sounddevice import with the capture-extra install hint."""
    try:
        import sounddevice
    except ImportError as exc:
        raise AudioMetricsError(
            "recording needs sounddevice (PortAudio); install with "
            "`pip install 'helixgen[capture]'` "
            "(macOS may also need `brew install portaudio`)"
        ) from exc
    return sounddevice


# ------------------------------------------------------------------ wav ----

def load_wav(path: Path | str) -> tuple["object", int]:
    """Read a WAV file → (float64 samples shaped (frames, channels) in
    [-1, 1], sample_rate).

    Self-contained RIFF parser (stdlib `wave` rejects IEEE-float files, and
    libsndfile is only a requirement of the IR-hash path): PCM 8/16/24/32-bit
    and IEEE float32/64, plus WAVE_FORMAT_EXTENSIBLE wrapping either.
    Raises AudioMetricsError on anything unreadable.
    """
    np = _np()
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AudioMetricsError(f"cannot read {path}: {exc}") from exc
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise AudioMetricsError(f"{path}: not a RIFF/WAVE file")

    fmt = None
    data = None
    pos = 12
    while pos + 8 <= len(raw):
        cid, size = struct.unpack_from("<4sI", raw, pos)
        body = raw[pos + 8: pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data" and data is None:  # RIFF: first data chunk wins
            data = body
        pos += 8 + size + (size & 1)  # chunks are word-aligned
    if fmt is None or len(fmt) < 16:
        raise AudioMetricsError(f"{path}: missing/short fmt chunk")
    if data is None:
        raise AudioMetricsError(f"{path}: missing data chunk")

    tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == 0xFFFE and len(fmt) >= 26:  # WAVE_FORMAT_EXTENSIBLE
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if channels < 1 or rate < 1:
        raise AudioMetricsError(f"{path}: invalid fmt (channels={channels}, "
                                f"rate={rate})")

    bytes_per = bits // 8
    frames = len(data) // (bytes_per * channels) if bytes_per else 0
    data = data[: frames * bytes_per * channels]
    if frames == 0:
        raise AudioMetricsError(f"{path}: no audio frames")

    if tag == 1:  # PCM
        if bits == 8:
            x = (np.frombuffer(data, dtype=np.uint8).astype(np.float64)
                 - 128.0) / 128.0
        elif bits == 16:
            x = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
        elif bits == 24:
            b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
            ints = (b[:, 0].astype(np.int32)
                    | (b[:, 1].astype(np.int32) << 8)
                    | (b[:, 2].astype(np.int32) << 16))
            ints = (ints ^ 0x800000) - 0x800000  # sign-extend
            x = ints.astype(np.float64) / 8388608.0
        elif bits == 32:
            x = np.frombuffer(data, dtype="<i4").astype(np.float64) / 2147483648.0
        else:
            raise AudioMetricsError(f"{path}: unsupported PCM depth {bits}")
    elif tag == 3:  # IEEE float
        if bits == 32:
            x = np.frombuffer(data, dtype="<f4").astype(np.float64)
        elif bits == 64:
            x = np.frombuffer(data, dtype="<f8").astype(np.float64)
        else:
            raise AudioMetricsError(f"{path}: unsupported float depth {bits}")
    else:
        raise AudioMetricsError(
            f"{path}: unsupported WAV format tag {tag} (PCM and IEEE float "
            "only; convert with `sox in.wav -e signed -b 16 out.wav`)")

    return x.reshape(-1, channels), int(rate)


def write_wav_float32(path: Path | str, samples, rate: int) -> Path:
    """Write (frames, channels) float samples as a canonical IEEE-float32 WAV."""
    np = _np()
    path = Path(path)
    x = np.asarray(samples, dtype="<f4")
    if x.ndim == 1:
        x = x[:, None]
    frames, channels = x.shape
    payload = x.tobytes()
    byte_rate = rate * channels * 4
    fmt = struct.pack("<HHIIHH", 3, channels, rate, byte_rate, channels * 4, 32)
    fact = struct.pack("<I", frames)
    body = (b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"fact" + struct.pack("<I", len(fact)) + fact
            + b"data" + struct.pack("<I", len(payload)) + payload)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def record_wav(path: Path | str, seconds: float, *, rate: int = 48000,
               channels: int = 2, device: str | int | None = None) -> Path:
    """EXPERIMENTAL: record `seconds` from an audio input device to a
    float32 WAV at `path` (the Stadium shows up as a USB audio interface).

    Requires the `capture` extra (sounddevice/PortAudio). Blocks until the
    capture completes. Untested against real hardware — see backlog #62.
    """
    if seconds <= 0:
        raise AudioMetricsError("--record needs a positive duration")
    if rate <= 0:
        raise AudioMetricsError(f"--record needs a positive sample rate "
                                f"(got --rate {rate})")
    if channels <= 0:
        raise AudioMetricsError(f"--record needs a positive channel count "
                                f"(got --channels {channels})")
    sd = _sounddevice()
    np = _np()
    frames = int(round(seconds * rate))
    # PortAudioError may not exist on a stubbed module; () never matches.
    pa_error = getattr(sd, "PortAudioError", None) or ()
    try:
        data = sd.rec(frames, samplerate=rate, channels=channels,
                      dtype="float32", device=device)
        sd.wait()
    except pa_error as exc:
        raise AudioMetricsError(
            f"audio capture failed: {exc} (check --input against "
            "`python -m sounddevice` and that --rate/--channels are "
            "supported by the device)") from exc
    return write_wav_float32(path, np.asarray(data), rate)


# ---------------------------------------------------------- K-weighting ----

def _biquad_coeffs(rate: int) -> tuple[tuple, tuple]:
    """K-weighting biquad cascade (shelf, high-pass) at `rate`, each as
    (b, a) with a0 == 1. Bilinear-transform reconstruction of the BS.1770
    analog prototype (the De Man derivation, as used by every serious
    BS.1770 implementation for non-48 kHz rates); at 48 kHz it reproduces
    the standard's printed coefficient table to ~1e-6."""
    import math

    # stage 1: high shelf
    K = math.tan(math.pi * _SHELF_FC / rate)
    Vh = 10.0 ** (_SHELF_GAIN_DB / 20.0)
    Vb = Vh ** _SHELF_VB_EXP
    denom = 1.0 + K / _SHELF_Q + K * K
    shelf = (
        ((Vh + Vb * K / _SHELF_Q + K * K) / denom,
         2.0 * (K * K - Vh) / denom,
         (Vh - Vb * K / _SHELF_Q + K * K) / denom),
        (1.0,
         2.0 * (K * K - 1.0) / denom,
         (1.0 - K / _SHELF_Q + K * K) / denom),
    )

    # stage 2: high-pass (b fixed at [1, -2, 1], as in the ITU table)
    K = math.tan(math.pi * _HIPASS_FC / rate)
    denom = 1.0 + K / _HIPASS_Q + K * K
    hipass = (
        (1.0, -2.0, 1.0),
        (1.0,
         2.0 * (K * K - 1.0) / denom,
         (1.0 - K / _HIPASS_Q + K * K) / denom),
    )
    return shelf, hipass


@lru_cache(maxsize=8)
def _k_weighting_fir(rate: int) -> "object":
    """Truncated impulse response of the K-weighting cascade at `rate`.

    The high-pass pole radius keeps the tail decaying ~e^-40 within the tap
    count below, so truncation error is negligible; FFT convolution with
    this FIR is then an (effectively exact) stand-in for the IIR cascade.
    """
    np = _np()
    taps = max(8192, 1 << (int(0.17 * rate)).bit_length())
    x = np.zeros(taps)
    x[0] = 1.0
    for b, a in _biquad_coeffs(rate):
        y = np.zeros(taps)
        x0 = x1 = y0 = y1 = 0.0
        b0, b1, b2 = b
        _, a1, a2 = a
        for i in range(taps):
            xi = x[i]
            yi = b0 * xi + b1 * x0 + b2 * x1 - a1 * y0 - a2 * y1
            y[i] = yi
            x1, x0 = x0, xi
            y1, y0 = y0, yi
        x = y
    return x


def _k_weight(x, rate: int):
    """Apply K-weighting to (frames, channels) samples via FFT convolution."""
    np = _np()
    fir = _k_weighting_fir(rate)
    n, ch = x.shape
    size = 1 << (n + len(fir) - 1).bit_length()
    firf = np.fft.rfft(fir, size)
    out = np.empty_like(x)
    for c in range(ch):
        out[:, c] = np.fft.irfft(np.fft.rfft(x[:, c], size) * firf, size)[:n]
    return out


# ------------------------------------------------------------- loudness ----

def _block_loudness(weighted, rate: int, window_s: float, hop_s: float):
    """Per-block loudness (LUFS) + per-block channel-summed mean square.
    Returns (loudness ndarray, mean_square ndarray); empty when the signal
    is shorter than one window."""
    np = _np()
    n = weighted.shape[0]
    block = int(round(window_s * rate))
    hop = max(1, int(round(hop_s * rate)))
    if n < block:
        return np.empty(0), np.empty(0)
    csum = np.vstack([np.zeros((1, weighted.shape[1])),
                      np.cumsum(weighted * weighted, axis=0)])
    starts = np.arange(0, n - block + 1, hop)
    z = (csum[starts + block] - csum[starts]) / block  # (blocks, ch)
    zsum = z.sum(axis=1)  # unit channel weights (mono/stereo captures)
    with np.errstate(divide="ignore"):
        loud = _LUFS_OFFSET + 10.0 * np.log10(zsum)
    return loud, zsum


def _loudness_stats(x, rate: int, notes: list[str]):
    """(integrated, momentary_max, short_term_max) per BS.1770-4."""
    np = _np()
    weighted = _k_weight(x, rate)

    loud, zsum = _block_loudness(weighted, rate, _BLOCK_S, _HOP_S)
    if loud.size == 0:
        notes.append("too short for a 400 ms gating block; LUFS omitted")
        return None, None, None

    finite = loud[np.isfinite(loud)]
    momentary_max = float(finite.max()) if finite.size else None

    st_loud, _ = _block_loudness(weighted, rate, _SHORT_TERM_S, _HOP_S)
    st_finite = st_loud[np.isfinite(st_loud)]
    short_term_max = float(st_finite.max()) if st_finite.size else None

    above_abs = loud > _ABS_GATE_LUFS
    if not above_abs.any():
        notes.append("every 400 ms block sits below the -70 LUFS absolute "
                     "gate (silence?); integrated LUFS omitted")
        return None, momentary_max, short_term_max
    gamma_r = (_LUFS_OFFSET + 10.0 * np.log10(zsum[above_abs].mean())
               + _REL_GATE_LU)
    gated = above_abs & (loud > gamma_r)
    if not gated.any():  # defensive; can't normally happen
        return None, momentary_max, short_term_max
    integrated = float(_LUFS_OFFSET + 10.0 * np.log10(zsum[gated].mean()))
    return integrated, momentary_max, short_term_max


# ------------------------------------------------------------- spectrum ----

def _welch_psd(x, rate: int):
    """Hann-windowed averaged periodogram over all channels.
    Returns (freqs, psd) — psd in arbitrary power units (only ratios and
    centroids are consumed)."""
    np = _np()
    n = x.shape[0]
    nper = min(8192, n)
    hop = max(1, nper // 2)
    win = np.hanning(nper)
    freqs = np.fft.rfftfreq(nper, 1.0 / rate)
    acc = np.zeros(freqs.shape)
    count = 0
    for c in range(x.shape[1]):
        for s in range(0, n - nper + 1, hop):
            seg = x[s:s + nper, c] * win
            acc += np.abs(np.fft.rfft(seg)) ** 2
            count += 1
    return freqs, acc / max(count, 1)


def _band_energies(freqs, psd):
    """5-band energy fractions (relative to the 60 Hz-10 kHz vocabulary
    span) + spectral centroid. Returns (bands|None, centroid|None)."""
    np = _np()
    total_all = float(psd.sum())
    if total_all <= 0.0:
        return None, None
    centroid = float((freqs * psd).sum() / total_all)

    powers = []
    for name, lo, hi in GUITAR_BANDS:
        mask = (freqs >= lo) & (freqs < hi)
        powers.append((name, lo, hi, float(psd[mask].sum())))
    span_total = sum(p[3] for p in powers)
    if span_total <= 0.0:
        return None, centroid
    bands = []
    for name, lo, hi, p in powers:
        frac = p / span_total
        db = 10.0 * np.log10(frac) if frac > 0.0 else -120.0
        bands.append({"band": name, "lo_hz": lo, "hi_hz": hi,
                      "fraction": float(frac),
                      "db_rel": float(max(db, -120.0))})
    return bands, centroid


_TRUE_PEAK_PAD = 256   # reflect-pad samples per edge before FFT oversampling
_TRUE_PEAK_TRIM = 16   # edge samples whose interpolation is not trusted


def _true_peak(x, notes: list[str]) -> float | None:
    """Approximate true peak (linear) via 4x FFT oversampling; falls back to
    the sample peak (with a note) on very long signals.

    The FFT treats the buffer as periodic, so a capture truncated
    mid-waveform has an end->start discontinuity whose Gibbs ringing used to
    over-read by >1 dB. Two-part edge handling:

    * each channel is reflect-padded by _TRUE_PEAK_PAD samples before the
      FFT and only the original span is kept, moving the wraparound seam far
      enough away that its (1/distance) ringing decays to ~0.02 dB;
    * the reflection itself still kinks the slope at each edge, so the
      outermost _TRUE_PEAK_TRIM samples' interpolation is excluded from the
      max — the plain sample peak covers that region instead (a genuine
      inter-sample over within a few samples of either end is under-read to
      the sample peak; never over-read).
    """
    np = _np()
    n = x.shape[0]
    sample_peak = float(np.abs(x).max())
    if sample_peak == 0.0:
        return None
    if n > _TRUE_PEAK_MAX_SAMPLES:
        notes.append("signal too long for 4x oversampling; true peak is the "
                     "sample peak")
        return sample_peak
    pad = min(n - 1, _TRUE_PEAK_PAD)
    trim = 4 * _TRUE_PEAK_TRIM
    peak = sample_peak
    for c in range(x.shape[1]):
        col = x[:, c]
        if pad:
            col = np.concatenate([col[pad:0:-1], col,
                                  col[-2:-2 - pad:-1]])
        m = col.shape[0]
        spec = np.fft.rfft(col)
        up = np.fft.irfft(spec, 4 * m) * 4.0
        core = up[4 * pad + trim: 4 * (pad + n) - trim]
        if core.size:
            peak = max(peak, float(np.abs(core).max()))
    return peak


def _clipping(x) -> tuple[bool, int]:
    """Clipping heuristic: samples at/above _CLIP_THRESHOLD of full scale;
    flagged when any channel holds the threshold for _CLIP_RUN consecutive
    samples (a clean full-scale sine only grazes it for 1-2)."""
    np = _np()
    mask = np.abs(x) >= _CLIP_THRESHOLD
    count = int(mask.sum())
    if count == 0:
        return False, 0
    end = max(0, x.shape[0] - _CLIP_RUN + 1)  # 0 when shorter than a run
    run = mask[:end].copy()
    for k in range(1, _CLIP_RUN):
        run &= mask[k: end + k]
    return bool(run.any()), count


# -------------------------------------------------------------- results ----

@dataclass
class AudioMetrics:
    """Structured analysis result; `to_dict()` is the JSON contract."""
    seconds: float
    rate: int
    channels: int
    file: str | None = None
    lufs_integrated: float | None = None
    lufs_momentary_max: float | None = None
    lufs_short_term_max: float | None = None
    peak_dbfs: float | None = None
    true_peak_dbtp: float | None = None
    rms_dbfs: float | None = None
    crest_db: float | None = None
    clipped: bool = False
    clipped_samples: int = 0
    spectral_centroid_hz: float | None = None
    bands: list[dict] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-safe dict (plain Python floats/ints/bools, None for
        undefined metrics — never NaN/-inf)."""
        def _f(v):
            if v is None:
                return None
            v = float(v)
            return v if math.isfinite(v) else None  # belt-and-braces
        return {
            "file": self.file,
            "seconds": float(self.seconds),
            "rate": int(self.rate),
            "channels": int(self.channels),
            "lufs_integrated": _f(self.lufs_integrated),
            "lufs_momentary_max": _f(self.lufs_momentary_max),
            "lufs_short_term_max": _f(self.lufs_short_term_max),
            "peak_dbfs": _f(self.peak_dbfs),
            "true_peak_dbtp": _f(self.true_peak_dbtp),
            "rms_dbfs": _f(self.rms_dbfs),
            "crest_db": _f(self.crest_db),
            "clipped": bool(self.clipped),
            "clipped_samples": int(self.clipped_samples),
            "spectral_centroid_hz": _f(self.spectral_centroid_hz),
            "bands": self.bands,
            "notes": list(self.notes),
        }


def analyze(samples, rate: int, *, file: str | None = None) -> AudioMetrics:
    """Compute the full metric set over a sample buffer.

    `samples`: array-like shaped (frames,) or (frames, channels), float in
    [-1, 1] full scale. Undefined metrics (digital silence, files shorter
    than a gating block) come back as None with an explanatory note rather
    than raising.
    """
    np = _np()
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] == 0:
        raise AudioMetricsError("samples must be a non-empty 1-D or 2-D array")
    if rate <= 0:
        raise AudioMetricsError(f"invalid sample rate {rate}")
    n, channels = x.shape

    notes: list[str] = []
    finite = np.isfinite(x)
    if not finite.all():
        bad = int(x.size - int(finite.sum()))
        x = np.where(finite, x, 0.0)
        notes.append(f"{bad} non-finite sample{'s' if bad != 1 else ''} "
                     "(NaN/Inf) zeroed before analysis")
    if channels > 2:
        notes.append(f"{channels} channels: all weighted 1.0 (BS.1770 "
                     "surround weights not applied)")

    m = AudioMetrics(seconds=n / rate, rate=rate, channels=channels,
                     file=file, notes=notes)

    peak = float(np.abs(x).max())
    if peak > 0.0:
        rms = float(np.sqrt((x * x).mean()))
        m.peak_dbfs = 20.0 * np.log10(peak)
        m.rms_dbfs = 20.0 * np.log10(rms)
        m.crest_db = m.peak_dbfs - m.rms_dbfs
        tp = _true_peak(x, notes)
        m.true_peak_dbtp = 20.0 * np.log10(tp) if tp else None
    else:
        notes.append("digital silence: level metrics omitted")

    m.clipped, m.clipped_samples = _clipping(x)

    (m.lufs_integrated, m.lufs_momentary_max,
     m.lufs_short_term_max) = _loudness_stats(x, rate, notes)

    freqs, psd = _welch_psd(x, rate)
    m.bands, m.spectral_centroid_hz = _band_energies(freqs, psd)
    return m


def analyze_wav(path: Path | str) -> AudioMetrics:
    """load_wav + analyze, tagging the result with the file path."""
    samples, rate = load_wav(path)
    return analyze(samples, rate, file=str(path))
