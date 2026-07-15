"""`device reorder` — /ReorderContainerContent wire shape + name resolution.

Arg shape decoded 2026-07-14 (see
docs/superpowers/specs/2026-07-14-parity-capture-findings.md §1/§9):
``/ReorderContainerContent [cmd, containerCID, msgpack[movedCIDs], newPos]``.

Three layers are tested:
- ``HelixClient.reorder_container`` — the wire primitive (FakeSock/FakePoller,
  same pattern as ``test_device_client.py``/``test_device_liveops.py``).
- ``reorder.resolve_target_cid`` — pure name resolution against plain listings.
- ``reorder.reorder_setlist_item`` — the device-driving orchestrator, tested
  against a tiny stub client (no real socket).
"""
from __future__ import annotations

import pytest

msgpack = pytest.importorskip("msgpack")

from helixgen.device.client import (  # noqa: E402
    Cctp, Container, HelixClient, HelixError)
from helixgen.device.osc import osc_encode, parse_osc_message  # noqa: E402
from helixgen.device import reorder as R  # noqa: E402


# ---------------------------------------------------------------------------
# HelixClient.reorder_container — wire shape
# ---------------------------------------------------------------------------

class FakePoller:
    def __init__(self, frames):
        self._remaining = len(frames)

    def register(self, *_a, **_k):
        pass

    def poll(self, _ms):
        if self._remaining > 0:
            self._remaining -= 1
            return [("sock", 1)]
        return []


class FakeSock:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self):
        return self._frames.pop(0)

    def close(self):
        pass


def _wire(client, frames):
    client.sock = FakeSock(frames)
    client.poller = FakePoller(frames)


class SeqPoller:
    """Poller for a multi-RPC flow: returns truthy once per frame in the
    current group, then one empty poll (ending that rpc) before advancing to
    the next group. Mirrors ``test_device_client.py``'s helper of the same
    shape."""

    def __init__(self, groups):
        self._groups = [list(g) for g in groups]
        self._i = 0
        self._pos = 0

    def register(self, *_a, **_k):
        pass

    def poll(self, _timeout_ms):
        if self._i >= len(self._groups):
            return []
        grp = self._groups[self._i]
        if self._pos < len(grp):
            self._pos += 1
            return [("sock", 1)]
        self._i += 1
        self._pos = 0
        return []


class SeqSock:
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


def _last_sent(h):
    raw = h.sock.sent[-1]
    addr, args, _ = parse_osc_message(raw, raw.find(b"/"))
    return addr, [v for _t, v in args]


def test_reorder_container_wire_shape():
    h = HelixClient()
    reply = osc_encode(
        "/updateContainerContent",
        [("i", 1000), ("b", msgpack.packb([]))])
    _wire(h, [reply])
    h.reorder_container(-2, [1206], 5)
    addr, args = _last_sent(h)
    assert addr == "/ReorderContainerContent"
    # [reqid, container, movedCIDs(blob), newPos]
    assert args[0] == 1000
    assert args[1] == -2
    assert msgpack.unpackb(args[2], raw=False) == [1206]
    assert args[3] == 5


def test_reorder_container_parses_update_reply():
    h = HelixClient()
    listing = [{"cid_": 10, "posi": 0, "cctp": 1003, "rcid": 900},
               {"cid_": 11, "posi": 1, "cctp": 1003, "rcid": 901}]
    reply = osc_encode(
        "/updateContainerContent",
        [("i", 1000), ("b", msgpack.packb(listing))])
    _wire(h, [reply])
    items = h.reorder_container(500, [11], 0)
    assert [m["cid_"] for m in items] == [10, 11]


def test_reorder_container_empty_update_reply_is_not_a_fallback_trigger():
    """An /updateContainerContent reply that is OBSERVED but legitimately
    empty must not be confused with "no reply seen" — only one RPC call
    (the reorder itself) should be sent."""
    h = HelixClient()
    reply = osc_encode(
        "/updateContainerContent",
        [("i", 1000), ("b", msgpack.packb([]))])
    _wire(h, [reply])
    items = h.reorder_container(-2, [1206], 0)
    assert items == []
    assert len(h.sock.sent) == 1  # no fallback re-list call


