"""Offline tests for the sox-backed capture pipeline (hc-57h).

No sox, no audio hardware: the argv builder is pure, the subprocess call is
monkeypatched, and the middle-segment analysis runs over a synthesized WAV.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from helixgen import audio_capture as AC
from helixgen.audio_metrics import AudioMetricsError, write_wav_float32

np = pytest.importorskip("numpy")


# --- argv builder ------------------------------------------------------------

def test_capture_argv_puts_format_flags_before_the_device_name():
    # sox applies format flags to whatever file/device FOLLOWS them; putting
    # them after the input device silently resamples instead of pinning the
    # capture. Order is the whole point of this builder.
    argv = AC.capture_argv(Path("/tmp/cap.wav"), 30.0,
                           device="Helix Stadium XL",
                           input_type="coreaudio", channels=1)
    assert argv[0] == "sox"
    dev = argv.index("Helix Stadium XL")
    for flag in ("-c", "-b", "-r", "-t"):
        assert argv.index(flag) < dev, f"{flag} must precede the input device"
    assert argv[dev + 1] == "/tmp/cap.wav"
    assert argv[-3:] == ["trim", "0", "30"]


def test_capture_argv_defaults_are_the_proven_recipe():
    argv = AC.capture_argv(Path("cap.wav"), 30.0, device="X",
                           input_type="coreaudio")
    assert argv[:9] == ["sox", "-c", "1", "-b", "24", "-r", "48000",
                        "-t", "coreaudio"]


def test_capture_argv_without_a_device_uses_soxs_default_input():
    argv = AC.capture_argv(Path("cap.wav"), 5.0, input_type="alsa")
    assert "-d" in argv
    assert argv[argv.index("-d") + 1] == "cap.wav"


def test_capture_argv_appends_remix():
    argv = AC.capture_argv(Path("cap.wav"), 30.0, device="X",
                           input_type="coreaudio", channels=8, remix="1,2")
    assert argv[-2:] == ["remix", "1,2"]
    assert argv[-5:-2] == ["trim", "0", "30"]


@pytest.mark.parametrize("kwargs", [
    {"seconds": 0.0}, {"seconds": -1.0}, {"rate": 0}, {"channels": 0},
    {"bits": 20},
])
def test_capture_argv_rejects_bad_parameters(kwargs):
    args = {"seconds": 10.0, "rate": 48000, "channels": 1, "bits": 24}
    args.update(kwargs)
    with pytest.raises(AudioMetricsError):
        AC.capture_argv(Path("cap.wav"), device="X", input_type="coreaudio",
                        **args)


# --- preflight ---------------------------------------------------------------

def test_preflight_fails_when_sox_is_missing(monkeypatch):
    monkeypatch.setattr(AC.shutil, "which", lambda _: None)
    with pytest.raises(AudioMetricsError, match="sox"):
        AC.preflight()


def test_capture_and_analyze_preflights_before_capturing(monkeypatch, tmp_path):
    """The [analyze] extra check must run BEFORE a 60 s capture, not after —
    otherwise the player plays a whole window for nothing."""
    captured: list = []
    monkeypatch.setattr(AC, "capture_wav",
                        lambda *a, **k: captured.append(a) or a[0])
    monkeypatch.setattr(AC, "preflight",
                        lambda: (_ for _ in ()).throw(
                            AudioMetricsError("audio analysis needs numpy")))
    with pytest.raises(AudioMetricsError, match="numpy"):
        AC.capture_and_analyze(tmp_path / "cap.wav", 30.0, device="X")
    assert captured == [], "capture ran despite a failed preflight"


def test_capture_wav_reports_a_missing_sox(monkeypatch, tmp_path):
    monkeypatch.setattr(AC.shutil, "which", lambda _: None)
    with pytest.raises(AudioMetricsError, match="sox"):
        AC.capture_wav(tmp_path / "cap.wav", 1.0, device="X")


def test_capture_wav_reports_a_sox_failure(monkeypatch, tmp_path):
    class _Proc:
        returncode = 2
        stderr = "sox FAIL formats: can't open input `Nope'\n"

    monkeypatch.setattr(AC.shutil, "which", lambda _: "/usr/bin/sox")
    monkeypatch.setattr(AC.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(AudioMetricsError, match="can't open input"):
        AC.capture_wav(tmp_path / "cap.wav", 1.0, device="Nope")


# --- middle-segment analysis -------------------------------------------------

def _wav(tmp_path: Path, pad_s: float, tone_s: float, rate: int = 48000):
    """A capture-shaped WAV: `pad_s` of silence, `tone_s` of sine, `pad_s`
    of silence."""
    t = np.arange(int(tone_s * rate)) / rate
    tone = 0.5 * np.sin(2 * np.pi * 220.0 * t)
    pad = np.zeros(int(pad_s * rate))
    return write_wav_float32(tmp_path / "cap.wav",
                             np.concatenate([pad, tone, pad]), rate)


def test_analyze_capture_analyzes_the_middle(tmp_path):
    path = _wav(tmp_path, pad_s=2.5, tone_s=5.0)
    m = AC.analyze_capture(path, skip_seconds=2.5)
    assert m.seconds == pytest.approx(5.0, abs=0.01)
    assert any("middle" in n for n in m.notes)
    assert m.lufs_integrated is not None


def test_analyze_capture_keeps_a_short_capture_whole(tmp_path):
    path = _wav(tmp_path, pad_s=0.0, tone_s=2.0)
    m = AC.analyze_capture(path, skip_seconds=2.5)
    assert m.seconds == pytest.approx(2.0, abs=0.01)
    assert any("too short" in n for n in m.notes)
