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
    assert h._raw.create_content(-2, 7, "x") == 930


def test_create_content_none_on_nonzero_code():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 5), ("i", 1)])  # code=1
    _wire(h, [reply])
    assert h._raw.create_content(-2, 7, "x") is None


def test_save_preset_with_cid_ok():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 0)])
    _wire(h, [reply])
    assert h._raw.save_preset_with_cid(930) is True


def test_set_content_data_converts_and_sends():
    from helixgen.device import content as C
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 0)])
    _wire(h, [reply])
    # feed an edit-buffer (_sbepgsm) blob; set_content_data must convert it to
    # the stored-content format before sending.
    sbe = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    assert h._raw.set_content_data(930, sbe) is True
    sent = h.sock.sent[0]
    assert b"/SetContentData" in sent
    assert C.CONTENT_DATA_MAGIC in sent and C.MAGIC not in sent  # converted


def test_malformed_reply_frame_raises_helixerror():
    # a frame that starts an OSC address but is never NUL-terminated -> the
    # parser raises ValueError, which _rpc must wrap as HelixError (not leak).
    h = HelixClient()
    _wire(h, [b"/GetContentRef no null terminator here"])
    with pytest.raises(HelixError):
        h.list_presets()


# ---------------------------------------------------------------------------
# Multi-RPC sequencing fakes (each _rpc call gets exactly its own frame group)
# and a fake 2001 subscriber for mutating()-wrapped ops.
# ---------------------------------------------------------------------------

class SeqPoller:
    """Poller for a multi-RPC flow: returns truthy once per frame in the
    current group, then one empty poll (ending that rpc) before advancing to
    the next group."""

    def __init__(self, groups):
        self._groups = [list(g) for g in groups]
        self._i = 0    # current group index
        self._pos = 0  # frame within the current group

    def register(self, *_a, **_k):
        pass

    def poll(self, _timeout_ms):
        if self._i >= len(self._groups):
            return []
        grp = self._groups[self._i]
        if self._pos < len(grp):
            self._pos += 1
            return [("sock", 1)]  # truthy, dict()-able
        # group exhausted: end this rpc and advance to the next group
        self._i += 1
        self._pos = 0
        return []


class SeqSock:
    """Socket for a multi-RPC flow: recv() pops the next frame across all
    groups in order."""

    def __init__(self, groups):
        self._frames = [f for g in groups for f in g]
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        return self._frames.pop(0)

    def close(self):
        pass


def _wire_seq(client, groups):
    client.sock = SeqSock(groups)
    client.poller = SeqPoller(groups)


class _NoopSub:
    """Stand-in for HelixSubscriber so mutating() opens no real ZMQ socket."""

    def __init__(self, *a, **k):
        pass

    def connect(self):
        return self

    def close(self):
        pass


def _patch_sub(monkeypatch):
    from helixgen.device import subscribe as sub_mod
    monkeypatch.setattr(sub_mod, "HelixSubscriber", _NoopSub)


# -- enums + backward-compat aliases ----------------------------------------

def test_container_and_cctp_enum_values():
    from helixgen.device.client import Container, Cctp

    assert (int(Container.FACTORY), int(Container.POOL),
            int(Container.SETLISTS_ROOT), int(Container.USER_IRS)) == (-1, -2, -5, -11)
    assert (int(Cctp.PRESET), int(Cctp.SETLIST),
            int(Cctp.TEMPLATE), int(Cctp.REFERENCE)) == (1000, 1001, 1002, 1003)


def test_backward_compat_aliases():
    from helixgen.device.client import (
        FACTORY, USER, THROWAWAY, SETLISTS_ROOT, USER_IRS,
        CT_PRESET, CT_SETLIST, CT_TEMPLATE, Container,
    )

    assert FACTORY == -1
    assert USER == Container.POOL == -2
    assert USER_IRS == -11
    assert SETLISTS_ROOT == -5
    # DEPRECATED: -5 is the setlists root, kept as the throwaway alias value.
    assert THROWAWAY == SETLISTS_ROOT == -5
    assert (CT_PRESET, CT_SETLIST, CT_TEMPLATE) == (1000, 1001, 1002)


# -- setlist enumeration under -5 -------------------------------------------

