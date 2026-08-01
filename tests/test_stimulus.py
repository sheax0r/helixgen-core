"""Tests for helixgen.device.stimulus: playback preflight, the looped player,
and the macOS volume knob calibration drives (he-xth / hc-ged).

Nothing here runs sox: the module is exercised against a fake argv so the
suite stays silent, offline and fast.
"""
from __future__ import annotations

import sys

import pytest

from helixgen.device import stimulus as ST


# --- command construction ---------------------------------------------------


def test_argv_formats_the_path_into_the_command(tmp_path):
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    assert ST.argv(wav, "play -q {path} repeat 9999") == [
        "play", "-q", str(wav), "repeat", "9999"]


def test_argv_quotes_survive_a_spaced_path(tmp_path):
    wav = tmp_path / "my loop.wav"
    wav.write_bytes(b"RIFF")
    # the path is substituted as ONE argv element, never re-split on its
    # spaces -- a stimulus under "~/My Loops/" must not become two arguments.
    assert ST.argv(wav, "play -q {path} repeat 9999")[2] == str(wav)


def test_argv_rejects_a_command_without_the_path_placeholder(tmp_path):
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    with pytest.raises(ST.StimulusError, match=r"\{path\}"):
        ST.argv(wav, "play -q repeat 9999")


# --- preflight --------------------------------------------------------------


def test_preflight_rejects_a_missing_file(tmp_path):
    with pytest.raises(ST.StimulusError, match="no stimulus file"):
        ST.preflight(tmp_path / "absent.wav", "play -q {path} repeat 9999")


def test_preflight_rejects_a_missing_binary(tmp_path, monkeypatch):
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(ST.shutil, "which", lambda name: None)
    with pytest.raises(ST.StimulusError, match="play"):
        ST.preflight(wav, "play -q {path} repeat 9999")


def test_preflight_passes_when_both_exist(tmp_path, monkeypatch):
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(ST.shutil, "which", lambda name: "/usr/bin/" + name)
    assert ST.preflight(wav, "play -q {path} repeat 9999") == [
        "play", "-q", str(wav), "repeat", "9999"]


def test_preflight_rejects_a_shell_looped_afplay(tmp_path, monkeypatch):
    # `afplay` in a shell loop costs ~0.8-0.9 s of process startup per
    # invocation, which turns a 5.00 s loop into a ~5.9 s jittering period
    # and destroys the whole point of an exact-length stimulus.
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(ST.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(ST.StimulusError, match="gapless"):
        ST.preflight(wav, "while :; do afplay {path}; done")


# --- the player -------------------------------------------------------------


class _FakeProc:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


def test_playing_starts_and_always_stops(tmp_path, monkeypatch):
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(ST.shutil, "which", lambda name: "/usr/bin/" + name)
    started = []
    monkeypatch.setattr(ST.subprocess, "Popen",
                        lambda argv, **kw: started.append(_FakeProc(argv))
                        or started[-1])

    with ST.playing(wav, "play -q {path} repeat 9999") as proc:
        assert proc.argv[0] == "play"
        assert proc.poll() is None
    assert started[0].terminated is True


def test_playing_stops_the_loop_even_when_the_body_raises(tmp_path, monkeypatch):
    # a measurement that blows up mid-run must never leave a stimulus
    # playing into the user's amp forever.
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(ST.shutil, "which", lambda name: "/usr/bin/" + name)
    procs = []
    monkeypatch.setattr(ST.subprocess, "Popen",
                        lambda argv, **kw: procs.append(_FakeProc(argv))
                        or procs[-1])

    with pytest.raises(ZeroDivisionError):
        with ST.playing(wav, "play -q {path} repeat 9999"):
            raise ZeroDivisionError
    assert procs[0].terminated is True


def test_playing_reports_a_loop_that_died_immediately(tmp_path, monkeypatch):
    wav = tmp_path / "loop.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(ST.shutil, "which", lambda name: "/usr/bin/" + name)

    class _DeadProc(_FakeProc):
        def poll(self):
            return 1

    monkeypatch.setattr(ST.subprocess, "Popen",
                        lambda argv, **kw: _DeadProc(argv))
    with pytest.raises(ST.StimulusError, match="exited immediately"):
        with ST.playing(wav, "play -q {path} repeat 9999"):
            pass


# --- the macOS volume knob --------------------------------------------------


def test_set_output_volume_is_a_no_op_off_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ST.set_output_volume(53) is False


def test_set_output_volume_runs_osascript_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(ST.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    assert ST.set_output_volume(53) is True
    assert calls[0][0] == "osascript"
    assert "set volume output volume 53" in " ".join(calls[0])


def test_set_output_volume_clamps_to_the_0_100_range(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(ST.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    ST.set_output_volume(140)
    ST.set_output_volume(-8)
    assert "set volume output volume 100" in " ".join(calls[0])
    assert "set volume output volume 0" in " ".join(calls[1])


def test_next_volume_steps_toward_the_reference():
    # macOS output volume is roughly proportional to amplitude, so a dB delta
    # maps onto a multiplicative step. Under-shooting is fine (the loop
    # re-measures); the requirement is that the step goes the RIGHT way.
    assert ST.next_volume(50, delta_db=+6.0) > 50   # too quiet -> louder
    assert ST.next_volume(50, delta_db=-6.0) < 50   # too loud  -> quieter
    assert ST.next_volume(50, delta_db=0.0) == 50


def test_next_volume_never_leaves_the_range():
    assert ST.next_volume(95, delta_db=+40.0) == 100
    assert ST.next_volume(3, delta_db=-40.0) == 0


def test_next_volume_moves_at_least_one_step():
    # a tiny delta that rounds back to the same integer would stall the loop
    # forever; the step is nudged to at least 1.
    assert ST.next_volume(50, delta_db=0.1) == 51
    assert ST.next_volume(50, delta_db=-0.1) == 49
