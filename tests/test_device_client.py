"""Unit tests for HelixClient RPC/reply parsing with a FAKE socket + poller.

No real ZeroMQ socket or device is used: we inject fakes that mimic pyzmq's
``poller.poll(ms)`` (truthy list once, then empty) and ``sock.recv()`` (returns
a pre-built OSC reply frame). The client's request ids come from
``itertools.count(1000)`` so the first reqid is 1000.
"""
from __future__ import annotations

import logging

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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    # reply carries reqid 999, but client's first reqid is 1000 -> no match
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 999), ("b", msgpack.packb([{"cctp": 1000, "posi": 0}], use_bin_type=True))],
    )
    _wire(h, [reply])
    assert h.list_presets() == []


def test_ok_true_on_status_zero():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 1)])
    _wire(h, [reply])
    assert h.load_preset(904) is True


def test_ok_false_on_status_nonzero():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 1), ("i", 0)])
    _wire(h, [reply])
    assert h.load_preset(904) is False


def test_ok_false_when_no_status_frame():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/somethingelse", [("i", 1000), ("i", 0)])
    _wire(h, [reply])
    assert h.load_preset(904) is False


def test_rpc_raises_when_not_connected():
    h = HelixClient("10.0.0.99")  # never wired: sock is None
    with pytest.raises(HelixError):
        h.list_presets()


def test_slot_label():
    from helixgen.device.client import slot_label

    assert slot_label(0) == "1A"
    assert slot_label(5) == "2B"
    assert slot_label(None) == ""


def test_container_for_setlist_keyword():
    from helixgen.device.client import (
        container_for_setlist_keyword, Container,
    )

    assert container_for_setlist_keyword("user") == Container.POOL
    assert container_for_setlist_keyword("factory") == Container.FACTORY
    assert container_for_setlist_keyword("throwaway") == Container.SETLISTS_ROOT
    # case/whitespace-insensitive
    assert container_for_setlist_keyword("  User ") == Container.POOL
    # unknown -> ValueError naming the valid keywords
    with pytest.raises(ValueError):
        container_for_setlist_keyword("bogus")
    with pytest.raises(ValueError):
        container_for_setlist_keyword("")


def test_create_content_reads_new_cid_from_status_second_field():
    # /CreateContent replies /status [reqid, newCid, code] (cid in 2nd field!)
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    _wire(h, [reply])
    assert h._raw.create_content(-2, 7, "x") == 930


def test_create_content_none_on_nonzero_code_when_content_absent():
    # code != 0 AND the confirming re-list doesn't find (name, posi) -> the
    # write genuinely didn't land, so the historic None contract stands.
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 5), ("i", 1)])  # code=1
    _wire(h, [reply])
    h.create_confirm_delay = 0
    assert h._raw.create_content(-2, 7, "x") is None


def test_save_preset_with_cid_ok():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 0)])
    _wire(h, [reply])
    assert h._raw.save_preset_with_cid(930) is True


def test_set_content_data_converts_and_sends():
    from helixgen.device import content as C
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    sbe = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    reply = osc_encode("/GetContentData", [("i", 1000), ("b", sbe)])
    _wire(h, [reply])
    assert h.get_content(1064) == sbe


def test_get_content_raises_when_no_blob():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/GetContentData", [("i", 1000), ("i", 0)])
    _wire(h, [reply])
    with pytest.raises(HelixError):
        h.get_content(1064)


def test_malformed_reply_frame_raises_helixerror():
    # a frame that starts an OSC address but is never NUL-terminated -> the
    # parser raises ValueError, which _rpc must wrap as HelixError (not leak).
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    items = [{"cid_": 42, "name": "Helixgen", "cctp": 1001, "posi": 0}]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(items, use_bin_type=True))],
    )
    _wire(h, [reply])
    assert h.resolve_setlist_cid("HELIXGEN") == 42


def test_resolve_setlist_cid_absent_returns_none():
    h = HelixClient("10.0.0.99")
    items = [{"cid_": 42, "name": "helixgen", "cctp": 1001, "posi": 0}]
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb(items, use_bin_type=True))],
    )
    _wire(h, [reply])
    assert h.resolve_setlist_cid("nope") is None


def test_resolve_setlist_cid_is_strict_by_default(monkeypatch):
    """#39: a timeout/undecodable listing must raise, not read as absent — the
    default must be strict so every existing caller (which doesn't pass
    strict= explicitly) gets the safe behavior for free."""
    h = HelixClient("10.0.0.99")
    _wire(h, [])  # poller never fires -> _rpc returns [] -> timeout
    with pytest.raises(HelixError, match="no reply"):
        h.resolve_setlist_cid("helixgen")


def test_resolve_setlist_cid_strict_true_forwarded_to_list_setlists(monkeypatch):
    h = HelixClient("10.0.0.99")
    seen = []

    def fake_list_setlists(*, strict=False):
        seen.append(strict)
        return []

    monkeypatch.setattr(h, "list_setlists", fake_list_setlists)
    h.resolve_setlist_cid("anything")
    assert seen == [True]


def test_resolve_setlist_cid_explicit_non_strict_still_works(monkeypatch):
    """The one deliberate lenient use (create_setlist's post-create re-list
    retry) must still be reachable via strict=False."""
    h = HelixClient("10.0.0.99")
    seen = []

    def fake_list_setlists(*, strict=False):
        seen.append(strict)
        return []

    monkeypatch.setattr(h, "list_setlists", fake_list_setlists)
    assert h.resolve_setlist_cid("anything", strict=False) is None
    assert seen == [False]


def test_list_setlists_by_name_returns_all_matches(monkeypatch):
    """#52: the multi-match helper returns every setlist whose name matches
    (case-insensitive, stripped both sides), preserving list order."""
    h = HelixClient("10.0.0.99")
    setlists = [
        {"cid_": 10, "name": "gigs", "cctp": 1001, "posi": 0},
        {"cid_": 20, "name": "GIGS", "cctp": 1001, "posi": 1},
        {"cid_": 30, "name": " gigs ", "cctp": 1001, "posi": 2},
        {"cid_": 40, "name": "other", "cctp": 1001, "posi": 3},
    ]
    monkeypatch.setattr(h, "list_setlists", lambda *, strict=False: list(setlists))
    matches = h.list_setlists_by_name("GiGs")
    assert [m["cid_"] for m in matches] == [10, 20, 30]


def test_list_setlists_by_name_accepts_prefetched(monkeypatch):
    """When given a pre-fetched listing it filters that without an extra RPC."""
    h = HelixClient("10.0.0.99")

    def _boom(*, strict=False):
        raise AssertionError("must not re-list when setlists= is supplied")

    monkeypatch.setattr(h, "list_setlists", _boom)
    pre = [{"cid_": 1, "name": "a"}, {"cid_": 2, "name": "A"}]
    assert [m["cid_"] for m in h.list_setlists_by_name("a", setlists=pre)] == [1, 2]


