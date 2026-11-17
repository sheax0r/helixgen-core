from __future__ import annotations

from collections import namedtuple

from helixgen.liveedit.diffcache import DiffCache
from helixgen.liveedit.drawer import LibraryDrawer
from helixgen.liveedit.events import decode_content_event

Ev = namedtuple("Ev", ["port", "addr", "args", "seq"])


def _ev(addr, args):
    return Ev(2001, addr, args, 0)


def _drawer(tmp_path, cache=None):
    return LibraryDrawer(tmp_path, None, client_factory=None, cache=cache,
                         tone_loader=lambda: [], guitar_loader=lambda: [],
                         ir_local_loader=lambda: [])


def test_saveContentRef_invalidates_and_reverifies(tmp_path):
    (tmp_path / "tones").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    cache = DiffCache(tmp_path / "c.json")
    d = _drawer(tmp_path, cache)
    d._session.public_state = lambda: {
        "connected": True, "setlists": [], "irs": [], "facets": {},
        "tones": [{"variants": [{"presence": "both", "device_cid": 5,
                                 "hsp": "tones/a.hsp"}]}]}
    cache.put("5", {"hsp_hash": d._hsp_hash("tones/a.hsp"),
                    "verdict": "same", "ts": 0})
    d._confirmed.add(5)  # confirmed this session -> badge shows the verdict
    assert d.public_state()["tones"][0]["variants"][0]["diff"] == "same"

    ce = decode_content_event(_ev("/saveContentRef", [65295, 1014, 5]))
    assert ce.kind == "save" and ce.cid == 5
    d.apply_event(ce)
    assert cache.get("5") is None
    assert d.public_state()["tones"][0]["variants"][0]["diff"] == "verifying"


def test_updateContainerContent_replaces_membership(tmp_path):
    d = _drawer(tmp_path)
    members = [{"rcid": 2088, "posi": 0, "name": "X"}]
    ce = decode_content_event(
        _ev("/updateContainerContent", [69391, 1020, 2089, members]))
    delta = d.apply_event(ce)
    assert delta == {"kind": "setlist_members", "setlist_cid": 2089,
                     "members": members}


def test_self_initiated_event_is_idempotent(tmp_path):
    d = _drawer(tmp_path)
    row = {"cid_": 9, "ccid": -2, "name": "New", "posi": 3}
    ce = decode_content_event(_ev("/addContent", [3855, 1006, row]))
    r1 = d.apply_event(ce)
    r2 = d.apply_event(ce)
    assert r1 == {"kind": "add", "row": row}
    assert r2 is None
    assert d._known == {9}
