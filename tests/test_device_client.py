"""Unit tests for HelixClient RPC/reply parsing with a FAKE socket + poller.

No real ZeroMQ socket or device is used: we inject fakes that mimic pyzmq's
``poller.poll(ms)`` (truthy list once, then empty) and ``sock.recv()`` (returns
a pre-built OSC reply frame). The client's request ids come from
``itertools.count(1000)`` so the first reqid is 1000.
"""
from __future__ import annotations

import pytest

msgpack = pytest.importorskip("msgpack")

from helixgen.device.client import HelixClient, HelixError  # noqa: E402
from helixgen.device.osc import osc_encode  # noqa: E402


class FakePoller:
    """Mimic zmq.Poller: poll() yields a truthy events list once, then empty."""

    def __init__(self, frames):
        # one truthy poll result per queued frame
        self._remaining = len(frames)

    def register(self, *_a, **_k):
        pass

    def poll(self, _timeout_ms):
        if self._remaining > 0:
            self._remaining -= 1
            return [("sock", 1)]  # truthy, dict()-able
        return []


class FakeSock:
    """Mimic a zmq DEALER: send() is a no-op, recv() pops the next frame."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        return self._frames.pop(0)

    def close(self):
        pass


def _wire(client: HelixClient, frames):
    client.sock = FakeSock(frames)
    client.poller = FakePoller(frames)


def test_list_presets_parses_injected_reply():
    h = HelixClient()
    presets = [{"cid_": 904, "name": "Dream On", "cctp": 1000, "posi": 0}]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(presets, use_bin_type=True))],
    )
    _wire(h, [reply])

    out = h.list_presets()
    assert out == presets
    # request id 1000 was sent as the first int arg
    assert len(h.sock.sent) == 1


def test_list_presets_filters_and_sorts_by_pos():
    h = HelixClient()
    items = [
        {"cid_": 2, "name": "B", "cctp": 1000, "posi": 5},
        {"cid_": 9, "name": "setlist", "cctp": 1001, "posi": 1},  # not a preset
        {"cid_": 1, "name": "A", "cctp": 1000, "posi": 0},
    ]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(items, use_bin_type=True))],
    )
    _wire(h, [reply])

    out = h.list_presets()
    assert [m["name"] for m in out] == ["A", "B"]  # 1001 filtered, sorted by posi


def test_reply_with_wrong_reqid_is_ignored():
    h = HelixClient()
    # reply carries reqid 999, but client's first reqid is 1000 -> no match
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 999), ("b", msgpack.packb([{"cctp": 1000, "posi": 0}], use_bin_type=True))],
    )
    _wire(h, [reply])
    assert h.list_presets() == []


def test_ok_true_on_status_zero():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 1)])
    _wire(h, [reply])
    assert h.load_preset(904) is True


def test_ok_false_on_status_nonzero():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 1), ("i", 0)])
    _wire(h, [reply])
    assert h.load_preset(904) is False


def test_ok_false_when_no_status_frame():
    h = HelixClient()
    reply = osc_encode("/somethingelse", [("i", 1000), ("i", 0)])
    _wire(h, [reply])
    assert h.load_preset(904) is False


def test_rpc_raises_when_not_connected():
    h = HelixClient()  # never wired: sock is None
    with pytest.raises(HelixError):
        h.list_presets()


def test_set_model_raises_when_not_connected():
    h = HelixClient()
    with pytest.raises(HelixError):
        h.set_model(12345)


def test_slot_label():
    from helixgen.device.client import slot_label

    assert slot_label(0) == "1A"
    assert slot_label(5) == "2B"
    assert slot_label(None) == ""


def test_create_content_reads_new_cid_from_status_second_field():
    # /CreateContent replies /status [reqid, newCid, code] (cid in 2nd field!)
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    _wire(h, [reply])
    assert h.create_content(-2, 7, "x") == 930


def test_create_content_none_on_nonzero_code():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 5), ("i", 1)])  # code=1
    _wire(h, [reply])
    assert h.create_content(-2, 7, "x") is None


def test_save_preset_with_cid_ok():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 0)])
    _wire(h, [reply])
    assert h.save_preset_with_cid(930) is True


def test_malformed_reply_frame_raises_helixerror():
    # a frame that starts an OSC address but is never NUL-terminated -> the
    # parser raises ValueError, which _rpc must wrap as HelixError (not leak).
    h = HelixClient()
    _wire(h, [b"/GetContentRef no null terminator here"])
    with pytest.raises(HelixError):
        h.list_presets()