def test_resolve_setlist_cid_routes_through_by_name(monkeypatch):
    """resolve returns the first match's cid via the shared helper."""
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(
        h, "list_setlists_by_name",
        lambda name, *, strict=True, setlists=None: [{"cid_": 7, "name": name}])
    assert h.resolve_setlist_cid("x") == 7


# -- _raw guardrail ----------------------------------------------------------

def test_raw_create_content_rejects_non_pool_container():
    h = HelixClient("10.0.0.99")  # unwired: guardrail fires before any RPC
    with pytest.raises(HelixError) as ei:
        h._raw.create_content(-5, 0, "x")
    assert "reference_into_setlist" in str(ei.value)


def test_raw_create_content_allows_pool():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    _wire(h, [reply])
    assert h._raw.create_content(-2, 0, "x") == 930


# -- model-correct high-level ops -------------------------------------------

def test_install_into_pool_relists_by_name_for_cid(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
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


# -- #38: /CreateContent non-zero status + orphan-stub hardening ------------

def test_push_to_slot_happy_path_returns_cid(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    setdata = osc_encode("/status", [("i", 1001), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [setdata]])
    assert h._raw.push_to_slot(-2, 3, "X", blob) == 930


def test_push_to_slot_nonzero_code_with_content_present_is_success(monkeypatch):
    """#38 root cause: field 3 of the /CreateContent /status reply tracks the
    device's edit-buffer dirty flag (``hist``), NOT an error code. With a dirty
    active preset the device answers ``code == 1`` yet creates the content at
    the requested posi. The confirming re-list finds it, so this is a SUCCESS:
    push_to_slot must carry on to SetContentData, return the cid, and delete
    nothing."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    # rpc 1000: create -> /status [reqid, newCid=1237, code=1]  (dirty buffer)
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    # rpc 1001: the confirming re-list -> the content IS there, real cid 777
    # (the create-reply cid stays documented-unreliable; the re-list wins)
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    # rpc 1002: SetContentData into the confirmed cid -> ok
    setdata = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [listrep], [setdata]])

    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777
    # nothing was destroyed: the write landed
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)
    # and the content went into the re-listed cid, not the reply cid
    assert any(b"/SetContentData" in s for s in h.sock.sent)


def test_push_to_slot_zero_code_still_succeeds_without_relist(monkeypatch):
    """code == 0 with the content present stays exactly as before — a clean
    create needs no confirming re-list (the callers re-list by name anyway)."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 0, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    setdata = osc_encode("/status", [("i", 1001), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [setdata]])
    assert h._raw.push_to_slot(-2, 3, "X", blob) == 930
    assert not any(b"/GetContainerContents" in s for s in h.sock.sent)


def test_push_to_slot_raises_when_nonzero_and_content_absent(monkeypatch):
    """The genuine failure: non-zero code AND the confirming re-list (bounded
    settle/retry, the container index lags) never finds the content. Only then
    is it an error."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    empty = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb([], use_bin_type=True))])
        for r in range(1001, 1006)]
    _wire_seq(h, [[create]] + [[f] for f in empty])

    with pytest.raises(HelixError) as ei:
        h._raw.push_to_slot(-2, 5, "X", blob)
    msg = str(ei.value)
    assert "status code 1" in msg  # code surfaced
    assert "1237" in msg           # allocated cid surfaced for recovery
    assert "#38" in msg
    # the confirming re-list retried before giving up
    assert len([s for s in h.sock.sent if b"/GetContainerContents" in s]) > 1
    # nothing was written into a slot we could not confirm
    assert not any(b"/SetContentData" in s for s in h.sock.sent)


def test_save_edit_buffer_to_nonzero_code_with_content_present_is_success(
        monkeypatch):
    """Same root cause on the save path: saving the edit buffer means the
    buffer is dirty by definition, so ``code == 1`` is the NORMAL reply here.
    The preset was created — confirm it and save into the confirmed cid."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    saved = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [listrep], [saved]])

    assert h._raw.save_edit_buffer_to(-2, 5, "X") == 777
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)
    assert any(b"/SavePresetWithCID" in s for s in h.sock.sent)


def test_push_to_slot_cleanup_relists_not_create_cid_on_setdata_failure(monkeypatch):
    """On a SetContentData failure, cleanup must delete the entry we created by
    (name, pos) from a fresh listing — NOT the unreliable create-reply cid."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    # rpc 1000: create ok, reply cid=930 (unreliable)
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    # rpc 1001: set_content_data FAILS (code 1)
    setfail = osc_encode("/status", [("i", 1001), ("i", 1), ("i", 0)])
    # rpc 1002: _delete_created_stub re-lists -> real cid is 777 (not 930)
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    # rpc 1003: delete ok
    delete = osc_encode("/status", [("i", 1003), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [setfail], [listrep], [delete]])

    assert h._raw.push_to_slot(-2, 5, "X", blob,
                               prechecked_empty=True) is None
    del_sent = [s for s in h.sock.sent if b"/RemoveContent" in s]
    assert del_sent, "expected a /RemoveContent cleanup"
    # the msgpack cid list in the delete frame must carry 777 (relist), not 930
    assert b"\x77" not in del_sent[0] or True  # (777 asserted structurally below)
    import msgpack as _mp
    # pull the trailing blob arg (the msgpack cid array) out of the frame
    assert _mp.packb([777], use_bin_type=True) in del_sent[0]
    assert _mp.packb([930], use_bin_type=True) not in del_sent[0]


def test_push_to_slot_without_a_precheck_never_cleans_up(monkeypatch, caplog):
    """``slots restore --force`` skips the emptiness precheck, so the entry at
    (name, pos) may be a PRE-EXISTING occupant. A failed write must leave it
    alone — deleting it would destroy a preset we never created (#38)."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    # create reports the dirty-buffer flag; confirm re-list matches the occupant
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 1)])
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    # set_content_data FAILS on the occupant we just "confirmed"
    setfail = osc_encode("/status", [("i", 1002), ("i", 1), ("i", 0)])
    _wire_seq(h, [[create], [listrep], [setfail]])

    with caplog.at_level(logging.WARNING):
        assert h._raw.push_to_slot(-2, 5, "X", blob,
                                   prechecked_empty=False) is None
    assert not any(b"/RemoveContent" in s for s in h.sock.sent), \
        "a --force write failure must not delete the slot's occupant"
    assert "may predate this call" in caplog.text


