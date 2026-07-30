"""Sox-backed audio capture for measurement (backlog #62, hc-57h).

The Stadium presents itself as a USB audio interface, and — measured on
hardware (Stadium XL fw 1.3.2, 2026-07-30) — **that capture is DOWNSTREAM of
the output block's gain**: with the output gain at 8/14/20/25 dB the captured
RMS read -30.20/-24.20/-18.20/-13.20 dBFS, i.e. the writes landed 1:1. So a
capture-based metric measures what the listener actually hears, and a written
trim can be *confirmed* by re-capturing.

This module is the plumbing between that USB stream and
:mod:`helixgen.audio_metrics` (which already implements BS.1770). It shells
out to **sox** rather than using the ``[capture]`` extra's sounddevice path:
sox is the invocation proven against real hardware, and it addresses the
input device BY NAME so the user's system default input is never touched.

Two things about the sox command line are load-bearing:

* **Format flags must precede the input device.** ``sox -c 1 -b 24 -r 48000
  -t coreaudio 'Helix Stadium XL' cap.wav`` pins the capture; move any of
  those flags after the device name and they describe the OUTPUT file
  instead, so sox opens the device at whatever it defaults to and silently
  RESAMPLES. :func:`capture_argv` exists to make that ordering impossible to
  get wrong.
* **Capture continuously and analyze the middle** (:func:`analyze_capture`,
  ``skip_seconds`` at each end). Dropping the ends removes the capture
  start/stop transients, and — for a looper source — keeps reverb tails
  overlapping naturally; capturing exactly one loop period truncates
  long-reverb leads and systematically under-counts them.

The USB channel map of a Stadium is ch1/2 = processed output, ch7 = DI tap,
ch3-6 silent; the default here is the proven mono capture, and an 8-channel
rig can pass ``remix="1,2"`` to fold the processed pair down.

Analysis needs the ``analyze`` extra (numpy). :func:`preflight` checks that
AND sox up front — a missing dependency must never be discovered *after* a
player has held a riff for a whole capture window.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from helixgen.audio_metrics import (AudioMetrics, AudioMetricsError, analyze,
                                    load_wav, require_numpy)

__all__ = [
    "DEFAULT_BITS",
    "DEFAULT_CHANNELS",
    "DEFAULT_RATE",
    "DEFAULT_SKIP_SECONDS",
    "analyze_capture",
    "capture_and_analyze",
    "capture_argv",
    "capture_wav",
    "default_input_type",
    "preflight",
]

DEFAULT_RATE = 48000        # the Stadium's native rate; no resampling
DEFAULT_BITS = 24
DEFAULT_CHANNELS = 1        # ch1 of the processed pair — the proven recipe
DEFAULT_SKIP_SECONDS = 2.5  # dropped at EACH end before analysis

_SOX_BITS = (8, 16, 24, 32)

#: sox input driver per platform (``-t``). Overridable — a Linux box on
#: PulseAudio wants ``pulseaudio``, not ``alsa``.
_INPUT_TYPES = {"Darwin": "coreaudio", "Linux": "alsa", "Windows": "waveaudio"}


def default_input_type() -> str:
    """The sox input driver for this platform (``-t``)."""
    return _INPUT_TYPES.get(platform.system(), "coreaudio")


def preflight(*, require_sox: bool = True) -> None:
    """Raise :class:`AudioMetricsError` unless a capture+analyze round trip
    can actually complete — numpy (the ``analyze`` extra) and sox.

    Call this BEFORE a capture: the analysis dependency is not installed by
    the ``device`` extra, and discovering that after a 60 s window wastes the
    window."""
    require_numpy()
    if require_sox and shutil.which("sox") is None:
        raise AudioMetricsError(
            "audio capture needs the `sox` binary on PATH; install with "
            "`brew install sox` (macOS) or `apt install sox` (Debian)")


def capture_argv(out: Path | str, seconds: float, *,
                 device: str | None = None,
                 input_type: str | None = None,
                 rate: int = DEFAULT_RATE,
                 channels: int = DEFAULT_CHANNELS,
                 bits: int = DEFAULT_BITS,
                 remix: str | None = None) -> list[str]:
    """The sox command line for one capture — format flags BEFORE the input
    device (see the module docstring; the other order silently resamples).

    ``device`` is the input device NAME (``'Helix Stadium XL'``); ``None``
    uses sox's default input (``-d``), which is the user's system default and
    is almost never what you want to measure. ``remix`` (e.g. ``"1,2"``)
    folds a multi-channel capture down to the processed pair.
    """
    if seconds <= 0:
        raise AudioMetricsError(f"capture needs a positive duration "
                                f"(got {seconds})")
    if rate <= 0:
        raise AudioMetricsError(f"capture needs a positive sample rate "
                                f"(got {rate})")
    if channels <= 0:
        raise AudioMetricsError(f"capture needs a positive channel count "
                                f"(got {channels})")
    if bits not in _SOX_BITS:
        raise AudioMetricsError(f"capture bit depth {bits} not one of "
                                f"{', '.join(map(str, _SOX_BITS))}")
    argv = ["sox",
            "-c", str(channels), "-b", str(bits), "-r", str(rate),
            "-t", input_type or default_input_type()]
    argv += [device] if device else ["-d"]
    argv += [str(out), "trim", "0", f"{seconds:g}"]
    if remix:
        argv += ["remix", remix]
    return argv


def capture_wav(out: Path | str, seconds: float, **kwargs) -> Path:
    """Record ``seconds`` from the input device to ``out`` via sox; blocks
    for the window. Keyword arguments are :func:`capture_argv`'s."""
    out = Path(out)
    argv = capture_argv(out, seconds, **kwargs)
    if shutil.which("sox") is None:
        raise AudioMetricsError(
            "audio capture needs the `sox` binary on PATH; install with "
            "`brew install sox` (macOS) or `apt install sox` (Debian)")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=seconds + 30.0)
    except subprocess.TimeoutExpired as exc:
        raise AudioMetricsError(
            f"sox capture did not finish within {seconds + 30:g}s "
            f"(device busy or unreadable?)") from exc
    except OSError as exc:
        raise AudioMetricsError(f"could not run sox: {exc}") from exc
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or "").split()) or "no stderr"
        raise AudioMetricsError(
            f"sox capture failed (exit {proc.returncode}): {detail}. Check "
            f"the capture device name against `sox -h` / your system's audio "
            f"settings.")
    if not out.exists() or out.stat().st_size == 0:
        raise AudioMetricsError(f"sox reported success but wrote no audio to "
                                f"{out}")
    return out


