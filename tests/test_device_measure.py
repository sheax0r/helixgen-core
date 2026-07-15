"""Playing-gated loudness measurement (loudness spec phase 1).

Synthetic `/dspEvent` payloads in the same documented shape the tuner/meters
tests use: pitch = {eid_:10, mid_:796, vals:[midi_float]}, meters =
{eid_:1, mid_:796/800, vals:[128 floats]}.
"""
import pytest

from helixgen.device import measure, meters
from helixgen.device.subscribe import Event

msgpack = pytest.importorskip("msgpack")


def _map(eid, mid, vals):
    return {meters._K_ID: {meters._K_EID: eid, meters._K_MID: mid},
            meters._K_VALS: vals}


def _grid(mid, cells):
    vals = [0.0] * 128
    for i, v in cells.items():
        vals[i] = v
    return vals


def _ev(*payloads):
    return Event(port=2003, addr="/dspEvent", args=list(payloads))


def _burst(pitch, inp, out):
    """One full telemetry burst: pitch + input grid + output grid events."""
    return [
        _ev(_map(10, 796, [pitch])),
        _ev(_map(1, 796, _grid(796, {0: inp, 1: inp, 8: out, 9: out}))),
        _ev(_map(1, 800, _grid(800, {108: out, 109: out, 110: out, 111: out}))),
    ]


def test_samples_pair_pitch_input_output():
    events = _burst(40.05, 0.02, 0.5) + _burst(40.10, 0.03, 0.6)
    samples = list(measure.samples_from_events(events))
    assert len(samples) == 2   # one per mid-800 reading
    assert samples[0].input_level == pytest.approx(0.02)
    assert samples[0].output_level == pytest.approx(0.5)
    assert samples[0].pitch == pytest.approx(40.05)
    assert samples[1].pitch == pytest.approx(40.10)


def test_samples_before_any_pitch_have_none():
    events = [
        _ev(_map(1, 796, _grid(796, {0: 0.02, 1: 0.02}))),
        _ev(_map(1, 800, _grid(800, {108: 0.5, 109: 0.5}))),
    ]
    (s,) = list(measure.samples_from_events(events))
    assert s.pitch is None


def test_is_playing_gates_on_pitch_and_input():
    playing = measure.MeasureSample(0.02, 0.5, 40.0)
    hum = measure.MeasureSample(0.03, 0.5, -1.0)          # no pitch = hum
    silent = measure.MeasureSample(0.0, 0.0, -1.0)
    unknown = measure.MeasureSample(0.02, 0.5, None)
    assert measure.is_playing(playing)
    assert not measure.is_playing(hum)
    assert not measure.is_playing(silent)
    assert not measure.is_playing(unknown)


def test_summarize_happy_path():
    samples = [measure.MeasureSample(0.02, 0.5, 40.0)] * 60
    r = measure.summarize(samples, seconds=10.0)
    assert r.ok
    assert r.n_samples == 60 and r.n_playing == 60
    assert r.playing_seconds == pytest.approx(6.0)
    assert r.input_db == pytest.approx(meters.to_db(0.02), abs=1e-6)
    assert r.output_db == pytest.approx(meters.to_db(0.5), abs=1e-6)
    assert r.output_db_p75 == pytest.approx(meters.to_db(0.5), abs=1e-6)
    # chain gain: 0.5 / 0.02 = 25x = +27.96 dB
    assert r.gain_db == pytest.approx(27.96, abs=0.01)


def test_summarize_rejects_too_little_playing():
    samples = ([measure.MeasureSample(0.02, 0.5, 40.0)] * 5
               + [measure.MeasureSample(0.03, 0.5, -1.0)] * 100)
    r = measure.summarize(samples, seconds=10.0)
    assert not r.ok
    assert "playing" in r.reason
    assert r.n_playing == 5


def test_summarize_no_data():
    r = measure.summarize([], seconds=10.0)
    assert not r.ok and r.reason == "no meter data"


def test_summarize_p75_orders_outputs():
    quiet = [measure.MeasureSample(0.02, 0.25, 40.0)] * 30
    loud = [measure.MeasureSample(0.02, 0.5, 40.0)] * 30
    r = measure.summarize(quiet + loud, seconds=10.0, min_playing=10)
    assert meters.to_db(0.25) <= r.output_db <= meters.to_db(0.5)
    assert r.output_db_p75 >= r.output_db
