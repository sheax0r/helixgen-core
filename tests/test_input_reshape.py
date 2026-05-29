"""Unit tests for _reshape_input_params (mono <-> stereo)."""
from helixgen.generate import _reshape_input_params


def test_mono_to_stereo_wraps_each_value_and_adds_stereolink():
    mono = {
        "Pad":       {"value": 1},
        "Trim":      {"value": 0.5},
        "threshold": {"value": -48.0},
    }
    out = _reshape_input_params(mono, to_stereo=True)
    assert out == {
        "Pad":         {"1": {"value": 1},    "2": {"value": 1}},
        "Trim":        {"1": {"value": 0.5},  "2": {"value": 0.5}},
        "threshold":   {"1": {"value": -48.0},"2": {"value": -48.0}},
        "StereoLink":  {"value": False},
    }


def test_stereo_to_mono_takes_channel_one_and_drops_stereolink():
    stereo = {
        "Pad":        {"1": {"value": 1},   "2": {"value": 0}},
        "Trim":       {"1": {"value": 0.3}, "2": {"value": 0.7}},  # distinct values
        "StereoLink": {"value": False},
    }
    out = _reshape_input_params(stereo, to_stereo=False)
    assert out == {
        "Pad":  {"value": 1},
        "Trim": {"value": 0.3},  # channel 1, NOT channel 2
    }


def test_mono_to_mono_identity():
    mono = {"Pad": {"value": 1}}
    out = _reshape_input_params(mono, to_stereo=False)
    assert out == mono


def test_stereo_to_stereo_identity():
    stereo = {
        "Pad":        {"1": {"value": 1}, "2": {"value": 1}},
        "StereoLink": {"value": False},
    }
    out = _reshape_input_params(stereo, to_stereo=True)
    assert out == stereo