def analyze_capture(path: Path | str,
                    skip_seconds: float = DEFAULT_SKIP_SECONDS
                    ) -> AudioMetrics:
    """Analyze the MIDDLE of a capture: ``skip_seconds`` dropped at each end
    (start/stop transients out; for a looper source, reverb tails still
    overlap naturally). A capture too short to survive the trim is analyzed
    whole, with a note."""
    samples, rate = load_wav(path)
    n = samples.shape[0]
    skip = int(round(max(0.0, skip_seconds) * rate))
    if skip > 0 and 2 * skip < n:
        segment = samples[skip:n - skip]
        note = (f"analyzed the middle {(n - 2 * skip) / rate:.1f}s of a "
                f"{n / rate:.1f}s capture ({skip_seconds:g}s dropped at each "
                f"end)")
    else:
        segment = samples
        note = (f"{n / rate:.1f}s capture is too short to drop "
                f"{skip_seconds:g}s at each end; analyzed whole")
    m = analyze(segment, rate, file=str(path))
    m.notes.append(note)
    return m


def capture_and_analyze(out: Path | str, seconds: float, *,
                        skip_seconds: float = DEFAULT_SKIP_SECONDS,
                        **kwargs) -> AudioMetrics:
    """:func:`preflight` -> :func:`capture_wav` -> :func:`analyze_capture`.

    The preflight is FIRST on purpose: a missing numpy or sox must fail
    before the player is asked to hold a riff for the window."""
    preflight()
    path = capture_wav(out, seconds, **kwargs)
    return analyze_capture(path, skip_seconds)