def test_list_setlists_enumerates_cctp_1001_under_root():
    h = HelixClient()
    items = [
        {"cid_": 42, "name": "helixgen", "cctp": 1001, "posi": 1},
        {"cid_": 43, "name": "Throwaway", "cctp": 1001, "posi": 0},
        {"cid_": 99, "name": "not a setlist", "cctp": 1000, "posi": 2},
    ]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(items, use_bin_type=True))],
    )
    _wire(h, [reply])
    out = h.list_setlists()
    # only the two cctp==1001 items, sorted by posi
    assert [(s["cid_"], s["name"]) for s in out] == [(43, "Throwaway"), (42, "helixgen")]
    assert all(s["cctp"] == 1001 for s in out)


def test_resolve_setlist_cid_case_insensitive():
    h = HelixClient()
    items = [{"cid_": 42, "name": "Helixgen", "cctp": 1001, "posi": 0}]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(items, use_bin_type=True))],
    )
    _wire(h, [reply])
    assert h.resolve_setlist_cid("HELIXGEN") == 42


def test_resolve_setlist_cid_absent_returns_none():
    h = HelixClient()
    items = [{"cid_": 42, "name": "helixgen", "cctp": 1001, "posi": 0}]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(items, use_bin_type=True))],
    )
    _wire(h, [reply])
    assert h.resolve_setlist_cid("nope") is None


# -- _raw guardrail ----------------------------------------------------------

def test_raw_create_content_rejects_non_pool_container():
    h = HelixClient()  # unwired: guardrail fires before any RPC
    with pytest.raises(HelixError) as ei:
        h._raw.create_content(-5, 0, "x")
    assert "reference_into_setlist" in str(ei.value)


def test_raw_create_content_allows_pool():
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    _wire(h, [reply])
    assert h._raw.create_content(-2, 0, "x") == 930


# -- model-correct high-level ops -------------------------------------------

def test_install_into_pool_relists_by_name_for_cid(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    name = "White Limo Lead"
    # rpc 1000: _create_content -> /status [reqid, newCid=930, 0]
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    # rpc 1001: _set_content_data -> /status ok
    setdata = osc_encode("/status", [("i", 1001), ("i", 0), ("i", 0)])
    # rpc 1002: list_presets(POOL) -> the real cid is 777 (NOT the create's 930)
    presets = [{"cid_": 777, "name": name, "cctp": 1000, "posi": 3}]
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb(presets, use_bin_type=True))],
    )
    _wire_seq(h, [[create], [setdata], [listrep]])

    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    got = h.install_into_pool(blob, name, pos=3)
    assert got == 777  # re-listed by name, not the unreliable create reply cid


def test_reference_into_setlist_returns_ref_cid(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    # rpc 1000: _create_copy -> /status ok
    ok = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 0)])
    # rpc 1001: list_container(setlist) -> a 1003 reference back to pool_cid 777
    refs = [{"cid_": 555, "cctp": 1003, "rcid": 777, "posi": 0}]
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb(refs, use_bin_type=True))],
    )
    _wire_seq(h, [[ok], [listrep]])

    assert h.reference_into_setlist(42, 777, 0) == 555


def test_mirror_setlist_adds_and_removes(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    # rpc 1000: mirror lists current refs -> one ref (rcid=100,posi=0) not wanted
    current = [{"cid_": 501, "cctp": 1003, "rcid": 100, "posi": 0}]
    list_current = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(current, use_bin_type=True))],
    )
    # rpc 1001: remove_reference(501) -> /status ok
    remove_ok = osc_encode("/status", [("i", 1001), ("i", 0), ("i", 0)])
    # rpc 1002: reference_into_setlist create_copy -> /status ok
    add_ok = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    # rpc 1003: re-list to recover the new ref cid for pool_cid 200
    added = [{"cid_": 502, "cctp": 1003, "rcid": 200, "posi": 0}]
    list_added = osc_encode(
        "/GetContainerContents",
        [("i", 1003), ("b", msgpack.packb(added, use_bin_type=True))],
    )
    _wire_seq(h, [[list_current], [remove_ok], [add_ok], [list_added]])

    res = h.mirror_setlist(42, [200])
    assert res == {"added": [502], "removed": [501]}


# -- mutating() context ------------------------------------------------------

# -- connection resilience: bounded auto-reconnect --------------------------

class _FakeZmqError(Exception):
    """Stand-in for zmq.ZMQError so tests can trigger the drop path without a
    real ZeroMQ socket."""