def test_push_to_slot_with_a_precheck_still_cleans_up(monkeypatch):
    """The default (prechecked-empty) path keeps deleting its own orphan stub —
    the no-cleanup guard must be scoped to --force, not blanket."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    setfail = osc_encode("/status", [("i", 1001), ("i", 1), ("i", 0)])
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    delete = osc_encode("/status", [("i", 1003), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [setfail], [listrep], [delete]])

    assert h._raw.push_to_slot(-2, 5, "X", blob,
                               prechecked_empty=True) is None
    assert any(b"/RemoveContent" in s for s in h.sock.sent)


def test_delete_created_stub_lists_strictly(monkeypatch, caplog):
    """A dropped listing reply must raise out of list_container and be reported
    as a failed listing — never decode as an empty container and read as
    'no entry matched', which looks like there was nothing to clean up (#40)."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    seen = {}

    def _fake_list(cid, *, strict=False):
        seen["strict"] = strict
        raise HelixError("timeout")
    monkeypatch.setattr(h, "list_container", _fake_list)

    with caplog.at_level(logging.WARNING):
        assert h._delete_created_stub(-2, "X", 5) is None
    assert seen["strict"] is True
    assert "the listing failed" in caplog.text


def test_confirmed_create_never_reaches_the_destructive_cleanup(monkeypatch):
    """The data-destroying path: cleanup must be UNREACHABLE once the confirming
    re-list found the content. Pinned by making _delete_created_stub explode —
    the old code deleted a preset that had been written correctly."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0

    def _boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("cleanup ran on a create that landed")
    monkeypatch.setattr(h, "_delete_created_stub", _boom)

    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    setdata = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [listrep], [setdata]])
    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777


def test_create_status_error_does_not_delete(monkeypatch):
    """_create_status_error is only reached when the bounded confirming re-list
    already established the content is ABSENT — so there is nothing of ours to
    remove. It must not re-list-and-delete: an entry that shows up in that later
    listing is the create landing late, and deleting it is the #38 data loss."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    # pinned locally so the wired frame count below stays in step with the
    # retry budget regardless of the shipped default
    h.create_confirm_tries = 3
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    # every confirming listing is empty...
    empty = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb([], use_bin_type=True))])
        for r in range(1001, 1004)]
    # ...but a further listing WOULD show the entry (the index caught up late).
    late = osc_encode(
        "/GetContainerContents",
        [("i", 1004), ("b", msgpack.packb(
            [{"cid_": 777, "name": "X", "cctp": 1000, "posi": 5}],
            use_bin_type=True))],
    )
    delete = osc_encode("/status", [("i", 1005), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create]] + [[f] for f in empty] + [[late], [delete]])

    with pytest.raises(HelixError):
        h._raw.push_to_slot(-2, 5, "X", blob)
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)
    # only the bounded confirm listings were made — no extra cleanup listing
    assert len([s for s in h.sock.sent
                if b"/GetContainerContents" in s]) == h.create_confirm_tries


def test_create_status_error_message_names_the_real_precondition(monkeypatch):
    """Message contract (#38). The old text said to power-cycle the Helix — live
    A/B showed that demonstrably does not help, because the same dirty edit
    buffer is reloaded on boot. Root-causing went further: field 3 is the edit
    buffer's dirty flag, so a code of 1 is not the failure and "save or reload
    it and retry" is a non-remedy. The message must say so, matching the
    saw_cidless / not-listed-cleanly branches rather than contradicting them."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    empty = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb([], use_bin_type=True))])
        for r in range(1001, 1006)]
    _wire_seq(h, [[create]] + [[f] for f in empty])

    with pytest.raises(HelixError) as ei:
        h._raw.push_to_slot(-2, 5, "X", blob)
    msg = str(ei.value)
    assert "power-cycle" not in msg.lower()
    assert "unsaved edits" in msg
    # code 1 is the dirty-buffer flag, never the diagnosis — no non-remedy
    assert "save or reload" not in msg.lower()
    assert "expected code 0" not in msg.lower()
    assert "not an error" in msg.lower()
    # the recoverable facts stay in the message
    assert "status code 1" in msg
    assert "1237" in msg
    assert "#38" in msg


def test_cidless_preset_message_calls_the_survivor_an_empty_stub(monkeypatch):
    """A confirming listing that shows (name, pos) but no cid is raised BEFORE
    the content write, so on the preset paths what survives is an EMPTY stub,
    not a saved tone. Telling the user "the content is on the device, don't
    retry" would leave an empty preset squatting the slot while they believe
    the save worked — `device list` shows the name either way."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    # every confirming listing shows the entry but never reports a cid
    cidless = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb(
            [{"name": "X", "posi": 5, "cctp": 1000}], use_bin_type=True))])
        for r in range(1001, 1007)]
    _wire_seq(h, [[create]] + [[f] for f in cidless])

    with pytest.raises(HelixError) as ei:
        h._raw.push_to_slot(-2, 5, "X", blob)
    msg = str(ei.value)
    assert "EMPTY" in msg
    assert "Delete it before retrying" in msg
    # and it must NOT claim the tone itself made it onto the device. The
    # no-cleanup-needed wording is built from `what`, which is "pool" on this
    # path — asserting on any other noun would pass vacuously.
    assert "the pool appears to be on the device" not in msg
    assert "do NOT retry blindly" not in msg
    # the create path itself still deletes nothing (#38)
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_cidless_setlist_message_does_not_call_it_a_stub(monkeypatch):
    """The setlist path is the exception: an empty setlist container IS the
    deliverable, so a surviving entry needs no cleanup and must not be
    described as an empty stub to delete."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    create = osc_encode("/status", [("i", 1001), ("i", 1186), ("i", 1)])
    cidless = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb(
            [{"name": "ZZC-x", "posi": 0}], use_bin_type=True))])
        for r in range(1002, 1008)]
    _wire_seq(h, [[list1], [create]] + [[f] for f in cidless])

    with pytest.raises(HelixError) as ei:
        h.create_setlist("ZZC-x")
    msg = str(ei.value)
    assert "EMPTY" not in msg
    assert "the setlist appears to be on the device" in msg
    assert "helixgen device setlists" in msg


def test_cleanup_that_matches_nothing_is_reported(monkeypatch, caplog):
    """The silent-no-op hazard: when cleanup runs (a genuine create-then-write
    failure) but the listing matches nothing, that must be reported. The old
    silence is why the orphan accounting looked clean."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    setfail = osc_encode("/status", [("i", 1001), ("i", 1), ("i", 0)])
    # the cleanup listing is stale/empty -> nothing matches
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb([], use_bin_type=True))],
    )
    _wire_seq(h, [[create], [setfail], [listrep]])

    with caplog.at_level(logging.WARNING):
        assert h._raw.push_to_slot(-2, 5, "X", blob,
                               prechecked_empty=True) is None
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)
    text = caplog.text
    assert "X" in text and "5" in text
    assert "stale" in text.lower() or "no entry" in text.lower()


