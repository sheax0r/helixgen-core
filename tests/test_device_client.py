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


def test_get_content_sends_getcontentdata_and_returns_blob():
    # /GetContentData [reqid, cid] is the NON-activating read: it must send
    # /GetContentData and NEVER /LoadPresetWithCID, and return the raw blob.
    from helixgen.device import content as C
    h = HelixClient()
    stored = C.encode_content_data({"cg__": {}, "pm__": [], "sfg_": {}})
    reply = osc_encode("/GetContentData", [("i", 1000), ("b", stored)])
    _wire(h, [reply])

    blob = h.get_content(1064)
    assert blob == stored
    assert len(h.sock.sent) == 1  # exactly one RPC — no separate load_preset
    sent = h.sock.sent[0]
    assert b"/GetContentData" in sent
    assert b"/LoadPresetWithCID" not in sent


def test_get_content_accepts_edit_buffer_magic_too():
    # If the device happened to answer with the edit-buffer (_sbepgsm) form,
    # get_content must still accept it.
    from helixgen.device import content as C
    h = HelixClient()
    sbe = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    reply = osc_encode("/GetContentData", [("i", 1000), ("b", sbe)])
    _wire(h, [reply])
    assert h.get_content(1064) == sbe


def test_get_content_raises_when_no_blob():
    h = HelixClient()
    reply = osc_encode("/GetContentData", [("i", 1000), ("i", 0)])
    _wire(h, [reply])
    with pytest.raises(HelixError):
        h.get_content(1064)


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


# --- global-settings property methods -------------------------------------

from helixgen.device import settings as _S  # noqa: E402


def test_get_property_parses_value_blob():
    h = HelixClient()
    blob = _S.encode_value_blob("global.midi.channel", "i", 7)
    reply = osc_encode("/getPropertyValue",
                       [("i", 1000), ("s", "global.midi.channel"), ("b", blob)])
    _wire(h, [reply])
    pv = h.get_property("global.midi.channel")
    assert pv.key == "global.midi.channel" and pv.value == 7 and pv.type == "i"
    # request went out as /PropertyValueGet [reqid, key]
    assert h.sock.sent[0].startswith(b"/PropertyValueGet")


def test_get_property_def_parses_def_blob():
    h = HelixClient()
    # golden def blob for global.tuner.type (enum Needle/Strobe)
    defblob = bytes.fromhex(
        "666564707067736d8ace64697370a0ce6476616c83ce6b65795fb1676c6f6261"
        "6c2e74756e65722e74797065ce74797065a169ce76616c5f01ce69645f5fcce3"
        "ce6e616d65aa54756e65722054797065ce73687274a0ce7479706500ce756e74"
        "730fce766d617801ce766d696e00ce766e6d6592a64e6565646c65a65374726f"
        "6265")
    reply = osc_encode("/keyPropertyDefinition",
                       [("i", 1000), ("s", "global.tuner.type"), ("b", defblob)])
    _wire(h, [reply])
    d = h.get_property_def("global.tuner.type")
    assert d.enum == ["Needle", "Strobe"] and d.vmax == 1


def test_set_property_true_on_success():
    h = HelixClient()
    reply = osc_encode("/success", [("i", 1000), ("i", 0)])
    _wire(h, [reply])
    assert h.set_property("global.midi.channel", "i", 5) is True
    assert h.sock.sent[0].startswith(b"/PropertyValueSet")


def test_set_property_raises_on_error():
    h = HelixClient()
    reply = osc_encode("/error", [("i", 1000), ("i", 0), ("s", "NOPE")])
    _wire(h, [reply])
    with pytest.raises(HelixError):
        h.set_property("global.bad.key", "i", 1)


def test_get_property_raises_on_error():
    h = HelixClient()
    reply = osc_encode("/error", [("i", 1000), ("i", 0), ("s", "NOPE")])
    _wire(h, [reply])
    with pytest.raises(HelixError):
        h.get_property("global.bad.key")


def test_set_property_refuses_self_severing_key():
    h = HelixClient()
    # no socket wired — guard must fire BEFORE any RPC attempt (ValueError,
    # same type coerce_value raises, so the CLI/MCP set paths surface it cleanly)
    with pytest.raises(ValueError):
        h.set_property("global.wifi.enable", "i", 0)


# -- library polish: IR delete + setlist create/delete/duplicate -------------

def test_delete_irs_removes_from_user_irs_container(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    ok = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 1)])
    _wire(h, [ok])
    assert h.delete_irs([1159]) is True
    # the /RemoveContent went to the USER_IRS container (-11)
    from helixgen.device.osc import parse_osc_message
    raw = h.sock.sent[0]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    assert addr == "/RemoveContent"
    assert args[1] == ("i", -11)