class _DropSock:
    """A socket whose send()/recv() always raise the zmq error type."""

    def send(self, data):
        raise _FakeZmqError("connection reset by peer")

    def recv(self):
        raise _FakeZmqError("connection reset by peer")

    def close(self):
        pass


def _install_fake_zmq(h):
    """Point the client's _zmq at a namespace exposing _FakeZmqError as
    ZMQError, so _rpc treats it as a drop."""
    import types
    h._zmq = types.SimpleNamespace(ZMQError=_FakeZmqError)


def test_rpc_reconnects_and_recovers_after_drop():
    # First attempt (rid 1000) raises on send; reconnect() swaps in a healthy
    # socket and the retried rpc (rid 1001) returns the reply transparently.
    h = HelixClient(reconnect_tries=3, reconnect_backoff=0.0)
    _install_fake_zmq(h)
    h.sock = _DropSock()
    h.poller = FakePoller([])

    # the retry consumes rid 1001 (1000 was burned by the failed first attempt)
    reply = osc_encode("/status", [("i", 1001), ("i", 0), ("i", 0)])

    def _fake_reconnect():
        h.sock = FakeSock([reply])
        h.poller = FakePoller([reply])
        return h

    h.reconnect = _fake_reconnect

    assert h.load_preset(904) is True  # recovered, parsed the ok status


def test_rpc_raises_helixerror_after_exhausting_reconnects():
    h = HelixClient(reconnect_tries=3, reconnect_backoff=0.0)
    _install_fake_zmq(h)
    h.sock = _DropSock()
    h.poller = FakePoller([])

    reconnects = []

    def _fake_reconnect():
        reconnects.append(1)
        h.sock = _DropSock()  # still broken
        h.poller = FakePoller([])
        return h

    h.reconnect = _fake_reconnect

    with pytest.raises(HelixError) as ei:
        h.load_preset(904)
    assert "reboot" in str(ei.value).lower()
    assert len(reconnects) == 3  # exactly reconnect_tries reconnect attempts


def test_rpc_non_drop_error_does_not_retry():
    # a malformed reply is NOT a connection drop -> propagate immediately,
    # never call reconnect().
    h = HelixClient()
    _wire(h, [b"/GetContentRef no null terminator here"])
    reconnects = []
    h.reconnect = lambda: reconnects.append(1)
    with pytest.raises(HelixError):
        h.list_presets()
    assert reconnects == []


def test_reconnect_reopens_socket(monkeypatch):
    # reconnect() closes the old socket and re-runs _open_socket (no verify).
    h = HelixClient()
    closed = []

    class OldSock:
        def close(self):
            closed.append(1)

    h.sock = OldSock()
    opened = []
    monkeypatch.setattr(h, "_open_socket", lambda: opened.append(1))
    h.reconnect()
    assert closed == [1]
    assert opened == [1]


def test_mutating_survives_subscriber_open_failure(monkeypatch):
    # a subscriber that fails to open must not abort the mutating() batch.
    from helixgen.device import subscribe as sub_mod

    class BoomSub:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            raise RuntimeError("2001 subscribe failed")

        def close(self):
            pass

    monkeypatch.setattr(sub_mod, "HelixSubscriber", BoomSub)
    h = HelixClient()
    h.mutate_settle = 0
    ran = False
    with h.mutating():
        ran = True
    assert ran is True
    assert h._mutating == 0


def test_mutating_opens_and_closes_subscriber(monkeypatch):
    from helixgen.device import subscribe as sub_mod

    events = []

    class FakeSub:
        def __init__(self, ip, ports=(2001,)):
            events.append(("init", ip, tuple(ports)))

        def connect(self):
            events.append(("connect",))
            return self

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(sub_mod, "HelixSubscriber", FakeSub)
    h = HelixClient()
    h.mutate_settle = 0

    assert h._mutating == 0
    with h.mutating():
        assert h._mutating == 1
        # nesting is safe: inner context does not open a second subscriber
        with h.mutating():
            assert h._mutating == 2
        assert h._mutating == 1
    assert h._mutating == 0

    assert ("init", h.ip, (2001,)) in events
    assert events.count(("connect",)) == 1  # only the outermost opened one
    assert events.count(("close",)) == 1
    assert events.index(("close",)) > events.index(("connect",))