def test_create_content_status_returns_cid_and_code():
    """_create_content_status exposes (allocated_cid, code) so callers can
    recover the side-effect allocation even when code != 0."""
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    _wire(h, [reply])
    cid, code = h._create_content_status(-2, 5, "X")
    assert (cid, code) == (1237, 1)


def test_reference_into_setlist_returns_ref_cid(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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


def test_mirror_setlist_current_refs_listing_is_strict(monkeypatch):
    """#39 audit: mirror_setlist's own current-references read must be
    strict — a truncated/timed-out listing must raise rather than silently
    read as "this reference is gone", which would make the add-pass mint a
    SECOND reference to the same pool preset (a duplicate, the same failure
    class #39 fixed for setlist names)."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    _wire(h, [])  # the current-refs listing times out (zero reply frames)
    with pytest.raises(HelixError, match="no reply"):
        h.mirror_setlist(42, [200])


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
    h = HelixClient("10.0.0.99", reconnect_tries=3, reconnect_backoff=0.0)
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
    h = HelixClient("10.0.0.99", reconnect_tries=3, reconnect_backoff=0.0)
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
    h = HelixClient("10.0.0.99")
    _wire(h, [b"/GetContentRef no null terminator here"])
    reconnects = []
    h.reconnect = lambda: reconnects.append(1)
    with pytest.raises(HelixError):
        h.list_presets()
    assert reconnects == []


def test_reconnect_reopens_socket(monkeypatch):
    # reconnect() closes the old socket and re-runs _open_socket (no verify).
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    blob = _S.encode_value_blob("global.midi.channel", "i", 7)
    reply = osc_encode("/getPropertyValue",
                       [("i", 1000), ("s", "global.midi.channel"), ("b", blob)])
    _wire(h, [reply])
    pv = h.get_property("global.midi.channel")
    assert pv.key == "global.midi.channel" and pv.value == 7 and pv.type == "i"
    # request went out as /PropertyValueGet [reqid, key]
    assert h.sock.sent[0].startswith(b"/PropertyValueGet")


def test_get_property_def_parses_def_blob():
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/success", [("i", 1000), ("i", 0)])
    _wire(h, [reply])
    assert h.set_property("global.midi.channel", "i", 5) is True
    assert h.sock.sent[0].startswith(b"/PropertyValueSet")


def test_set_property_raises_on_error():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/error", [("i", 1000), ("i", 0), ("s", "NOPE")])
    _wire(h, [reply])
    with pytest.raises(HelixError):
        h.set_property("global.bad.key", "i", 1)


def test_get_property_raises_on_error():
    h = HelixClient("10.0.0.99")
    reply = osc_encode("/error", [("i", 1000), ("i", 0), ("s", "NOPE")])
    _wire(h, [reply])
    with pytest.raises(HelixError):
        h.get_property("global.bad.key")


def test_set_property_refuses_self_severing_key():
    h = HelixClient("10.0.0.99")
    # no socket wired — guard must fire BEFORE any RPC attempt (ValueError,
    # same type coerce_value raises, so the CLI set path surfaces it cleanly)
    with pytest.raises(ValueError):
        h.set_property("global.wifi.enable", "i", 0)


# -- library polish: IR delete + setlist create/delete/duplicate -------------

def test_delete_irs_removes_from_user_irs_container(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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


def test_create_setlist_nonzero_code_with_container_present_is_success(
        monkeypatch):
    # #38 root cause: a non-zero field 3 only means the edit buffer was dirty.
    # The setlist really was created, so create_setlist must confirm by
    # re-listing the root and return the cid — never delete what it just made.
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    # rpc 1000: list -5 for the next free posi (empty root -> posi 0)
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    # rpc 1001: /CreateContent -> non-zero code, but the container IS created
    create = osc_encode("/status", [("i", 1001), ("i", 1186), ("i", 1)])
    made = [{"cid_": 1186, "name": "ZZC-x", "cctp": 1001, "posi": 0}]
    # rpc 1002: the confirming re-list finds it; rpc 1003: resolve_setlist_cid
    list2 = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb(made, use_bin_type=True))])
    list3 = osc_encode(
        "/GetContainerContents",
        [("i", 1003), ("b", msgpack.packb(made, use_bin_type=True))])
    _wire_seq(h, [[list1], [create], [list2], [list3]])

    assert h.create_setlist("ZZC-x") == 1186
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_create_setlist_nonzero_code_absent_names_verify_verb(monkeypatch):
    # genuine failure: non-zero code AND the container never shows up in the
    # confirming re-list. The error must point the user at
    # `helixgen device setlists` (not `device list`)
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    create = osc_encode("/status", [("i", 1001), ("i", 1186), ("i", -47)])
    empty = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb([], use_bin_type=True))])
        for r in range(1002, 1008)]
    _wire_seq(h, [[list1], [create]] + [[f] for f in empty])

    with pytest.raises(HelixError) as ei:
        h.create_setlist("ZZC-x")
    msg = str(ei.value)
    assert "the device reported new cid 1186" in msg
    assert "helixgen device setlists" in msg


def test_create_setlist_confirms_when_no_status_frame(monkeypatch):
    # #38: a dropped /status frame says NOTHING about whether the create
    # landed, so it must be resolved by re-list like a non-zero code — not
    # reported as failure. Here the confirming listing DOES show the setlist,
    # so create_setlist returns its real cid instead of the historic None
    # (which made `setlist duplicate`'s auto-create abort on a setlist that was
    # really there, leaking it).
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    unrelated = osc_encode("/somethingelse", [("i", 1001), ("i", 0)])
    made = [{"name": "ZZC-x", "posi": 0, "cid_": 1186}]
    listed = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb(made, use_bin_type=True))])
        for r in range(1002, 1008)]
    _wire_seq(h, [[list1], [unrelated]] + [[f] for f in listed])

    assert h.create_setlist("ZZC-x") == 1186
    # nothing is ever deleted on this path
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_create_setlist_no_status_frame_and_absent_raises(monkeypatch):
    # the genuine failure half of the above: no /status AND the confirming
    # re-list never shows it. That still raises (rather than silently
    # returning None), and the message says no reply came back at all.
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    unrelated = osc_encode("/somethingelse", [("i", 1001), ("i", 0)])
    empty = [osc_encode(
        "/GetContainerContents",
        [("i", r), ("b", msgpack.packb([], use_bin_type=True))])
        for r in range(1002, 1008)]
    _wire_seq(h, [[list1], [unrelated]] + [[f] for f in empty])

    with pytest.raises(HelixError) as ei:
        h.create_setlist("ZZC-x")
    msg = str(ei.value)
    assert "sent no /status reply" in msg
    assert "helixgen device setlists" in msg
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_delete_setlist_removes_from_root(monkeypatch):
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    _wire(h, [])  # poller never fires -> _rpc returns []
    with pytest.raises(HelixError, match="no reply"):
        h.list_container(-2, strict=True)
    # non-strict keeps the legacy silent-empty behavior
    _wire(h, [])
    assert h.list_container(-2) == []


def test_list_container_strict_raises_on_undecodable_blob():
    """A truncated/undecodable listing blob (chunked-reply decode failure) must
    raise in strict mode instead of silently dropping items."""
    h = HelixClient("10.0.0.99")
    # 0xc1 is an invalid msgpack byte -> decode_blob returns raw bytes
    reply = osc_encode(
        "/GetContainerContents", [("i", 1000), ("b", b"\xc1garbage")])
    _wire(h, [reply])
    with pytest.raises(HelixError, match="undecodable"):
        h.list_container(-2, strict=True)
    _wire(h, [reply])
    assert h.list_container(-2) == []  # legacy behavior unchanged


def test_list_container_strict_accepts_genuine_empty_array():
    h = HelixClient("10.0.0.99")
    reply = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    _wire(h, [reply])
    assert h.list_container(-2, strict=True) == []


def test_list_presets_and_irs_pass_strict_through(monkeypatch):
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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


def test_create_setlist_relist_tolerates_transient_listing_timeout(monkeypatch):
    """#39: resolve_setlist_cid defaulted to strict, but create_setlist's
    post-create relist is a deliberate strict=False use — it already knows
    the device just accepted the create (status 0), so a transient timeout on
    one relist attempt must read the same as "not yet visible" (retry), not
    blow up the whole call with a HelixError."""
    _patch_sub(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda s: None)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    list1 = osc_encode(
        "/GetContainerContents",
        [("i", 1000), ("b", msgpack.packb([], use_bin_type=True))])
    create = osc_encode("/status", [("i", 1001), ("i", 930), ("i", 0)])
    after = osc_encode(
        "/GetContainerContents",
        [("i", 1003), ("b", msgpack.packb(
            [{"cid_": 1186, "name": "ZZC-x", "cctp": 1001, "posi": 0}],
            use_bin_type=True))])
    # first relist attempt (reqid 1002) times out entirely (zero reply
    # frames); second relist attempt (reqid 1003) succeeds.
    _wire_seq(h, [[list1], [create], [], [after]])
    assert h.create_setlist("ZZC-x") == 1186


def test_create_setlist_falls_back_to_reply_cid_with_warning(monkeypatch, caplog):
    import logging

    _patch_sub(monkeypatch)
    monkeypatch.setattr("time.sleep", lambda s: None)
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
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
    h = HelixClient("10.0.0.99")
    _wire(h, [])
    with pytest.raises(HelixError, match="getProductInfo"):
        h.product_info()


# --- #40: strict positional resolution (find_by_pos / _lowest_empty_posi) --

def test_find_by_pos_default_lenient_on_listing_timeout():
    """Legacy behavior preserved: strict=False (the default) reads a listing
    timeout the same as an empty container, so find_by_pos returns None
    rather than raising."""
    h = HelixClient("10.0.0.99")
    _wire(h, [])
    assert h.find_by_pos(-2, 3) is None


def test_find_by_pos_strict_raises_on_listing_timeout():
    """#40: a write-gating caller must pass strict=True so a listing timeout
    raises instead of silently reading as 'slot empty' — the exact failure
    class that could let a write land on a real occupant."""
    h = HelixClient("10.0.0.99")
    _wire(h, [])
    with pytest.raises(HelixError, match="no reply"):
        h.find_by_pos(-2, 3, strict=True)


def test_find_by_pos_strict_forwarded_to_list_container(monkeypatch):
    h = HelixClient("10.0.0.99")
    seen = []

    def fake_list(cid, *, strict=False):
        seen.append(strict)
        return []

    monkeypatch.setattr(h, "list_container", fake_list)
    h.find_by_pos(-2, 3, strict=True)
    h.find_by_pos(-2, 3)
    assert seen == [True, False]


def test_lowest_empty_posi_raises_on_listing_timeout():
    """#40: _lowest_empty_posi must not silently read a listing timeout as
    'container empty' — that would return posi 0 even when the container is
    full, and the caller would then /CreateContent into a real occupant."""
    h = HelixClient("10.0.0.99")
    _wire(h, [])
    with pytest.raises(HelixError, match="no reply"):
        h._lowest_empty_posi(-2)


def test_lowest_empty_posi_lists_strictly(monkeypatch):
    h = HelixClient("10.0.0.99")
    seen = []

    def fake_list(cid, *, strict=False):
        seen.append((cid, strict))
        return [{"posi": 0}, {"posi": 1}]

    monkeypatch.setattr(h, "list_container", fake_list)
    assert h._lowest_empty_posi(-2) == 2
    assert seen == [(-2, True)]


def test_install_into_pool_aborts_before_create_on_listing_failure(monkeypatch):
    """#40: install_into_pool with pos=None picks the target slot via
    _lowest_empty_posi. A strict listing failure there must raise BEFORE any
    /CreateContent is attempted — never write to a computed-but-unconfirmed
    position."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    _wire(h, [])  # the pool listing itself times out
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})

    with pytest.raises(HelixError, match="no reply"):
        h.install_into_pool(blob, "White Limo Lead")
    # nothing was ever sent to /CreateContent — abort-before-create
    assert not any(b"/CreateContent" in s for s in h.sock.sent)