def test_reorder_container_falls_back_to_relist_when_no_update_reply():
    h = HelixClient()
    # rpc 1000: /ReorderContainerContent -> bare /status, no listing
    reorder_reply = osc_encode("/status", [("i", 1000), ("i", 0), ("i", 0)])
    # rpc 1001: fallback /GetContainerContents
    listing = [{"cid_": 20, "posi": 0, "cctp": 1003, "rcid": 700}]
    relist_reply = osc_encode(
        "/GetContainerContents",
        [("i", 1001), ("b", msgpack.packb(listing))])
    _wire_seq(h, [[reorder_reply], [relist_reply]])
    items = h.reorder_container(500, [20], 0)
    assert items == listing


def test_reorder_container_error_reply_raises():
    """A device /error must raise, not silently fall back to a re-list of the
    unchanged container (which would read as a false success)."""
    h = HelixClient()
    reply = osc_encode("/error", [("i", 1000), ("i", 0), ("s", "NOPE")])
    _wire(h, [reply])
    with pytest.raises(HelixError, match="rejected"):
        h.reorder_container(-2, [1206], 5)


def test_reorder_container_failing_status_raises():
    """A /status whose code field (args[1], the _ok convention) is non-zero is
    a refusal, not a confirmation-on-another-channel."""
    h = HelixClient()
    reply = osc_encode("/status", [("i", 1000), ("i", -21), ("i", 0)])
    _wire(h, [reply])
    with pytest.raises(HelixError, match="refused"):
        h.reorder_container(-2, [1206], 5)


# ---------------------------------------------------------------------------
# reorder.resolve_target_cid — pure name resolution
# ---------------------------------------------------------------------------

SETLIST_ITEMS = [
    {"cid_": 501, "posi": 0, "cctp": Cctp.REFERENCE, "rcid": 100},
    {"cid_": 502, "posi": 1, "cctp": Cctp.REFERENCE, "rcid": 101},
    {"cid_": 503, "posi": 2, "cctp": Cctp.REFERENCE, "rcid": 102},
]
POOL_NAMES = {100: "Clean Machine", 101: "Lead Tone", 102: "Lead Tone (v2)"}

ROOT_ITEMS = [
    {"cid_": 900, "posi": 0, "cctp": Cctp.SETLIST, "name": "user"},
    {"cid_": 988, "posi": 1, "cctp": Cctp.SETLIST, "name": "helixgen"},
    {"cid_": 1014, "posi": 2, "cctp": Cctp.SETLIST, "name": "Mike"},
]


def test_resolve_target_cid_literal_digit_bypasses_lookup():
    assert R.resolve_target_cid(SETLIST_ITEMS, "12345",
                                is_setlists_root=False) == 12345
    assert R.resolve_target_cid(SETLIST_ITEMS, "-7",
                                is_setlists_root=False) == -7


def test_resolve_target_cid_malformed_int_literal_resolves_by_name():
    """"--5" is not a well-formed int literal — it must fall through to name
    resolution (a clean "no ... named" error), never a raw int() ValueError."""
    with pytest.raises(ValueError, match="no preset named '--5'"):
        R.resolve_target_cid(SETLIST_ITEMS, "--5",
                             is_setlists_root=False, pool_names=POOL_NAMES)


def test_resolve_target_cid_numeric_name_no_such_cid_raises():
    """Case (ii): a digit target matching only a DISPLAY NAME (no item
    carries the literal cid) can only be a mistake — raise, pointing at the
    named item's real cid."""
    pool = dict(POOL_NAMES)
    pool[101] = "7"  # ref cid 502 is display-named "7"; no item has cid 7
    with pytest.raises(ValueError,
                       match="no item in this container has cid 7.*NAMED '7'.*502"):
        R.resolve_target_cid(SETLIST_ITEMS, "7",
                             is_setlists_root=False, pool_names=pool)


