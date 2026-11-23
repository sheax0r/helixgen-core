"""The read-only Tuner pane (81v.10): LiveSession caches the latest pitch off
the always-on 2003 ``/dspEvent`` stream and projects it for the pane.

The pitch DECODER (``device/tuner.py``) is covered by tests/test_device_tuner.py
against a golden capture. What's NEW here is the thin live wiring: tapping 2003
events in ``handle_events``, caching the latest reading, and the staleness guard
so a stalled stream reads as no-signal rather than a frozen note.
"""
from __future__ import annotations

from helixgen.device.subscribe import Event
from helixgen.liveedit.session import TUNER_STALE, LiveSession


def _pitch_event(midi: float) -> Event:
    """A 2003 ``/dspEvent`` carrying the pitch scalar (string-key form; the
    decoder's ``_get`` accepts either key spelling)."""
    return Event(2003, "/dspEvent",
                 [{"id__": {"eid_": 10, "mid_": 796}, "vals": [float(midi)]}])


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _session():
    clk = _Clock()
    sess = LiveSession(clock=clk)
    return sess, clk


# --- no reading yet -----------------------------------------------------------

def test_tuner_no_reading_is_no_signal():
    sess, _ = _session()
    r = sess.tuner_reading()
    assert r["signal"] is False and r["name"] == "—"


# --- caching off the 2003 stream ----------------------------------------------

def test_pitch_event_is_cached_and_projected():
    sess, _ = _session()
    sess.handle_events([_pitch_event(45.0)])      # MIDI 45 = A2
    r = sess.tuner_reading()
    assert r["signal"] is True
    assert r["name"] == "A2" and r["note"] == "A" and r["octave"] == 2
    assert r["cents"] == 0 and round(r["hz"], 1) == 110.0


def test_slightly_flat_note_reports_negative_cents():
    sess, _ = _session()
    sess.handle_events([_pitch_event(45.0 - 0.1)])   # 10 cents flat of A2
    r = sess.tuner_reading()
    assert r["signal"] is True and r["name"] == "A2" and r["cents"] == -10


def test_silence_sentinel_reads_no_signal():
    sess, _ = _session()
    sess.handle_events([_pitch_event(45.0)])
    sess.handle_events([_pitch_event(-1.0)])         # detector reports silence
    assert sess.tuner_reading()["signal"] is False


def test_non_pitch_2003_events_are_ignored():
    sess, _ = _session()
    sess.handle_events([_pitch_event(45.0)])
    # a meter event (eid_ 1) and a bare heartbeat must not disturb the reading
    sess.handle_events([Event(2003, "/dspEvent",
                              [{"id__": {"eid_": 1, "mid_": 0}, "vals": [0.0]}])])
    sess.handle_events([Event(2003, "/heartbeat", [])])
    assert sess.tuner_reading()["name"] == "A2"


# --- staleness: a stalled stream must not freeze a note -----------------------

def test_stale_reading_decays_to_no_signal():
    sess, clk = _session()
    sess.handle_events([_pitch_event(45.0)])
    assert sess.tuner_reading()["signal"] is True
    clk.t += TUNER_STALE + 0.1                       # stream goes quiet
    assert sess.tuner_reading()["signal"] is False


def test_fresh_reading_within_window_still_shows():
    sess, clk = _session()
    sess.handle_events([_pitch_event(45.0)])
    clk.t += TUNER_STALE - 0.1                        # just inside the window
    assert sess.tuner_reading()["signal"] is True