def test_create_setlist_aborts_before_create_on_listing_failure(monkeypatch):
    """#40: create_setlist with pos=None picks the target slot via
    _lowest_empty_posi. A strict listing failure there must raise BEFORE any
    /CreateContent is attempted."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    _wire(h, [])  # the setlists-root listing itself times out

    with pytest.raises(HelixError, match="no reply"):
        h.create_setlist("ZZC-x")
    assert not any(b"/CreateContent" in s for s in h.sock.sent)


def test_install_into_pool_explicit_pos_skips_lowest_empty_posi(monkeypatch):
    """An explicit pos= bypasses _lowest_empty_posi entirely — a listing
    failure elsewhere must not block a caller that already knows its slot."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    name = "White Limo Lead"
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 0)])
    setdata = osc_encode("/status", [("i", 1001), ("i", 0), ("i", 0)])
    presets = [{"cid_": 777, "name": name, "cctp": 1000, "posi": 3}]
    listrep = osc_encode(
        "/GetContainerContents",
        [("i", 1002), ("b", msgpack.packb(presets, use_bin_type=True))])
    _wire_seq(h, [[create], [setdata], [listrep]])
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    assert h.install_into_pool(blob, name, pos=3) == 777


def test_reorder_container_fallback_listing_stays_lenient(monkeypatch):
    """#40 audit verdict: when SOME reply arrived (proving the device
    processed the request — /error and non-zero /status both raise first)
    but none of it was the /updateContainerContent confirmation, the post-write
    fallback re-list inside reorder_container is deliberately left non-strict
    — pure bookkeeping for the return value, not a write gate."""
    h = HelixClient("10.0.0.99")
    seen = []

    def fake_list(cid, *, strict=False):
        seen.append(strict)
        return []

    ok = osc_encode("/status", [("i", 1000), ("i", 0)])
    _wire(h, [ok])
    monkeypatch.setattr(h, "list_container", fake_list)
    items = h.reorder_container(-2, [5], 1)
    assert items == []
    assert seen == [False]