def test_resolve_target_cid_cid_present_wins_over_name_with_warning():
    """Case (i): when the literal cid IS present AND another item is NAMED
    that digit string, the cid reading wins (cid-first — raising would make
    the cid-carrying item unaddressable) and a warning names the shadowed
    item."""
    pool = dict(POOL_NAMES)
    pool[101] = "501"  # ref cid 502 is display-named "501"; cid 501 exists
    warnings: list = []
    got = R.resolve_target_cid(SETLIST_ITEMS, "501", is_setlists_root=False,
                               pool_names=pool, warnings=warnings)
    assert got == 501
    assert len(warnings) == 1 and "502" in warnings[0] and "cid-first" in warnings[0]


def test_resolve_target_cid_numeric_name_same_cid_is_not_a_collision():
    """An item named exactly its own cid string resolves cleanly — both
    readings point at the same item; no warning."""
    pool = dict(POOL_NAMES)
    pool[101] = "502"  # ref cid 502 is display-named "502"
    warnings: list = []
    assert R.resolve_target_cid(SETLIST_ITEMS, "502",
                                is_setlists_root=False,
                                pool_names=pool, warnings=warnings) == 502
    assert warnings == []


def test_resolve_target_cid_numeric_setlist_name_no_such_cid_raises():
    root = ROOT_ITEMS + [
        {"cid_": 1200, "posi": 3, "cctp": Cctp.SETLIST, "name": "816"}]
    with pytest.raises(ValueError,
                       match="no item in this container has cid 816.*NAMED '816'.*1200"):
        R.resolve_target_cid(root, "816", is_setlists_root=True)


def test_resolve_target_cid_setlist_cid_present_wins_with_warning():
    root = ROOT_ITEMS + [
        {"cid_": 1200, "posi": 3, "cctp": Cctp.SETLIST, "name": "988"}]
    warnings: list = []
    got = R.resolve_target_cid(root, "988", is_setlists_root=True,
                               warnings=warnings)
    assert got == 988  # the real cid-988 setlist, not the one named "988"
    assert len(warnings) == 1 and "1200" in warnings[0]


POOL_ITEMS = [
    {"cid_": 1085, "posi": 0, "cctp": Cctp.PRESET,
     "name": "Always With Me Always With You - Satriani"},
    {"cid_": 1087, "posi": 2, "cctp": Cctp.PRESET, "name": "Back In Black"},
]


def test_resolve_target_cid_pool_presets_match_by_own_name():
    """The pool (-2) holds cctp==PRESET entries named directly — name
    resolution must work against it, not just against setlist references."""
    cid = R.resolve_target_cid(POOL_ITEMS, "Back In Black",
                               is_setlists_root=False)
    assert cid == 1087


def test_resolve_target_cid_pool_no_match_lists_preset_names():
    with pytest.raises(ValueError, match="no preset named 'Nope'"):
        R.resolve_target_cid(POOL_ITEMS, "Nope", is_setlists_root=False)


def test_resolve_target_cid_by_preset_name_via_rcid_join():
    cid = R.resolve_target_cid(SETLIST_ITEMS, "Clean Machine",
                               is_setlists_root=False, pool_names=POOL_NAMES)
    assert cid == 501


def test_resolve_target_cid_case_insensitive():
    cid = R.resolve_target_cid(SETLIST_ITEMS, "clean machine",
                               is_setlists_root=False, pool_names=POOL_NAMES)
    assert cid == 501


def test_resolve_target_cid_no_match_raises_with_available_names():
    with pytest.raises(ValueError, match="no preset named"):
        R.resolve_target_cid(SETLIST_ITEMS, "Nope",
                             is_setlists_root=False, pool_names=POOL_NAMES)


