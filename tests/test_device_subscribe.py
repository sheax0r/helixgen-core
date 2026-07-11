"""Unit tests for HelixSubscriber PUB-stream parsing with FAKE SUB sockets.

No real ZeroMQ socket or device is used: we inject fake SUB sockets (one per
port) and a fake Poller that reports a socket ready while it still holds queued
frames.  Test frames are built with ``osc_encode`` — including a 2001-style
frame with a 12-byte binary prefix before the OSC packet (to prove the header
is skipped) and a 2003 ``/dspEvent`` frame carrying a msgpack blob.
"""
from __future__ import annotations

import itertools
import struct

import pytest

msgpack = pytest.importorskip("msgpack")

from helixgen.device.subscribe import HelixSubscriber, Event  # noqa: E402
from helixgen.device.client import HelixError  # noqa: E402
from helixgen.device.osc import osc_encode  # noqa: E402
from helixgen.device import content as _content  # noqa: E402


# --------------------------------------------------------------------------
# fakes (mirror tests/test_device_client.py's FakeSock/FakePoller)
# --------------------------------------------------------------------------
class FakeSock:
    """Mimic a zmq SUB socket: recv() pops the next queued frame."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def has_frames(self):
        return bool(self._frames)

    def recv(self):
        return self._frames.pop(0)

    def close(self):
        self.closed = True


class FakePoller:
    """Mimic zmq.Poller: report each socket ready while it holds frames."""

    def __init__(self, socks):
        self._socks = list(socks)

    def register(self, *_a, **_k):
        pass

    def poll(self, _timeout_ms):
        return [(s, 1) for s in self._socks if s.has_frames()]


def _wire(sub, frames_by_port):
    """Inject a FakeSock per port + a FakePoller over them."""
    socks = {}
    for port, frames in frames_by_port.items():
        socks[FakeSock(frames)] = port
    sub._socks = socks
    sub.poller = FakePoller(list(socks))
    sub._zmq = None  # -> zmq_error is (), no real ZMQError to catch
    return socks


# --------------------------------------------------------------------------
# frame builders
# --------------------------------------------------------------------------
def _frame_2001(addr, args, *, version=1, seq=7):
    """A 2001 device->editor frame: 12-byte BE header + OSC packet."""
    osc = osc_encode(addr, args)
    header = struct.pack(">III", version, seq, len(osc))
    return header + osc


def _frame_2003(addr, args):
    """A 2003 frame: the frame *is* the OSC packet (no prefix)."""
    return osc_encode(addr, args)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_poll_returns_decoded_events_with_correct_port_and_addr():
    sub = HelixSubscriber()
    prop = _frame_2001("/setPropertyValue", [("i", 42), ("f", 0.5)])
    dsp = _frame_2003("/dspEvent", [("b", msgpack.packb({"blk": 3, "v": 0.25}))])
    _wire(sub, {2001: [prop], 2003: [dsp]})

    events = sub.poll(timeout=0.1)
    by_addr = {e.addr: e for e in events}

    assert set(by_addr) == {"/setPropertyValue", "/dspEvent"}
    assert all(isinstance(e, Event) for e in events)

    prop_ev = by_addr["/setPropertyValue"]
    assert prop_ev.port == 2001
    assert prop_ev.args[0] == 42
    assert prop_ev.args[1] == pytest.approx(0.5)

    dsp_ev = by_addr["/dspEvent"]
    assert dsp_ev.port == 2003
    assert dsp_ev.args == [{"blk": 3, "v": 0.25}]  # blob msgpack-decoded


def test_12byte_prefixed_2001_frame_parses_after_skipping_header():
    sub = HelixSubscriber()
    frame = _frame_2001("/setEditBuffer", [("i", 1)], version=2, seq=99)
    # the header contains no '/', so find(b"/") lands on the OSC address
    assert frame.find(b"/") == 12
    _wire(sub, {2001: [frame]})

    events = sub.poll(timeout=0.1)
    assert len(events) == 1
    assert events[0].addr == "/setEditBuffer"
    assert events[0].port == 2001
    assert events[0].args == [1]


def test_sbepgsm_content_blob_stays_raw_bytes():
    sub = HelixSubscriber()
    blob = _content.MAGIC + msgpack.packb({"foo": "bar"}, use_bin_type=True)
    frame = _frame_2001("/setEditBuffer", [("b", blob)])
    _wire(sub, {2001: [frame]})

    (ev,) = sub.poll(timeout=0.1)
    assert isinstance(ev.args[0], bytes)
    assert ev.args[0][:8] == _content.MAGIC
    assert ev.args[0] == blob  # verbatim, NOT decoded to a dict


def test_stream_drops_trigger_noise_by_default():
    sub = HelixSubscriber()
    dsp = _frame_2003("/dspEvent", [("b", msgpack.packb({"v": 1}))])
    trig = _frame_2003("/trigger", [("i", 0)])
    prop = _frame_2001("/setPropertyValue", [("i", 1)])
    _wire(sub, {2001: [prop], 2003: [dsp, trig]})

    # exactly two non-noise frames present -> islice(2) pulls them, then stops
    events = list(itertools.islice(sub.stream(include_noise=False), 2))
    addrs = {e.addr for e in events}
    assert "/trigger" not in addrs
    assert addrs == {"/dspEvent", "/setPropertyValue"}


def test_stream_include_noise_keeps_trigger():
    sub = HelixSubscriber()
    dsp = _frame_2003("/dspEvent", [("b", msgpack.packb({"v": 1}))])
    trig = _frame_2003("/trigger", [("i", 0)])
    _wire(sub, {2003: [dsp, trig]})

    events = list(itertools.islice(sub.stream(include_noise=True), 2))
    assert {e.addr for e in events} == {"/dspEvent", "/trigger"}


def test_stream_filter_addrs_restricts_to_set():
    sub = HelixSubscriber()
    dsp = _frame_2003("/dspEvent", [("i", 1)])
    meter = _frame_2003("/meter", [("f", 0.9)])
    _wire(sub, {2003: [dsp, meter]})

    events = list(itertools.islice(
        sub.stream(filter_addrs={"/meter"}, include_noise=True), 1))
    assert len(events) == 1
    assert events[0].addr == "/meter"


def test_stream_duration_zero_terminates_immediately():
    sub = HelixSubscriber()
    _wire(sub, {2003: [_frame_2003("/dspEvent", [("i", 1)])]})
    # duration already elapsed -> generator yields nothing and returns
    assert list(sub.stream(duration=0.0)) == []


def test_poll_drains_multiple_queued_frames_per_socket():
    sub = HelixSubscriber()
    frames = [_frame_2003("/dspEvent", [("i", n)]) for n in range(3)]
    _wire(sub, {2003: frames})

    events = sub.poll(timeout=0.1)
    assert [e.args[0] for e in events] == [0, 1, 2]


def test_frame_without_osc_address_is_skipped():
    sub = HelixSubscriber()
    # no '/' anywhere -> find returns -1 -> frame yields no Event
    _wire(sub, {2003: [b"\x00\x01\x02garbage-no-slash"]})
    assert sub.poll(timeout=0.05) == []


def test_poll_raises_when_not_connected():
    sub = HelixSubscriber()  # never wired: poller is None
    with pytest.raises(HelixError):
        sub.poll(timeout=0.01)


def test_close_closes_sockets_and_clears_state():
    sub = HelixSubscriber()
    socks = _wire(sub, {2001: [], 2003: []})
    sub.close()
    assert all(s.closed for s in socks)
    assert sub._socks == {}
    assert sub.poller is None


def test_context_manager_uses_connect_and_close(monkeypatch):
    sub = HelixSubscriber()
    calls = []
    monkeypatch.setattr(sub, "connect", lambda: (calls.append("connect"), sub)[1])
    monkeypatch.setattr(sub, "close", lambda: calls.append("close"))
    with sub as s:
        assert s is sub
    assert calls == ["connect", "close"]


def test_missing_pyzmq_raises_helixerror(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "zmq":
            raise ImportError("no zmq")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    sub = HelixSubscriber()
    with pytest.raises(HelixError):
        sub.connect()