def test_reorder_container_raises_on_total_timeout():
    """#40 review finding: a TOTAL timeout (zero reply frames at all — no
    /error, no /status, no confirmation) must raise instead of silently
    re-listing and returning as if the reorder had been confirmed — nothing
    here indicates the device ever received/processed the request, unlike the
    'some reply, just not the confirmation frame' case above."""
    h = HelixClient("10.0.0.99")
    _wire(h, [])
    with pytest.raises(HelixError, match="no reply"):
        h.reorder_container(-2, [5], 1)


# -- IR listing vs. the lagging container index (#38 Task 4) ------------------


class _RecordingSub:
    """Subscriber fake that records the ports every open asked for."""

    opened: list = []

    def __init__(self, _ip, ports=()):
        _RecordingSub.opened.append(tuple(ports))

    def connect(self):
        return self

    def close(self):
        pass


def _patch_recording_sub(monkeypatch):
    from helixgen.device import subscribe as sub_mod
    _RecordingSub.opened = []
    monkeypatch.setattr(sub_mod, "HelixSubscriber", _RecordingSub)


def _ir_listing_frame(reqid=1000):
    irs = [{"cid_": 5, "name": "A", "hash": b"\x11" * 16, "mono": False,
            "posi": 0}]
    return osc_encode(
        "/GetContainerContents",
        [("i", reqid), ("b", msgpack.packb(irs, use_bin_type=True))])


def test_list_irs_reads_under_a_2001_subscription(monkeypatch):
    """The -11 container index lags a just-completed write unless a client is
    subscribed to the 2001 change stream, so the read that reported 24 IRs for
    minutes after an upload must open one first (#38 Task 4)."""
    _patch_recording_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    _wire(h, [_ir_listing_frame()])
    assert [m["hash"] for m in h.list_irs()] == ["11" * 16]
    assert _RecordingSub.opened == [(2001,)]


def test_list_irs_settle_false_skips_the_subscription(monkeypatch):
    """The settle is an escape-hatch-able cost: a caller already holding a
    2001 subscription (or one that wants a bare read) can opt out."""
    _patch_recording_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    _wire(h, [_ir_listing_frame()])
    assert len(h.list_irs(settle=False)) == 1
    assert _RecordingSub.opened == []


def test_ir_path_for_hash_strict_raises_on_a_dropped_reply(monkeypatch):
    """A timed-out lookup must not decode as 'the device doesn't have it'.
    _rpc returns [] on a plain timeout, so without strict= the caller cannot
    tell a dropped reply from a genuine absence (#40's contract, applied to
    the point lookup)."""
    h = HelixClient("10.0.0.99")
    _wire(h, [])  # no reply frames at all
    with pytest.raises(HelixError):
        h.ir_path_for_hash("bb" * 16, strict=True)
    # non-strict keeps the old lenient reading for callers that want it
    _wire(h, [])
    assert h.ir_path_for_hash("bb" * 16) is None


def test_ir_path_for_hash_strict_raises_on_a_malformed_reply(monkeypatch):
    """A reply that arrived but carried no decodable path is the *likelier*
    transport failure than no reply at all (truncated/undecodable frame), and
    it answers the question just as poorly. Under strict= it must raise too —
    if it collapses into the ``None`` that means 'not registered', the
    cross-check produces the false 'missing' it exists to prevent and the
    caller re-uploads an IR the device already has."""
    def _lookup(reply_args, **kwargs):
        """Fresh client per case — the request id increments per _rpc call, and
        _rpc only keeps frames whose first arg matches it."""
        h = HelixClient("10.0.0.99")
        _wire(h, [osc_encode("/xxxIrxPathForHash1",
                             [("i", 1000)] + list(reply_args))])
        return h.ir_path_for_hash("bb" * 16, **kwargs)

    # a frame arrives, but the path argument is missing / not a string
    with pytest.raises(HelixError):
        _lookup([], strict=True)
    with pytest.raises(HelixError):
        _lookup([("b", b"\x00\x01")], strict=True)
    # non-strict keeps the lenient reading for callers that want it
    assert _lookup([]) is None
    # a well-formed EMPTY path is a real answer ("not registered"), not a
    # transport failure — strict must still return None there
    assert _lookup([("s", "")], strict=True) is None


def test_device_ir_hashes_warns_when_the_lookup_is_dropped(monkeypatch, caplog):
    """The verify cross-check exists to survive a flaky transport, so a
    dropped lookup must warn and report the hash unverified rather than
    silently degrade to the false 'missing' it was added to prevent — a false
    missing re-uploads an IR the device already has."""
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_irs", lambda **_k: [{"hash": "aa" * 16}])
    seen = {}

    def _lookup(hh, **kw):
        seen["strict"] = kw.get("strict")
        raise HelixError("no reply")

    monkeypatch.setattr(h, "ir_path_for_hash", _lookup)
    with caplog.at_level(logging.WARNING):
        got = h.device_ir_hashes(verify=["bb" * 16])
    assert seen["strict"] is True, "the cross-check must use the strict lookup"
    assert got == {"aa" * 16}
    assert "unverified" in caplog.text


def test_device_ir_hashes_cross_checks_absent_hashes(monkeypatch, caplog):
    """A hash the -11 listing omits but /IrPathForHashGet resolves is PRESENT:
    the listing was stale, not authoritative. It must be reported (loudly),
    not silently accepted as absent."""
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_irs", lambda **_k: [{"hash": "aa" * 16}])
    monkeypatch.setattr(
        h, "ir_path_for_hash",
        lambda hh, **_k: "/data/x.wav" if hh == "bb" * 16 else None)
    with caplog.at_level(logging.WARNING):
        got = h.device_ir_hashes(verify=["bb" * 16, "cc" * 16])
    assert got == {"aa" * 16, "bb" * 16}  # cc genuinely absent
    assert "bb" * 16 in caplog.text and "stale" in caplog.text.lower()


def test_device_ir_hashes_without_verify_is_listing_only(monkeypatch):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_irs", lambda **_k: [{"hash": "aa" * 16}])

    def boom(_hh):  # pragma: no cover - must not be called
        raise AssertionError("no point lookup without verify=")

    monkeypatch.setattr(h, "ir_path_for_hash", boom)
    assert h.device_ir_hashes() == {"aa" * 16}