def test_resolve_target_cid_ambiguous_raises():
    dup_pool = dict(POOL_NAMES)
    dup_pool[102] = "Lead Tone"  # now two refs share the display name
    with pytest.raises(ValueError, match="ambiguous preset"):
        R.resolve_target_cid(SETLIST_ITEMS, "Lead Tone",
                             is_setlists_root=False, pool_names=dup_pool)


def test_resolve_target_cid_setlists_root_by_name():
    cid = R.resolve_target_cid(ROOT_ITEMS, "helixgen", is_setlists_root=True)
    assert cid == 988


def test_resolve_target_cid_setlists_root_no_match():
    with pytest.raises(ValueError, match="no setlist named"):
        R.resolve_target_cid(ROOT_ITEMS, "nope", is_setlists_root=True)


# ---------------------------------------------------------------------------
# reorder.reorder_setlist_item — device-driving orchestration (stub client)
# ---------------------------------------------------------------------------

class StubClient:
    """Minimal stand-in exposing exactly the surface reorder_setlist_item
    calls — no socket, no HelixClient machinery."""

    def __init__(self, *, setlists=None, container_items=None, pool=None):
        self.setlists = setlists or {}
        self.container_items = container_items or {}
        self.pool = pool or []
        self.reorder_calls = []

    def resolve_setlist_cid(self, name, *, strict=True):
        return self.setlists.get(name)

    def list_setlists(self, *, strict=False):
        return [{"cid_": cid, "name": name, "cctp": Cctp.SETLIST}
                for name, cid in self.setlists.items()]

    def list_setlists_by_name(self, name, *, strict=True, setlists=None):
        want = name.strip().casefold()
        source = self.list_setlists(strict=strict) if setlists is None else setlists
        return [m for m in source
                if str(m.get("name", "")).strip().casefold() == want]

    def list_container(self, cid, *, strict=False):
        return self.container_items.get(cid, [])

    def list_presets(self, container=Container.POOL, *, strict=False):
        return self.pool

    def reorder_container(self, container, moved_cids, new_pos):
        self.reorder_calls.append((container, list(moved_cids), new_pos))
        return [{"cid_": c, "posi": i} for i, c in enumerate(moved_cids)]


def test_reorder_setlist_item_named_setlist_by_preset_name():
    client = StubClient(
        setlists={"throwaway": 1234},
        container_items={1234: SETLIST_ITEMS},
        pool=[{"cid_": cid, "name": name} for cid, name in POOL_NAMES.items()],
    )
    res = R.reorder_setlist_item(client, "throwaway", "Lead Tone", 0)
    assert res["ok"] is True
    assert res["container"] == 1234
    assert res["moved_cid"] == 502  # rcid 101 == "Lead Tone"
    assert res["new_pos"] == 0
    assert client.reorder_calls == [(1234, [502], 0)]


def test_reorder_setlist_item_by_literal_cid():
    client = StubClient(
        setlists={"throwaway": 1234},
        container_items={1234: SETLIST_ITEMS},
        pool=[],
    )
    res = R.reorder_setlist_item(client, "throwaway", "503", 1)
    assert res["moved_cid"] == 503
    assert client.reorder_calls == [(1234, [503], 1)]


def test_reorder_setlist_item_unknown_setlist_raises_helpful_error():
    client = StubClient(setlists={})
    with pytest.raises(ValueError, match="no setlist named 'ghost'"):
        R.reorder_setlist_item(client, "ghost", "x", 0)
    assert client.reorder_calls == []


def test_reorder_setlist_item_setlists_keyword_reorders_root():
    client = StubClient(
        container_items={int(Container.SETLISTS_ROOT): ROOT_ITEMS},
    )
    res = R.reorder_setlist_item(client, "setlists", "Mike", 0)
    assert res["container"] == int(Container.SETLISTS_ROOT)
    assert res["moved_cid"] == 1014
    assert client.reorder_calls == [(int(Container.SETLISTS_ROOT), [1014], 0)]


