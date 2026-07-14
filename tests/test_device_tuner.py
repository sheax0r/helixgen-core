"""Network-tuner decoder tests.

The golden fixture ``tests/fixtures/tuner/pitch_dspevent.msgpack`` is the exact
msgpack payload of a ``/dspEvent`` pitch frame captured 2026-07-14 while the
owner plucked a string.
"""
import os

import pytest

from helixgen.device import tuner

msgpack = pytest.importorskip("msgpack")

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "tuner")


def _golden_map():
    with open(os.path.join(FIX, "pitch_dspevent.msgpack"), "rb") as f:
        return msgpack.unpackb(f.read(), raw=False, strict_map_key=False)


def test_golden_is_pitch_event():
    m = _golden_map()
    assert tuner.is_pitch_map(m)
    midi = tuner.pitch_from_map(m)
    assert midi is not None and 20 < midi < 100


def test_golden_decodes_to_reading():
    m = _golden_map()
    r = tuner.reading_from_event_args([m])
    assert r is not None and r.signal
    # golden pitch ≈ 34.93 → B1, ~-7 cents, ~61.5 Hz
    assert r.note == "B" and r.octave == 1
    assert -12 <= r.cents <= 0
    assert 60.0 < r.hz < 63.0
    assert r.name == "B1"


def test_reading_from_midi_low_e_flat():
    # 39.90 → E2 (MIDI 40) at -10 cents ≈ 81.93 Hz (the documented low-E example)
    r = tuner.reading_from_midi(39.90)
    assert r.signal and r.note == "E" and r.octave == 2
    assert r.cents == -10
    assert 81.0 < r.hz < 83.0


def test_reading_from_midi_a440():
    r = tuner.reading_from_midi(69.0)
    assert r.note == "A" and r.octave == 4 and r.cents == 0
    assert abs(r.hz - 440.0) < 0.01


def test_no_signal_sentinel_is_not_a_pitch():
    m = _golden_map()
    # overwrite vals with the -1 sentinel (using whatever key form the map has)
    valkey = tuner._K_VALS if tuner._K_VALS in m else "vals"
    m2 = dict(m)
    m2[valkey] = [-1.0]
    r = tuner.reading_from_event_args([m2])
    assert r is not None and not r.signal and r.name == "—"


def test_non_pitch_event_returns_none():
    # a meter event (eid_1) is not the pitch event
    assert tuner.reading_from_event_args([{tuner._K_ID: {tuner._K_EID: 1,
                                                         tuner._K_MID: 796},
                                           tuner._K_VALS: [0.1] * 128}]) is None
    assert tuner.reading_from_event_args([]) is None
    assert tuner.reading_from_event_args("nope") is None


def test_string_key_form_also_decodes():
    # some msgpack decoders surface 4-char names as strings, not uint32 ints
    m = {"id__": {"eid_": 10, "mid_": 796}, "vals": [45.0]}
    r = tuner.reading_from_event_args([m])
    assert r is not None and r.signal and r.note == "A" and r.octave == 2 and r.cents == 0
