"""CLI tests for `helixgen analyze-audio` (backlog #62 phase 3).

The per-verb --help is the agent contract (the MCP server is gone), so the
help text's key phrases are pinned here the same way test_cli_parity.py pins
the older verbs. Record mode is exercised offline with a fake `sounddevice`
module injected into sys.modules — no hardware, no PortAudio.
"""
from __future__ import annotations

import json
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from helixgen.cli import cli


RATE = 48000


def _write_sine(path: Path, freq: float = 1000.0, seconds: float = 2.0,
                amp: float = 0.5) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    x = amp * np.sin(2 * np.pi * freq * t)
    ints = np.round(x * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(ints.tobytes())
    return path


def _full_help(cmd) -> str:
    opt_help = " ".join(getattr(p, "help", None) or "" for p in cmd.params)
    return " ".join(((cmd.help or "") + "\n" + opt_help).split())


class TestHelpContract:
    def test_verb_exists(self):
        assert "analyze-audio" in cli.commands

    @pytest.mark.parametrize("phrase", [
        "LUFS",
        "BS.1770",
        "crest factor",
        "band",
        "low_mid",
        "helixgen[analyze]",
        "helixgen[capture]",
        "EXPERIMENTAL",
        "read-only",
        "--json",
        "non-finite",
    ])
    def test_key_contract_phrases(self, phrase: str):
        assert phrase in _full_help(cli.commands["analyze-audio"]), (
            f"agent-contract phrase {phrase!r} missing from "
            "`analyze-audio --help`")


class TestAnalyzeFile:
    def test_human_output(self, tmp_path: Path):
        wav = _write_sine(tmp_path / "tone.wav")
        result = CliRunner().invoke(cli, ["analyze-audio", str(wav)])
        assert result.exit_code == 0, result.output
        assert "LUFS" in result.output
        assert "crest" in result.output
        assert "high_mid" in result.output

    def test_json_output_shape(self, tmp_path: Path):
        wav = _write_sine(tmp_path / "tone.wav")
        result = CliRunner().invoke(cli, ["analyze-audio", str(wav), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["lufs_integrated"] == pytest.approx(-9.03, abs=0.15)
        assert data["crest_db"] == pytest.approx(3.01, abs=0.1)
        assert data["rate"] == RATE
        assert data["channels"] == 1
        assert data["clipped"] is False
        assert {b["band"] for b in data["bands"]} == {
            "low", "low_mid", "mid", "high_mid", "high"}
        assert data["file"].endswith("tone.wav")

    def test_unreadable_file_is_clean_error(self, tmp_path: Path):
        bad = tmp_path / "bad.wav"
        bad.write_bytes(b"nope" * 100)
        result = CliRunner().invoke(cli, ["analyze-audio", str(bad)])
        assert result.exit_code != 0
        assert "Error" in result.output
        assert "Traceback" not in result.output

    def test_no_args_is_usage_error(self):
        result = CliRunner().invoke(cli, ["analyze-audio"])
        assert result.exit_code != 0
        assert "--record" in result.output

    @pytest.mark.parametrize("flags", [
        ["--input", "Helix"],
        ["--rate", "44100"],
        ["--channels", "1"],
        ["--input", "Helix", "--rate", "44100", "--channels", "1"],
    ])
    def test_capture_flags_without_record_are_usage_error(
            self, tmp_path: Path, flags: list):
        # backlog #84(c): --input/--rate/--channels used to be silently
        # ignored when analyzing an existing file — now a usage error.
        wav = _write_sine(tmp_path / "tone.wav")
        result = CliRunner().invoke(cli, ["analyze-audio", str(wav)] + flags)
        assert result.exit_code != 0
        assert "--record" in result.output
        assert flags[0] in result.output

    def test_capture_flag_defaults_do_not_trip_the_guard(
            self, tmp_path: Path):
        # --rate/--channels have defaults; only EXPLICIT flags error.
        wav = _write_sine(tmp_path / "tone.wav")
        result = CliRunner().invoke(cli, ["analyze-audio", str(wav)])
        assert result.exit_code == 0, result.output

    def test_json_is_strictly_valid_on_nonfinite_samples(
            self, tmp_path: Path):
        # A wedged capture can hand back NaN/Inf samples; --json must still
        # emit strictly valid JSON (the help pins "Undefined metrics are
        # null, never NaN/-inf") — bare `NaN`/`Infinity` tokens are not JSON.
        from helixgen import audio_metrics as am
        t = np.arange(2 * RATE) / RATE
        x = 0.5 * np.sin(2 * np.pi * 1000.0 * t)
        x[100] = np.nan
        x[200] = np.inf
        wav = am.write_wav_float32(tmp_path / "nf.wav", x[:, None], RATE)
        result = CliRunner().invoke(cli, ["analyze-audio", str(wav), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(
            result.output,
            parse_constant=lambda s: pytest.fail(
                f"non-JSON constant {s!r} in --json output"))
        assert any("non-finite" in n for n in data["notes"])
        # a couple of zeroed samples must not null the level metrics
        assert data["lufs_integrated"] == pytest.approx(-9.03, abs=0.2)
        assert data["peak_dbfs"] == pytest.approx(-6.02, abs=0.1)


class TestRecordMode:
    def _fake_sounddevice(self, frames_store: dict):
        mod = types.ModuleType("sounddevice")

        def rec(frames, samplerate, channels, dtype, device=None):
            frames_store["frames"] = frames
            frames_store["samplerate"] = samplerate
            frames_store["channels"] = channels
            frames_store["device"] = device
            t = np.arange(frames) / samplerate
            x = 0.5 * np.sin(2 * np.pi * 1000.0 * t)
            return np.tile(x[:, None], (1, channels)).astype(np.float32)

        mod.rec = rec
        mod.wait = lambda: None
        return mod

    def test_record_requires_output_path(self):
        result = CliRunner().invoke(cli, ["analyze-audio", "--record", "2"])
        assert result.exit_code != 0
        assert "-o" in result.output

    def test_record_and_file_are_mutually_exclusive(self, tmp_path: Path):
        wav = _write_sine(tmp_path / "tone.wav")
        result = CliRunner().invoke(
            cli, ["analyze-audio", str(wav), "--record", "2",
                  "-o", str(tmp_path / "cap.wav")])
        assert result.exit_code != 0

    def test_record_captures_writes_and_analyzes(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        store: dict = {}
        monkeypatch.setitem(sys.modules, "sounddevice",
                            self._fake_sounddevice(store))
        out = tmp_path / "cap.wav"
        result = CliRunner().invoke(
            cli, ["analyze-audio", "--record", "1.5", "-o", str(out),
                  "--json"])
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert store["frames"] == int(1.5 * 48000)
        assert store["samplerate"] == 48000
        assert store["channels"] == 2
        data = json.loads(result.output)
        # stereo 1 kHz sine at 0.5: mono value -9.03 + 3.01 channel sum
        assert data["lufs_integrated"] == pytest.approx(-6.02, abs=0.2)
        assert data["file"] == str(out)

    def test_record_device_name_passthrough(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        store: dict = {}
        monkeypatch.setitem(sys.modules, "sounddevice",
                            self._fake_sounddevice(store))
        result = CliRunner().invoke(
            cli, ["analyze-audio", "--record", "1", "-o",
                  str(tmp_path / "c.wav"), "--input", "Helix Stadium"])
        assert result.exit_code == 0, result.output
        assert store["device"] == "Helix Stadium"

    def test_missing_sounddevice_hint(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(sys.modules, "sounddevice", None)
        result = CliRunner().invoke(
            cli, ["analyze-audio", "--record", "1",
                  "-o", str(tmp_path / "c.wav")])
        assert result.exit_code != 0
        assert "helixgen[capture]" in result.output

    def test_portaudio_error_is_clean_error(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # A real PortAudio failure (bad device, unsupported rate) must be a
        # clean CLI error, not a traceback.
        mod = self._fake_sounddevice({})

        class PortAudioError(Exception):
            pass

        def rec(*a, **k):
            raise PortAudioError("Error opening InputStream: Invalid device")

        mod.PortAudioError = PortAudioError
        mod.rec = rec
        monkeypatch.setitem(sys.modules, "sounddevice", mod)
        result = CliRunner().invoke(
            cli, ["analyze-audio", "--record", "1",
                  "-o", str(tmp_path / "c.wav")])
        assert result.exit_code != 0
        assert "Invalid device" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("flag,value", [
        ("--rate", "0"),
        ("--rate", "-48000"),
        ("--channels", "0"),
    ])
    def test_record_rejects_nonpositive_rate_and_channels(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
            flag: str, value: str):
        store: dict = {}
        monkeypatch.setitem(sys.modules, "sounddevice",
                            self._fake_sounddevice(store))
        out = tmp_path / "c.wav"
        result = CliRunner().invoke(
            cli, ["analyze-audio", "--record", "1", "-o", str(out),
                  flag, value])
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "frames" not in store  # never reached the device
        assert not out.exists()