def test_reorder_setlist_item_setlists_keyword_case_insensitive():
    client = StubClient(container_items={int(Container.SETLISTS_ROOT): ROOT_ITEMS})
    res = R.reorder_setlist_item(client, "SetLists", "988", 2)
    assert res["moved_cid"] == 988


def test_reorder_setlist_item_numeric_setlist_name_collision_warns():
    """Setlist-side case (i): a numeric <setlist> whose digit string is also
    a setlist's DISPLAY NAME, while a setlist with that cid exists — the cid
    reading wins with a warning naming the shadowed setlist."""
    client = StubClient(
        setlists={"7": 999, "other": 7},   # one NAMED "7", one WITH cid 7
        container_items={7: SETLIST_ITEMS},
        pool=[{"cid_": cid, "name": name} for cid, name in POOL_NAMES.items()],
    )
    res = R.reorder_setlist_item(client, "7", "Lead Tone", 0)
    assert res["container"] == 7
    assert res["moved_cid"] == 502
    assert any("999" in w and "cid-first" in w for w in res["warnings"])


def test_reorder_setlist_item_numeric_setlist_name_no_such_cid_raises():
    """Setlist-side case (ii): the digit string names a setlist but no
    container has that cid — raise with the named setlist's real cid."""
    client = StubClient(
        setlists={"7": 999},  # a setlist NAMED "7"; nothing has cid 7
        container_items={7: SETLIST_ITEMS},
    )
    with pytest.raises(ValueError,
                       match="no setlist container has cid 7.*NAMED '7'.*999"):
        R.reorder_setlist_item(client, "7", "x", 0)
    assert client.reorder_calls == []


def test_reorder_setlist_item_pool_cid_never_shadowed():
    """-2 (the pool) is always a valid container cid, even if a setlist is
    perversely named "-2" — case (i) applies (warn, use the pool)."""
    client = StubClient(
        setlists={"-2": 999},
        container_items={-2: POOL_ITEMS},
    )
    res = R.reorder_setlist_item(client, "-2", "Back In Black", 0)
    assert res["container"] == -2
    assert any("999" in w for w in res["warnings"])


def test_reorder_setlist_item_numeric_setlist_is_container_cid():
    """A literal-integer <setlist> addresses the container by cid directly —
    the escape hatch for a real setlist named "setlists" that the keyword
    shadows. No resolve_setlist_cid lookup happens."""
    client = StubClient(container_items={1234: SETLIST_ITEMS},
                        pool=[{"cid_": cid, "name": name}
                              for cid, name in POOL_NAMES.items()])
    res = R.reorder_setlist_item(client, "1234", "Lead Tone", 0)
    assert res["container"] == 1234
    assert res["moved_cid"] == 502
    assert client.reorder_calls == [(1234, [502], 0)]


@pytest.mark.parametrize("bad_to", [-1, 3, 99])
def test_reorder_setlist_item_bounds_validates_to_index(bad_to):
    """Negative or past-the-end --to is refused before touching the device
    (out-of-range newPos behavior on the wire is uncharacterized).
    SETLIST_ITEMS has 3 entries → valid positions are 0..2."""
    client = StubClient(
        setlists={"throwaway": 1234},
        container_items={1234: SETLIST_ITEMS},
        pool=[{"cid_": cid, "name": name} for cid, name in POOL_NAMES.items()],
    )
    with pytest.raises(ValueError, match="out of range"):
        R.reorder_setlist_item(client, "throwaway", "Lead Tone", bad_to)
    assert client.reorder_calls == []


def test_reorder_setlist_item_to_index_edges_ok():
    """0 and len(items)-1 are both valid destinations."""
    client = StubClient(
        setlists={"throwaway": 1234},
        container_items={1234: SETLIST_ITEMS},
        pool=[{"cid_": cid, "name": name} for cid, name in POOL_NAMES.items()],
    )
    R.reorder_setlist_item(client, "throwaway", "Lead Tone", 0)
    R.reorder_setlist_item(client, "throwaway", "Lead Tone", 2)
    assert [c[2] for c in client.reorder_calls] == [0, 2]