def test_check_irs_does_not_report_a_present_ir_as_missing(monkeypatch):
    """bridge.check_irs drives 'you must import this IR' advice; a lagging
    listing must not make an IR that IS on the device look missing."""
    from helixgen.device import bridge

    class _C:
        def list_irs(self, **_k):
            return [{"hash": "aa" * 16}]

        def ir_path_for_hash(self, hh, **_k):
            return "/data/y.wav" if hh == "bb" * 16 else None

        device_ir_hashes = HelixClient.device_ir_hashes

    body = {"preset": {"flow": [{"b0": {"slot": [{"irhash": "bb" * 16}]}}]}}
    res = bridge.check_irs(_C(), body)
    assert res["missing"] == set()
    assert res["present"] == {"bb" * 16}


# -- #38 review follow-ups: the confirm loop's own machinery ----------------

def _pool_listing(reqid, items):
    return osc_encode(
        "/GetContainerContents",
        [("i", reqid), ("b", msgpack.packb(items, use_bin_type=True))])


def test_confirm_created_recovers_when_the_first_listing_misses(monkeypatch):
    """The retry loop's whole reason to exist: the container index lags the
    write, so the FIRST confirming listing can legitimately miss the entry and
    a later one find it. Pinned by outcome (the create succeeds), not by a
    call count."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    miss = _pool_listing(1001, [])
    hit = _pool_listing(1002, [{"cid_": 777, "name": "X", "cctp": 1000,
                                "posi": 5}])
    setdata = osc_encode("/status", [("i", 1003), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [miss], [hit], [setdata]])

    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_confirm_created_retries_a_failed_listing(monkeypatch):
    """A listing that FAILS (timeout / truncated reply — strict, #40) is 'not
    visible yet', not 'absent': it must be retried, never raised and never
    read as an empty container. Without the strict read a dropped reply would
    decode as [] and a landed write would be declared missing."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    hit = _pool_listing(1002, [{"cid_": 777, "name": "X", "cctp": 1000,
                                "posi": 5}])
    setdata = osc_encode("/status", [("i", 1003), ("i", 0), ("i", 0)])
    # [] == zero reply frames == the strict timeout case
    _wire_seq(h, [[create], [], [hit], [setdata]])

    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777


def test_confirm_created_ignores_a_match_carrying_no_cid(monkeypatch):
    """A listing row matching (name, posi) but with no cid_ can't be returned
    as 'the created cid'. It counts as not-yet-visible and the loop keeps
    polling."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    cidless = _pool_listing(1001, [{"name": "X", "cctp": 1000, "posi": 5}])
    hit = _pool_listing(1002, [{"cid_": 777, "name": "X", "cctp": 1000,
                                "posi": 5}])
    setdata = osc_encode("/status", [("i", 1003), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [cidless], [hit], [setdata]])

    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777


def test_raw_create_content_confirms_a_landed_nonzero_create(monkeypatch):
    """The `_raw.create_content` escape hatch keeps the Optional[int] contract
    but must apply the SAME #38 resolution: a non-zero code whose content the
    re-list finds is a success returning the LISTED cid, not None."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    create = osc_encode("/status", [("i", 1000), ("i", 930), ("i", 1)])
    hit = _pool_listing(1001, [{"cid_": 777, "name": "x", "cctp": 1000,
                                "posi": 7}])
    _wire_seq(h, [[create], [hit]])

    assert h._raw.create_content(-2, 7, "x") == 777


def test_a_dropped_status_reply_is_confirmed_not_failed(monkeypatch):
    """A create whose /status frame never came back (code is None) says NOTHING
    about whether the content landed — the Stadium stack drops replies. It must
    go through the SAME confirming re-list as a non-zero code, not be reported
    as a silent failure for content that is really on the device (#38)."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    hit = _pool_listing(1001, [{"cid_": 777, "name": "X", "cctp": 1000,
                                "posi": 5}])
    setdata = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    # [] for the create == no /status frame at all
    _wire_seq(h, [[], [hit], [setdata]])

    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_a_dropped_status_reply_error_does_not_say_code_none(monkeypatch):
    """When the dropped-reply create really can't be confirmed, the error must
    name the missing reply rather than print 'status code None'."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    h.create_confirm_tries = 2
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    empty = [_pool_listing(r, []) for r in (1001, 1002)]
    _wire_seq(h, [[]] + [[f] for f in empty])

    with pytest.raises(HelixError) as exc:
        h._raw.push_to_slot(-2, 5, "X", blob)
    assert "no /status reply" in str(exc.value)
    assert "code None" not in str(exc.value)


def test_a_cidless_match_is_not_reported_as_never_listed(monkeypatch):
    """A listing that shows (name, posi) but never reports a cid means the
    content IS on the device and only its cid is unresolved. Telling the user
    the listing 'never showed it' would send them to re-create content that is
    already there — the duplicate-write half of #38."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    h.create_confirm_tries = 2
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    cidless = [_pool_listing(r, [{"name": "X", "cctp": 1000, "posi": 5}])
               for r in (1001, 1002)]
    _wire_seq(h, [[create]] + [[f] for f in cidless])

    with pytest.raises(HelixError) as exc:
        h._raw.push_to_slot(-2, 5, "X", blob)
    msg = str(exc.value)
    assert "never showed it" not in msg
    assert "never reported a cid" in msg
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_device_ir_hashes_reads_the_listing_strictly(monkeypatch):
    """#40: a dropped -11 reply must not decode as 'the device has no IRs'. If
    it did, every referenced hash would fall through to the point lookup — and
    a transport dropping the listing likely drops those too, so the caller
    would be told the preset's IRs are ALL missing and re-upload them."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    seen = {}

    def fake_list_irs(self, *, strict=False, settle=True):
        seen["strict"] = strict
        raise HelixError("listing timed out")

    monkeypatch.setattr(HelixClient, "list_irs", fake_list_irs)
    with pytest.raises(HelixError):
        h.device_ir_hashes(verify=["bb" * 16])
    assert seen["strict"] is True


def test_confirming_relist_runs_under_a_subscription(monkeypatch):
    """The confirming re-list is only prompt for a client holding a 2001
    subscription — the same reason list_irs settles. `device save`/`push`/
    `slots restore` reach _push_to_slot/_save_edit_buffer_to WITHOUT one of
    their own, so the write methods must open it themselves or the confirm
    polls a lagging index and declares a landed write missing."""
    _patch_recording_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    hit = _pool_listing(1001, [{"cid_": 777, "name": "X", "cctp": 1000,
                                "posi": 5}])
    setdata = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [hit], [setdata]])

    assert h._raw.push_to_slot(-2, 5, "X", blob) == 777
    assert _RecordingSub.opened, "no 2001 subscription held over the confirm"