def test_create_setlist_sends_ctype_1003_under_root(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    # rpc 1000: list -5 for the next free posi (one setlist at posi 0)
    existing = [{"cid_": 816, "name": "Throwaway", "cctp": 1001, "posi": 0}]
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(existing, use_bin_type=True))])
    # rpc 1001: /CreateContent -> /status [reqid, newCid, 0]
    create = osc_encode("/status", [("i", 1001), ("i", 1186), ("i", 0)])
    # rpc 1002: resolve_setlist_cid re-lists -5 -> real cid
    after = existing + [{"cid_": 1186, "name": "ZZC-x", "cctp": 1001, "posi": 1}]
    list2 = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb(after, use_bin_type=True))])
    _wire_seq(h, [[list1], [create], [list2]])

    assert h.create_setlist("ZZC-x") == 1186
    from helixgen.device.osc import parse_osc_message
    raw = h.sock.sent[1]  # the /CreateContent frame
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    assert addr == "/CreateContent"
    assert args[1] == ("i", -5)      # setlists root
    assert args[2] == ("i", 1)       # next free posi
    assert args[3] == ("i", 1003)    # setlist ctype


def test_create_setlist_none_on_nonzero_code(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    create = osc_encode("/status", [("i", 1001), ("i", 0), ("i", -47)])
    _wire_seq(h, [[list1], [create]])
    assert h.create_setlist("ZZC-x") is None


def test_delete_setlist_removes_from_root(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    ok = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 1)])
    _wire(h, [ok])
    assert h.delete_setlist(1186) is True
    from helixgen.device.osc import parse_osc_message
    raw = h.sock.sent[0]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    assert addr == "/RemoveContent"
    assert args[1] == ("i", -5)


def test_duplicate_setlist_refs_copies_in_posi_order(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    calls = []

    def fake_list(cid, strict=False):
        calls.append(("list", cid))
        if cid == 42:   # source: two refs, out of posi order
            return [
                {"cid_": 502, "cctp": 1003, "rcid": 200, "posi": 1},
                {"cid_": 501, "cctp": 1003, "rcid": 100, "posi": 0},
            ]
        return []       # destination: empty

    def fake_ref(dst, pool_cid, pos):
        calls.append(("ref", dst, pool_cid, pos))
        return 900 + pos

    monkeypatch.setattr(h, "list_container", fake_list)
    monkeypatch.setattr(h, "reference_into_setlist", fake_ref)
    assert h.duplicate_setlist_refs(42, 77) == 2
    assert ("ref", 77, 100, 0) in calls and ("ref", 77, 200, 1) in calls


def test_duplicate_setlist_refs_requires_empty_destination(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    monkeypatch.setattr(
        h, "list_container",
        lambda cid, strict=False: [{"cid_": 1, "cctp": 1003, "rcid": 5, "posi": 0}])
    with pytest.raises(HelixError, match="empty"):
        h.duplicate_setlist_refs(42, 77)


# -- strict container listings (ir-prune / duplicate safety; review #37-1) ----

def test_list_container_strict_raises_on_no_reply():
    """A /GetContainerContents timeout (zero reply frames) must NOT read as an
    empty container in strict mode — an empty 'pool' would make every user IR
    look like an orphan to ir-prune."""
    h = HelixClient()
    _wire(h, [])  # poller never fires -> _rpc returns []
    with pytest.raises(HelixError, match="no reply"):
        h.list_container(-2, strict=True)
    # non-strict keeps the legacy silent-empty behavior
    _wire(h, [])
    assert h.list_container(-2) == []


def test_list_container_strict_raises_on_undecodable_blob():
    """A truncated/undecodable listing blob (chunked-reply decode failure) must
    raise in strict mode instead of silently dropping items."""
    h = HelixClient()
    # 0xc1 is an invalid msgpack byte -> decode_blob returns raw bytes
    reply = osc_encode(
        "/GetContainerContents", [("i", 1000), ("b", b"\xc1garbage")])
    _wire(h, [reply])
    with pytest.raises(HelixError, match="undecodable"):
        h.list_container(-2, strict=True)
    _wire(h, [reply])
    assert h.list_container(-2) == []  # legacy behavior unchanged


def test_list_container_strict_accepts_genuine_empty_array():
    h = HelixClient()
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    _wire(h, [reply])
    assert h.list_container(-2, strict=True) == []


def test_list_presets_and_irs_pass_strict_through(monkeypatch):
    h = HelixClient()
    seen = []

    def fake_list(cid, strict=False):
        seen.append((cid, strict))
        return []

    monkeypatch.setattr(h, "list_container", fake_list)
    h.list_presets(-2, strict=True)
    h.list_irs(strict=True)
    h.list_setlists(strict=True)
    assert all(s is True for _c, s in seen) and len(seen) == 3


def test_duplicate_setlist_refs_lists_strictly(monkeypatch):
    """duplicate's 'destination must be empty' precondition must not trust a
    silent-empty listing (review #37-1)."""
    _patch_sub(monkeypatch)
    h = HelixClient()
    h.mutate_settle = 0
    seen = []

    def fake_list(cid, strict=False):
        seen.append((cid, strict))
        return []

    monkeypatch.setattr(h, "list_container", fake_list)
    h.duplicate_setlist_refs(42, 77)
    assert seen and all(s is True for _c, s in seen)


def test_hex_hash_lowercases_str():
    assert HelixClient._hex_hash("AA" * 16) == "aa" * 16
    assert HelixClient._hex_hash(bytes.fromhex("ab" * 16)) == "ab" * 16


def test_create_setlist_retries_relist_for_real_cid(monkeypatch):
    """The create-reply cid is unreliable — the relist is retried before
    falling back to it (review #37-10)."""
    _patch_sub(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda s: None)
    h = HelixClient()
    h.mutate_settle = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    create = osc_encode("/status", [("i", 1001), ("i", 930), ("i", 0)])
    # first relist: setlist not visible yet; second relist: there, cid 1186
    empty = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb([], use_bin_type=True))])
    after = osc_encode(
        "/GetContainerContents",
        [("i", 1003), ("b", msgpack.packb(
            [{"cid_": 1186, "name": "ZZC-x", "cctp": 1001, "posi": 0}],
            use_bin_type=True))])
    _wire_seq(h, [[list1], [create], [empty], [after]])
    assert h.create_setlist("ZZC-x") == 1186


