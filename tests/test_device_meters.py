"""Network level-meter decoder tests.

The meter events ride the same `/dspEvent` burst as the network tuner's pitch
scalar (see tests/test_device_tuner.py and its golden
tests/fixtures/tuner/pitch_dspevent.msgpack), but with `eid_:1` and
`mid_:796`/`800` — a 128-float grid-level array instead of a single pitch
float. No golden meter capture file exists yet, so these tests build synthetic
maps in the exact documented shape (see
docs/superpowers/specs/2026-07-14-parity-capture-findings.md §4).
"""
import pytest

from helixgen.device import meters

msgpack = pytest.importorskip("msgpack")


def _map(eid, mid, vals):
    return {meters._K_ID: {meters._K_EID: eid, meters._K_MID: mid},
            meters._K_VALS: vals}


def test_is_meter_map_true_for_both_mids():
    assert meters.is_meter_map(_map(1, 796, [0.0] * 128))
    assert meters.is_meter_map(_map(1, 800, [0.0] * 128))


def test_is_meter_map_false_for_pitch_event():
    # eid_10/mid_796 is the pitch event, not a meter event, even though 796
    # also appears as a meter mid under eid_1 — the pair must match together.
    assert not meters.is_meter_map(_map(10, 796, [45.0]))


def test_is_meter_map_false_for_unrelated_mid():
    assert not meters.is_meter_map(_map(1, 999, [0.0]))


def test_reading_from_map_decodes_values_and_peak():
    vals = [0.01] * 127 + [0.08]
    r = meters.reading_from_map(_map(1, 796, vals))
    assert r is not None
    assert r.mid == 796
    assert len(r.values) == 128
    assert r.peak == pytest.approx(0.08)


def test_reading_from_map_none_for_non_meter():
    assert meters.reading_from_map(_map(10, 796, [40.0])) is None
    assert meters.reading_from_map({"nope": 1}) is None


def test_reading_from_map_empty_values_peak_zero():
    r = meters.reading_from_map(_map(1, 800, []))
    assert r is not None and r.values == [] and r.peak == 0.0


def test_readings_from_event_args_finds_both_mids_and_skips_pitch():
    args = [
        _map(1, 796, [0.02] * 128),
        _map(1, 800, [0.03] * 128),
        _map(10, 796, [45.0]),  # pitch event — must be skipped
    ]
    out = meters.readings_from_event_args(args)
    mids = sorted(r.mid for r in out)
    assert mids == [796, 800]


def test_readings_from_event_args_empty_or_bad_input():
    assert meters.readings_from_event_args([]) == []
    assert meters.readings_from_event_args("nope") == []
    assert meters.readings_from_event_args(None) == []


def test_string_key_form_also_decodes():
    # some msgpack decoders surface 4-char names as strings, not uint32 ints
    m = {"id__": {"eid_": 1, "mid_": 800}, "vals": [0.05] * 4}
    r = meters.reading_from_map(m)
    assert r is not None and r.mid == 800 and r.peak == pytest.approx(0.05)
