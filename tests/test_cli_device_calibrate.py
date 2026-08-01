"""CLI tests for `device calibrate` (he-xth / hc-5dx).

The two-step source-level calibration: read the jack level while the user
plays BY HAND, then null a replayed stimulus against that reading and
persist both. Fully offline -- the telemetry window and the stimulus player
are both faked.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device.measure import MeasureResult
from helixgen.preferences import default_prefs_path, load_preferences


@pytest.fixture(autouse=True)
def _configured_device_ip(monkeypatch):
    monkeypatch.setenv("HELIXGEN_HELIX_IP", "10.0.0.99")


def _result(input_db, *, ok=True, reason=""):
    return MeasureResult(seconds=10.0, n_samples=100, n_playing=100,
                         playing_seconds=10.0, input_db=input_db,
                         output_db=input_db + 20, output_db_p75=input_db + 21,
                         gain_db=20.0, ok=ok, reason=reason)


@pytest.fixture
def stimulus(tmp_path):
    wav = tmp_path / "helix-cal-loop.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 64)
    return wav


@pytest.fixture
def rig(monkeypatch):
    """Fake the telemetry windows and the stimulus player.

    ``windows`` is the scripted list of MeasureResults handed out in order;
    ``state`` records the volumes actually set and whether the loop was
    playing during each measured window."""
    from helixgen import cli_device
    from helixgen.device import stimulus as ST

    state = {"windows": [], "volumes": [], "playing": [], "loop_running": False}

    def _fake_measure(ip, seconds, min_playing, source="input"):
        state["playing"].append(state["loop_running"])
        return state["windows"].pop(0)

    class _FakePlayer:
        def poll(self):
            return None

    import contextlib

    @contextlib.contextmanager
    def _fake_playing(path, cmd):
        state["loop_running"] = True
        try:
            yield _FakePlayer()
        finally:
            state["loop_running"] = False

    monkeypatch.setattr(cli_device, "_measure_window", _fake_measure)
    monkeypatch.setattr(ST, "playing", _fake_playing)
    monkeypatch.setattr(ST, "preflight", lambda path, cmd: ["play", str(path)])
    monkeypatch.setattr(ST, "set_output_volume",
                        lambda v: state["volumes"].append(v) or True)
    return state


def _prefs_block():
    return json.loads(default_prefs_path().read_text())["normalization"]


# --- the happy path ---------------------------------------------------------


def test_calibrate_nulls_the_stimulus_against_the_hand_played_reference(
        rig, stimulus):
    # by hand: -31.0 dB. First stimulus window is 6 dB quiet, so the volume
    # steps up and the second window lands in band.
    rig["windows"] = [_result(-31.0), _result(-37.0), _result(-30.7)]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6", "--volume", "50", "--guitar", "ec-1000"],
        input="\n\n")
    assert result.exit_code == 0, result.output
    # the hand-played window ran WITHOUT the loop; the stimulus ones with it
    assert rig["playing"] == [False, True, True]
    assert rig["volumes"] == [50, 100]   # start, then stepped up by +6 dB
    block = _prefs_block()
    assert block["calibration"]["reference_input_db"] == -31.0
    assert block["calibration"]["achieved_input_db"] == -30.7
    assert block["calibration"]["reference_guitar"] == "ec-1000"
    assert block["calibration"]["calibrated_on"]
    assert block["sample"]["path"] == str(stimulus)
    assert block["sample"]["volume"] == 100
    assert block["mode"] == "sample"


def test_calibrate_stops_immediately_when_already_in_band(rig, stimulus):
    rig["windows"] = [_result(-31.0), _result(-31.4)]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6", "--volume", "53"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert rig["volumes"] == [53]        # nothing to adjust
    assert _prefs_block()["sample"]["volume"] == 53


def test_calibrate_json_reports_the_steps(rig, stimulus):
    rig["windows"] = [_result(-31.0), _result(-37.0), _result(-30.7)]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6", "--volume", "50", "--json"], input="\n\n")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["reference_input_db"] == -31.0
    assert payload["achieved_input_db"] == -30.7
    assert payload["converged"] is True
    assert [s["input_db"] for s in payload["steps"]] == [-37.0, -30.7]
    assert payload["steps"][0]["volume"] == 50


def test_calibrate_preserves_other_preferences(rig, stimulus):
    path = default_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "author": "mike",
                                "favor_irs": True,
                                "normalization": {"target_db": 17.5}}))
    rig["windows"] = [_result(-31.0), _result(-31.0)]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6", "--volume", "53"], input="\n\n")
    assert result.exit_code == 0, result.output
    data = json.loads(path.read_text())
    assert data["author"] == "mike" and data["favor_irs"] is True
    # the untouched normalization keys survive the merge
    assert data["normalization"]["target_db"] == 17.5
    assert data["normalization"]["calibration"]["reference_input_db"] == -31.0
    assert load_preferences(path).normalization.is_calibrated is True


# --- refusals and failures --------------------------------------------------


def test_calibrate_fails_when_the_hand_played_window_has_no_playing(
        rig, stimulus):
    rig["windows"] = [_result(-31.0, ok=False, reason="too little playing")]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6"], input="\n")
    assert result.exit_code != 0
    assert "too little playing" in result.output
    assert not default_prefs_path().exists()   # nothing half-written


def test_calibrate_gives_up_after_max_steps_and_writes_nothing(rig, stimulus):
    # a stimulus that never moves (e.g. the audio is leaving over USB) must
    # not be recorded as a calibration -- that would make every later run
    # confidently wrong.
    rig["windows"] = [_result(-31.0)] + [_result(-60.0)] * 4
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6", "--volume", "50", "--max-steps", "4"],
        input="\n\n")
    assert result.exit_code != 0
    assert "did not converge" in result.output.lower()
    assert "output device" in result.output.lower()   # the usual culprit
    assert "normalization" not in (
        json.loads(default_prefs_path().read_text())
        if default_prefs_path().exists() else {})


def test_calibrate_needs_a_stimulus(rig):
    result = CliRunner().invoke(cli, ["device", "calibrate", "--seconds", "6"])
    assert result.exit_code != 0
    assert "--stimulus" in result.output


def test_calibrate_takes_the_stimulus_from_preferences(rig, stimulus):
    path = default_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "normalization": {"sample": {"path": str(stimulus), "volume": 53}}}))
    rig["windows"] = [_result(-31.0), _result(-31.2)]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--seconds", "6"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert rig["volumes"] == [53]       # the recorded volume is the start point


def test_calibrate_off_darwin_reports_the_volume_to_set_by_hand(
        rig, stimulus, monkeypatch):
    from helixgen.device import stimulus as ST

    monkeypatch.setattr(ST, "set_output_volume", lambda v: False)
    rig["windows"] = [_result(-31.0), _result(-37.0)]
    result = CliRunner().invoke(
        cli, ["device", "calibrate", "--stimulus", str(stimulus),
              "--seconds", "6", "--volume", "50", "--max-steps", "1"],
        input="\n\n")
    assert result.exit_code != 0
    assert "by hand" in result.output.lower()