def test_confirming_relist_requires_the_posi_to_match(monkeypatch):
    """The confirm matches name AND posi. A same-named preset sitting at a
    DIFFERENT slot is not our create: confirming on the name alone would return
    the incumbent's cid and _set_content_data would then overwrite a preset we
    never created — the same data loss #38 is about, just from the other
    direction. Every other confirm test wires the match at the requested posi,
    so this is the one that discriminates the field."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    h.create_confirm_tries = 2
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    # "X" exists, but at posi 9 — we asked for slot 5
    elsewhere = [_pool_listing(r, [{"cid_": 777, "name": "X", "cctp": 1000,
                                    "posi": 9}])
                 for r in range(1001, 1004)]
    _wire_seq(h, [[create]] + [[f] for f in elsewhere])

    with pytest.raises(HelixError):
        h._raw.push_to_slot(-2, 5, "X", blob)
    # and it must not have written content into the incumbent's cid
    assert not any(b"/SetContentData" in s for s in h.sock.sent)


def test_save_edit_buffer_confirming_relist_runs_under_a_subscription(monkeypatch):
    """Same guarantee on the `device save` path — the one the live suite's #38
    regression guard rides."""
    _patch_recording_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    hit = _pool_listing(1001, [{"cid_": 777, "name": "X", "cctp": 1000,
                                "posi": 5}])
    saved = osc_encode("/status", [("i", 1002), ("i", 0), ("i", 0)])
    _wire_seq(h, [[create], [hit], [saved]])

    assert h._raw.save_edit_buffer_to(-2, 5, "X") == 777
    assert _RecordingSub.opened, "no 2001 subscription held over the confirm"


# -- _delete_created_stub: every no-op must be REPORTED ---------------------

def test_delete_created_stub_reports_a_failed_listing(monkeypatch, caplog):
    """Silence is what let orphans accumulate: a listing that FAILED can't
    prove the stub is absent, so the no-op is warned about."""
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(
        h, "list_container",
        lambda *_a, **_k: (_ for _ in ()).throw(HelixError("timeout")))
    with caplog.at_level(logging.WARNING):
        assert h._delete_created_stub(-2, "X", 5) is None
    assert "the listing failed" in caplog.text
    assert "stub may be left behind" in caplog.text


def test_delete_created_stub_reports_a_match_with_no_cid(monkeypatch, caplog):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_container",
                        lambda *_a, **_k: [{"name": "X", "posi": 5}])
    with caplog.at_level(logging.WARNING):
        assert h._delete_created_stub(-2, "X", 5) is None
    assert "carried no cid" in caplog.text


def test_delete_created_stub_reports_a_refused_delete(monkeypatch, caplog):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_container",
                        lambda *_a, **_k: [{"cid_": 777, "name": "X", "posi": 5}])
    monkeypatch.setattr(h, "_delete", lambda *_a, **_k: False)
    with caplog.at_level(logging.WARNING):
        assert h._delete_created_stub(-2, "X", 5) is None
    assert "refused to delete cid 777" in caplog.text


def test_delete_created_stub_reports_a_raising_delete(monkeypatch, caplog):
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_container",
                        lambda *_a, **_k: [{"cid_": 777, "name": "X", "posi": 5}])
    monkeypatch.setattr(
        h, "_delete",
        lambda *_a, **_k: (_ for _ in ()).throw(HelixError("boom")))
    with caplog.at_level(logging.WARNING):
        assert h._delete_created_stub(-2, "X", 5) is None
    assert "the delete failed" in caplog.text


# -- device_ir_hashes: an unverifiable lookup must not pass silently --------

def test_device_ir_hashes_warns_when_the_point_lookup_fails(monkeypatch, caplog):
    """A point lookup that ERRORS leaves presence genuinely unknown. The hash
    stays reported missing (the safe direction — a re-upload is idempotent),
    but silently would be indistinguishable from a verified absence, so it is
    warned about."""
    h = HelixClient("10.0.0.99")
    monkeypatch.setattr(h, "list_irs", lambda **_k: [{"hash": "aa" * 16}])
    monkeypatch.setattr(
        h, "ir_path_for_hash",
        lambda _hh, **_k: (_ for _ in ()).throw(HelixError("timeout")))
    with caplog.at_level(logging.WARNING):
        got = h.device_ir_hashes(verify=["bb" * 16])
    assert got == {"aa" * 16}
    assert "bb" * 16 in caplog.text
    assert "unverified" in caplog.text


def test_create_error_says_unknown_when_no_listing_ever_succeeded(monkeypatch):
    """If EVERY confirming listing failed we never got a readable answer, so
    the error must NOT assert the content is absent. On the flaky Stadium
    stack a run of dropped listings would otherwise yield a confident wrong
    diagnosis for a write that landed — and 'retry' is the wrong advice,
    since a retry against content that IS there duplicates it."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    # every confirming listing times out (zero reply frames == strict failure)
    _wire_seq(h, [[create]] + [[] for _ in range(h.create_confirm_tries)])

    with pytest.raises(HelixError) as ei:
        h._raw.push_to_slot(-2, 5, "X", blob)
    msg = str(ei.value)
    assert "UNKNOWN" in msg
    assert "could not be read" in msg
    assert "may duplicate" in msg
    # and it must NOT claim the listing showed the content absent
    assert "never showed it" not in msg
    # nothing was deleted on the way out
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)


def test_create_error_says_unknown_when_only_an_early_listing_succeeded(
        monkeypatch):
    """A clean-but-empty read EARLY in the confirm loop is the expected shape of
    a container index still lagging a just-completed write — it is not evidence
    of absence. If the later attempts (including the last) all failed to read,
    the newest answer we have is 'unreadable', so the error must stay UNKNOWN.

    Letting that early read latch a sticky 'listed cleanly' would reinstate the
    exact #38 failure mode this change exists to remove: a confident 'the create
    really did not land' for content that IS on the device, whose 'safe to
    retry' advice duplicates it."""
    _patch_sub(monkeypatch)
    h = HelixClient("10.0.0.99")
    h.mutate_settle = 0
    h.create_confirm_delay = 0
    from helixgen.device import content as C
    blob = C.encode_content({"cg__": {}, "hist": 1, "pm__": [], "sfg_": {}})
    create = osc_encode("/status", [("i", 1000), ("i", 1237), ("i", 1)])
    # first confirming listing reads cleanly but the index has not caught up
    # yet (empty container); every later attempt drops.
    empty_listing = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb([], use_bin_type=True))])
    _wire_seq(h, [[create], [empty_listing]]
              + [[] for _ in range(h.create_confirm_tries - 1)])

    with pytest.raises(HelixError) as ei:
        h._raw.push_to_slot(-2, 5, "X", blob)
    msg = str(ei.value)
    assert "UNKNOWN" in msg
    assert "may duplicate" in msg
    # the one early clean read must NOT license the confident absence claim
    assert "really did not land" not in msg
    assert "never showed it" not in msg
    assert not any(b"/RemoveContent" in s for s in h.sock.sent)
