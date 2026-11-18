from __future__ import annotations

from helixgen.liveedit.drawer import LibraryDrawer
from helixgen.liveedit.diffcache import DiffCache
from helixgen.tone_meta import ToneMeta, Variant


def _onboth_state():
    return {"connected": True, "setlists": [], "irs": [], "facets": {},
            "tones": [{"slug": "x", "name": "X", "presence": "both",
                       "variants": [{"key": "k", "preset_name": "X",
                                     "hsp": "tones/a.hsp", "available": True,
                                     "presence": "both", "device_cid": 5}]}]}


def _meta(tmp_path):
    hsp = tmp_path / "tones" / "a.hsp"
    hsp.parent.mkdir(parents=True, exist_ok=True)
    hsp.write_text("{}")
    return ToneMeta(
        artist=None, song=None, descriptor="Crunch", tags=[],
        description_md=None,
        variants={"ibanez-prestige": Variant(hsp="tones/a.hsp",
                                              preset_name="Crunch - Prestige")},
        created="2026-01-01", updated="2026-01-01")


def _kw(tmp_path):
    return dict(tone_loader=lambda: [_meta(tmp_path)],
                guitar_loader=lambda: [], ir_local_loader=lambda: [])


def test_drawer_public_state_offline(tmp_path):
    d = LibraryDrawer(tmp_path, None, client_factory=None, cache=None,
                      **_kw(tmp_path))
    st = d.public_state()
    assert st["connected"] is False
    variants = [v for t in st["tones"] for v in t["variants"]]
    assert variants and variants[0]["presence"] == "computer"


def test_drawer_action_dispatch(tmp_path):
    _meta(tmp_path)  # writes tones/a.hsp
    closed, seen = [], {}

    class Client:
        def close(self):
            closed.append(1)

    client = Client()

    def copy_fn(h, hsp_path, *, setlist, pos):
        seen.update(h=h, hsp=str(hsp_path), setlist=setlist)
        return {"ok": True}

    d = LibraryDrawer(tmp_path, None, client_factory=lambda: client,
                      cache=None, copy_fn=copy_fn, **_kw(tmp_path))
    res = d.action("copy", {"hsp": "tones/a.hsp", "to": "Rock"})
    assert res == {"ok": True}
    assert seen["h"] is client and seen["setlist"] == "Rock"
    assert not closed


def test_drawer_ir_usage_and_remove_dispatch(tmp_path):
    _meta(tmp_path)

    class Client:
        def close(self):
            pass

    usage = {"hash": "aaa",
             "tones": [{"cid": 9, "name": "T", "setlists": []}],
             "indeterminate": []}
    seen = {}
    d = LibraryDrawer(
        tmp_path, None, client_factory=lambda: Client(), cache=None,
        usage_fn=lambda h, nh, ci: dict(usage),
        remove_everywhere_fn=lambda h, cid: seen.update(cid=cid)
        or {"ok": True, "removed_pool": cid},
        **_kw(tmp_path))
    assert d.action("ir_usage", {"name_or_hash": "aaa"}) == usage
    assert d.action("remove_tone_device", {"cid": 9})["ok"] is True
    assert seen["cid"] == 9


def test_drawer_delete_ir_guard_and_force(tmp_path):
    import pytest
    from helixgen.libraryui.session import LibraryError

    _meta(tmp_path)

    class Client:
        def close(self):
            pass

    calls = []
    d = LibraryDrawer(
        tmp_path, None, client_factory=lambda: Client(), cache=None,
        usage_fn=lambda h, nh, ci: {"hash": nh, "indeterminate": [],
                                    "tones": [{"cid": 1, "name": "a",
                                               "setlists": []}]},
        delete_ir_fn=lambda h, nh: calls.append(nh) or {"ok": True},
        **_kw(tmp_path))
    with pytest.raises(LibraryError):
        d.action("delete_ir", {"name_or_hash": "aaa"})
    assert not calls
    d.action("delete_ir", {"name_or_hash": "aaa", "force": True})
    assert calls == ["aaa"]


def test_public_state_badges_provisional(tmp_path):
    (tmp_path / "tones").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    cache = DiffCache(tmp_path / "c.json")
    d = LibraryDrawer(tmp_path, None, client_factory=None, cache=cache,
                      **_kw(tmp_path))
    d._session.public_state = _onboth_state
    v = d.public_state()["tones"][0]["variants"][0]
    assert v["diff"] == "verifying"


def test_resolve_diff_caches(tmp_path):
    (tmp_path / "tones").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    cache = DiffCache(tmp_path / "c.json")
    d = LibraryDrawer(tmp_path, None, client_factory=None, cache=cache,
                      **_kw(tmp_path))
    d._session.public_state = _onboth_state
    d._session.content_diff = lambda cid, hsp: {"cid": cid, "state": "same"}
    assert d.resolve_diff(5) == "same"
    v = d.public_state()["tones"][0]["variants"][0]
    assert v["diff"] == "same"


def test_persisted_cache_shows_verifying_until_confirmed(tmp_path):
    # A verdict left by a PRIOR session must NOT be shown as confirmed truth:
    # the device may have drifted (amp edit) while the app was closed, and the
    # library .hsp hash alone cannot detect that. Must read "verifying" until
    # this session re-confirms it (spec anti-goal: never lie about the device).
    (tmp_path / "tones").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tones" / "a.hsp").write_text("{}")
    cache = DiffCache(tmp_path / "c.json")
    d = LibraryDrawer(tmp_path, None, client_factory=None, cache=cache,
                      **_kw(tmp_path))
    d._session.public_state = _onboth_state
    cache.put("5", {"hsp_hash": d._hsp_hash("tones/a.hsp"),
                    "verdict": "same", "ts": 0})
    # fresh session, cid 5 not yet confirmed -> provisional, not "same"
    assert d.public_state()["tones"][0]["variants"][0]["diff"] == "verifying"
    # begin_reconcile() also drops confirmations so a reconnect re-verifies
    d._confirmed.add(5)
    assert d.public_state()["tones"][0]["variants"][0]["diff"] == "same"
    d.begin_reconcile()
    assert d.public_state()["tones"][0]["variants"][0]["diff"] == "verifying"
