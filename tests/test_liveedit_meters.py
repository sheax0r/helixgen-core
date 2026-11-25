"""I/O metering (Focus-View parity): LiveSession caches the latest input/output
levels off the always-on 2003 meter /dspEvent stream and projects them as 0-1
bar fractions (dB-mapped — the raw envelope is a small linear amplitude) for the
editor's I/O meters. The meter DECODER (device/meters.py) is covered elsewhere;
this is the thin live wiring + the bar mapping + the staleness guard."""
from __future__ import annotations

from helixgen.device.subscribe import Event
from helixgen.device import meters
from helixgen.liveedit.session import METER_STALE, LiveSession


def _meter_event(mid: int, vals) -> Event:
    return Event(2003, "/dspEvent",
                 [{"id__": {"eid_": meters.METER_EID, "mid_": mid},
                   "vals": [float(v) for v in vals]}])


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def _session():
    clk = _Clock()
    return LiveSession(clock=clk), clk


def test_meters_no_reading_is_stale():
    sess, _ = _session()
    assert sess.meters_reading().get("stale") is True


def test_levels_are_cached_and_dB_mapped_to_a_bar():
    sess, _ = _session()
    # a LOUD guitar envelope is ~0.08 linear (nowhere near 1.0); a raw 0-1 bar
    # would read ~8% — the dB mapping must put a loud signal high on the bar.
    sess.handle_events([_meter_event(meters.INPUT_MID, [0.08, 0.06]),
                        _meter_event(meters.OUTPUT_MID, [0.0, 0.04, 0.04, 0.0])])
    r = sess.meters_reading()
    assert not r.get("stale")
    assert 0.80 <= r["input"] <= 0.95           # ~0.08 linear -> ~0.89 bar
    assert 0.0 < r["output"] < r["input"]       # quieter output sits lower
    assert r["clip_in"] is False and r["clip_out"] is False


def test_clip_flags_at_the_dB_ceiling():
    sess, _ = _session()
    sess.handle_events([_meter_event(meters.INPUT_MID, [0.13, 0.13])])   # >= -18 dB
    r = sess.meters_reading()
    assert r["clip_in"] is True and r["input"] == 1.0


def test_silence_reads_a_zero_bar():
    sess, _ = _session()
    sess.handle_events([_meter_event(meters.INPUT_MID, [0.0, 0.0])])
    assert sess.meters_reading()["input"] == 0.0


def test_stale_after_the_stream_stalls():
    sess, clk = _session()
    sess.handle_events([_meter_event(meters.INPUT_MID, [0.05, 0.05])])
    clk.t += METER_STALE + 0.1
    assert sess.meters_reading().get("stale") is True


def test_a_non_meter_2003_event_is_ignored():
    sess, _ = _session()
    # a pitch event on the same 2003 stream must not register as a meter reading
    sess.handle_events([Event(2003, "/dspEvent",
                              [{"id__": {"eid_": 10, "mid_": 796}, "vals": [45.0]}])])
    assert sess.meters_reading().get("stale") is True
