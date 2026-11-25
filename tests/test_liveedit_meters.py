"""I/O metering (Focus-View parity): LiveSession caches the latest input/output
levels off the always-on 2003 meter /dspEvent stream and projects them for the
editor's I/O meters. The meter DECODER (device/meters.py) is covered elsewhere;
this is the thin live wiring + staleness guard, mirroring the Tuner pane."""
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


def test_input_output_levels_cached_and_projected():
    sess, _ = _session()
    sess.handle_events([_meter_event(meters.INPUT_MID, [0.5, 0.4]),
                        _meter_event(meters.OUTPUT_MID, [0.0, 0.7, 0.7, 0.0])])
    r = sess.meters_reading()
    assert not r.get("stale")
    assert abs(r["input"] - 0.5) < 1e-6
    assert r["output"] > 0.0
    assert r["clip_in"] is False and r["clip_out"] is False


def test_clip_flag_near_full_scale():
    sess, _ = _session()
    sess.handle_events([_meter_event(meters.INPUT_MID, [1.0, 1.0])])
    assert sess.meters_reading()["clip_in"] is True


def test_stale_after_the_stream_stalls():
    sess, clk = _session()
    sess.handle_events([_meter_event(meters.INPUT_MID, [0.5, 0.5])])
    clk.t += METER_STALE + 0.1
    assert sess.meters_reading().get("stale") is True


def test_a_non_meter_2003_event_is_ignored():
    sess, _ = _session()
    # a pitch event on the same 2003 stream must not register as a meter reading
    sess.handle_events([Event(2003, "/dspEvent",
                              [{"id__": {"eid_": 10, "mid_": 796}, "vals": [45.0]}])])
    assert sess.meters_reading().get("stale") is True