def test_create_setlist_falls_back_to_reply_cid_with_warning(monkeypatch, caplog):
    import logging

    _patch_sub(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda s: None)
    h = HelixClient()
    h.mutate_settle = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    create = osc_encode("/status", [("i", 1001), ("i", 930), ("i", 0)])
    empties = [osc_encode(
        "/GetContainerContents",
        [("i", 1002 + i), ("b", msgpack.packb([], use_bin_type=True))])
        for i in range(4)]
    _wire_seq(h, [[list1], [create]] + [[e] for e in empties])
    with caplog.at_level(logging.WARNING):
        assert h.create_setlist("ZZC-x") == 930
    assert any("unreliable" in r.message for r in caplog.records)

# --- product info (/ProductInfoGet) ------------------------------------------

def _product_info_reply(reqid=1000):
    # 4CC int keys exactly as the live device replies (fw 1.3.2 capture).
    def cc(s):
        return int.from_bytes(s.encode(), "big")
    info = {
        cc("clid"): 14,
        cc("host"): {
            cc("ctyp"): 1, cc("hoid"): 1, cc("id__"): 2490368,
            cc("name"): "stadium",
            cc("res_"): [{cc("path"): "/dev/p35-scribble"}],
            cc("sdas"): 23338147840, cc("sdcs"): 3, cc("sdts"): 23340777472,
            cc("snum"): "47292244582131381",
            cc("vers"): {cc("buld"): 1340, cc("date"): 1776097298,
                         cc("majo"): 1, cc("mino"): 3, cc("patc"): 2,
                         cc("targ"): 0},
        },
        cc("nexs"): [],
    }
    return osc_encode("/getProductInfo",
                      [("i", reqid), ("b", msgpack.packb(info, use_bin_type=True))])


def test_product_info_decodes_and_curates():
    h = HelixClient()
    _wire(h, [_product_info_reply()])
    info = h.product_info()
    assert info["model"] == "stadium"
    assert info["device_id"] == 2490368
    assert info["helixgen_model"] == "stadium_xl"
    assert info["serial"] == "47292244582131381"
    assert info["firmware"] == "1.3.2"
    assert info["firmware_build"] == 1340
    assert info["firmware_date"] == "2026-04-13"
    assert info["sd_total_bytes"] == 23340777472
    assert info["sd_available_bytes"] == 23338147840
    # full 4CC-decoded reply available for anything uncurated
    assert info["raw"]["host"]["sdcs"] == 3


def test_product_info_raises_without_reply():
    h = HelixClient()
    _wire(h, [])
    with pytest.raises(HelixError, match="getProductInfo"):
        h.product_info()