def test_reorder_setlist_item_pool_container_by_name():
    """Passing -2 as the container reorders the pool itself; targets resolve
    by the presets' own names (cctp==PRESET)."""
    client = StubClient(container_items={-2: POOL_ITEMS})
    res = R.reorder_setlist_item(client, "-2", "Back In Black", 0)
    assert res["container"] == -2
    assert res["moved_cid"] == 1087
    assert client.reorder_calls == [(-2, [1087], 0)]


def test_reorder_setlist_item_literal_cid_must_be_in_container():
    """The device silently no-ops a reorder of a cid that isn't in the
    container (/status success, order unchanged — live-observed), so the
    orchestrator validates membership and errors instead."""
    client = StubClient(
        setlists={"throwaway": 1234},
        container_items={1234: SETLIST_ITEMS},
        pool=[],
    )
    with pytest.raises(ValueError, match="cid 99999 is not in container 1234"):
        R.reorder_setlist_item(client, "throwaway", "99999", 0)
    assert client.reorder_calls == []


def test_reorder_setlist_item_numeric_root_cid_behaves_as_root():
    """Passing -5 as <setlist> is the setlists root — items resolve as
    setlists (by their own name), not as preset references."""
    client = StubClient(
        container_items={int(Container.SETLISTS_ROOT): ROOT_ITEMS})
    res = R.reorder_setlist_item(client, "-5", "Mike", 0)
    assert res["container"] == int(Container.SETLISTS_ROOT)
    assert res["moved_cid"] == 1014


# -- #39 audit: reorder's listings must gate the write strictly --------------

class StrictCheckingStubClient(StubClient):
    """Records the ``strict`` kwarg every listing call was made with, so the
    tests can assert reorder.py actually asks for strict listings (rather
    than just happening to pass because the fake ignores the kwarg)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.strict_seen = []

    def list_setlists(self, *, strict=False):
        self.strict_seen.append(("list_setlists", strict))
        return super().list_setlists()

    def list_container(self, cid, *, strict=False):
        self.strict_seen.append(("list_container", strict))
        return super().list_container(cid)

    def list_presets(self, container=Container.POOL, *, strict=False):
        self.strict_seen.append(("list_presets", strict))
        return super().list_presets(container)


def test_reorder_setlist_item_container_listing_is_strict():
    client = StrictCheckingStubClient(
        setlists={"throwaway": 1234},
        container_items={1234: SETLIST_ITEMS},
        pool=[{"cid_": cid, "name": name} for cid, name in POOL_NAMES.items()],
    )
    R.reorder_setlist_item(client, "throwaway", "Lead Tone", 0)
    assert ("list_container", True) in client.strict_seen
    assert ("list_presets", True) in client.strict_seen


def test_reorder_setlist_item_numeric_setlist_collision_listing_is_strict():
    client = StrictCheckingStubClient(
        setlists={"7": 999, "other": 7},
        container_items={7: SETLIST_ITEMS},
        pool=[{"cid_": cid, "name": name} for cid, name in POOL_NAMES.items()],
    )
    R.reorder_setlist_item(client, "7", "Lead Tone", 0)
    assert ("list_setlists", True) in client.strict_seen


def test_reorder_setlist_item_propagates_listing_failure_not_wrong_error():
    """A HelixError from a truncated/undecodable container listing must
    propagate as-is (#39 audit) — never get swallowed and misreported as
    "no item found" or a silent wrong-container reorder."""
    from helixgen.device.client import HelixError

    class RaisingClient(StubClient):
        def list_container(self, cid, *, strict=False):
            raise HelixError("undecodable listing blob for container "
                             f"{cid} (truncated chunked reply?)")

    client = RaisingClient(setlists={"throwaway": 1234})
    with pytest.raises(HelixError, match="undecodable"):
        R.reorder_setlist_item(client, "throwaway", "Lead Tone", 0)
    assert client.reorder_calls == []
