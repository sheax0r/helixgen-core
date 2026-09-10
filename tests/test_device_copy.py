"""`device copy` / `rm` / `move` — the file-copy model (2026-09-09 design).

All three verbs are orchestration over existing machinery, so these tests drive
``helixgen.device.copy`` against a tiny stub client (the
``tests/test_device_reorder.py`` ``StubClient`` pattern — no socket, no real
HelixClient) with the transcoder and the shared IR core stubbed out.
"""
from __future__ import annotations

import contextlib
import json

import pytest

from helixgen.device import copy as C
from helixgen.device import ir_upload, transcode
from helixgen.device.client import Cctp, Container
from helixgen.hsp import HSP_MAGIC

BLOB = b"_sbepgsm-blob"


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch):
    """Transcode + IR upload are already-tested machinery; stub both."""
    monkeypatch.setattr(transcode, "hsp_to_sbepgsm",
                        lambda body, strict=True: BLOB)
    monkeypatch.setattr(ir_upload, "sync_preset_irs",
                        lambda client, body, ip, auto_irs=True: [])


@pytest.fixture
def hsp(tmp_path):
    def _make(name="Dream On"):
        p = tmp_path / f"{name}.hsp"
        p.write_bytes(HSP_MAGIC + json.dumps({"meta": {"name": name},
                                              "preset": {}}).encode())
        return p
    return _make


class _Raw:
    def __init__(self, owner):
        self._o = owner

    def set_content_data(self, cid, blob):
        self._o.calls.append(("set_content_data", cid, blob))
        return True

    def delete(self, container, cids):
        self._o.calls.append(("delete", container, list(cids)))
        self._o.pool = [m for m in self._o.pool if m["cid_"] not in cids]
        return True


class StubClient:
    """Exactly the surface copy.py + reorder.reorder_setlist_item call."""

    ip = "10.0.0.99"

    def __init__(self, *, pool=None, setlists=None, refs=None):
        self.pool = [dict(m) for m in (pool or [])]
        self.setlists = dict(setlists or {})
        self.refs = {k: [dict(m) for m in v] for k, v in (refs or {}).items()}
        self.calls = []
        self.strict = []          # (what, strict) for every gating listing
        self._raw = _Raw(self)
        self._next_cid = 900

    # -- reads ----------------------------------------------------------
    def list_presets(self, container=Container.POOL, *, strict=False):
        self.strict.append(("pool", strict))
        return [dict(m) for m in self.pool]

    def list_container(self, cid, *, strict=False):
        self.strict.append((cid, strict))
        return [dict(m) for m in self.refs.get(cid, [])]

    def resolve_setlist_cid(self, name, *, strict=True):
        return self.setlists.get(name)

    def list_setlists(self, *, strict=False):
        return [{"cid_": cid, "name": n, "cctp": Cctp.SETLIST}
                for n, cid in self.setlists.items()]

    def list_setlists_by_name(self, name, *, strict=True, setlists=None):
        want = name.strip().casefold()
        src = self.list_setlists() if setlists is None else setlists
        return [m for m in src
                if str(m.get("name", "")).strip().casefold() == want]

    def find_by_pos(self, container, pos, *, strict=False):
        self.strict.append((container, strict))
        return next((dict(m) for m in self.refs.get(container, [])
                     if m.get("posi") == pos), None)

    def _lowest_empty_posi(self, container):
        items = (self.pool if container == int(Container.POOL)
                 else self.refs.get(container, []))
        used = {m.get("posi") for m in items}
        p = 0
        while p in used:
            p += 1
        return p

    # -- writes ---------------------------------------------------------
    @contextlib.contextmanager
    def mutating(self):
        yield self

    def install_into_pool(self, blob, name, *, template_blob=None, pos=None):
        self._next_cid += 1
        cid = self._next_cid
        posi = self._lowest_empty_posi(int(Container.POOL)) if pos is None else pos
        self.calls.append(("install_into_pool", cid, name, blob))
        self.pool.append({"cid_": cid, "name": name, "posi": posi,
                          "cctp": Cctp.PRESET})
        return cid

    def reference_into_setlist(self, setlist_cid, pool_cid, pos):
        self._next_cid += 1
        ref = self._next_cid
        self.calls.append(("reference", setlist_cid, pool_cid, pos))
        self.refs.setdefault(setlist_cid, []).append(
            {"cid_": ref, "rcid": pool_cid, "posi": pos,
             "cctp": Cctp.REFERENCE})
        return ref

    def remove_reference(self, setlist_cid, ref_cid):
        self.calls.append(("remove_reference", setlist_cid, ref_cid))
        self.refs[setlist_cid] = [m for m in self.refs.get(setlist_cid, [])
                                  if m["cid_"] != ref_cid]
        return True

    def reorder_container(self, container, moved_cids, new_pos):
        self.calls.append(("reorder", container, list(moved_cids), new_pos))
        return [{"cid_": c, "posi": new_pos} for c in moved_cids]


def _gigs(**kw):
    """A device with setlist 'Gigs' (cid 1234) holding one referenced tone."""
    pool = kw.pop("pool", [{"cid_": 101, "name": "Back In Black", "posi": 0,
                            "cctp": Cctp.PRESET}])
    refs = kw.pop("refs", {1234: [{"cid_": 501, "rcid": 101, "posi": 0,
                                   "cctp": Cctp.REFERENCE}]})
    return StubClient(pool=pool, setlists={"Gigs": 1234}, refs=refs, **kw)


# ---------------------------------------------------------------------------
# copy — create
# ---------------------------------------------------------------------------

def test_copy_creates_and_appends_reference(hsp):
    c = _gigs()
    res = C.copy_tone(c, hsp(), setlist="Gigs")
    assert res["ok"] is True
    assert res["action"] == "created"
    assert res["name"] == "Dream On"
    assert res["setlist"] == "Gigs"
    assert res["posi"] == 1                      # appended after the incumbent
    assert res["errors"] == []
    kinds = [k[0] for k in c.calls]
    assert kinds == ["install_into_pool", "reference"]
    assert res["pool_cid"] == c.calls[0][1]
    assert res["cid"] != res["pool_cid"]         # a reference cid
    assert c.calls[1] == ("reference", 1234, res["pool_cid"], 1)


def test_copy_pool_only_adds_no_reference(hsp):
    c = _gigs()
    res = C.copy_tone(c, hsp(), setlist=None)
    assert res["action"] == "created"
    assert res["setlist"] is None
    assert res["cid"] == res["pool_cid"]
    assert res["posi"] == 1
    assert [k[0] for k in c.calls] == ["install_into_pool"]


def test_copy_explicit_pos_refuses_occupied_position(hsp):
    c = _gigs()
    res = C.copy_tone(c, hsp(), setlist="Gigs", pos=0)
    assert res["ok"] is False
    assert "not empty" in res["errors"][0]
    assert c.calls == []          # refused BEFORE anything was written


def test_copy_unknown_setlist_is_a_reported_error(hsp):
    c = _gigs()
    res = C.copy_tone(c, hsp(), setlist="Ghost")
    assert res["ok"] is False
    assert "no setlist named 'Ghost'" in res["errors"][0]
    assert c.calls == []


def test_copy_requires_meta_name(tmp_path):
    p = tmp_path / "nameless.hsp"
    p.write_bytes(HSP_MAGIC + json.dumps({"meta": {}}).encode())
    with pytest.raises(ValueError, match="meta.name"):
        C.copy_tone(_gigs(), p, setlist="Gigs")


# ---------------------------------------------------------------------------
# copy — update (upsert)
# ---------------------------------------------------------------------------

def test_copy_updates_existing_setlist_preset_in_place(hsp):
    c = _gigs()
    res = C.copy_tone(c, hsp("Back In Black"), setlist="Gigs")
    assert res["ok"] is True
    assert res["action"] == "updated"
    assert res["cid"] == 501 and res["pool_cid"] == 101 and res["posi"] == 0
    assert c.calls == [("set_content_data", 101, BLOB)]


def test_copy_update_matches_name_case_insensitively(hsp):
    c = _gigs(pool=[{"cid_": 101, "name": "back in black", "posi": 0,
                     "cctp": Cctp.PRESET}])
    res = C.copy_tone(c, hsp("Back In Black"), setlist="Gigs")
    assert res["action"] == "updated"
    assert res["name"] == "Back In Black"        # the .hsp's exact spelling
    assert c.calls == [("set_content_data", 101, BLOB)]


def test_copy_updates_pooled_preset_and_adds_missing_reference(hsp):
    """In the pool but not referenced: update content AND reference it —
    never a second pool entry under the same name."""
    c = _gigs(refs={1234: []})
    res = C.copy_tone(c, hsp("Back In Black"), setlist="Gigs")
    assert res["action"] == "updated"
    assert res["pool_cid"] == 101 and res["posi"] == 0
    assert [k[0] for k in c.calls] == ["set_content_data", "reference"]


def test_copy_with_explicit_cid_short_circuits_resolution(hsp):
    c = _gigs(pool=[{"cid_": 101, "name": "Dup", "posi": 0, "cctp": Cctp.PRESET},
                    {"cid_": 102, "name": "Dup", "posi": 1, "cctp": Cctp.PRESET}],
              refs={1234: []})
    res = C.copy_tone(c, hsp("Dup"), setlist=None, cid=102)
    assert res["action"] == "updated"
    assert res["pool_cid"] == 102
    assert c.calls[0] == ("set_content_data", 102, BLOB)


# ---------------------------------------------------------------------------
# ambiguity
# ---------------------------------------------------------------------------

def test_resolve_target_raises_on_ambiguous_name():
    c = _gigs(pool=[{"cid_": 101, "name": "Dup", "posi": 0, "cctp": Cctp.PRESET},
                    {"cid_": 102, "name": "dup", "posi": 1, "cctp": Cctp.PRESET}],
              refs={1234: []})
    with pytest.raises(C.AmbiguousName) as e:
        C.resolve_target(c, "Dup", setlist="Gigs")
    assert e.value.name == "Dup"
    assert sorted(x["cid"] for x in e.value.candidates) == [101, 102]
    assert {x["setlist"] for x in e.value.candidates} == {None}


def test_copy_never_guesses_on_ambiguity(hsp):
    c = _gigs(pool=[{"cid_": 101, "name": "Dup", "posi": 0, "cctp": Cctp.PRESET},
                    {"cid_": 102, "name": "Dup", "posi": 1, "cctp": Cctp.PRESET}],
              refs={1234: []})
    with pytest.raises(C.AmbiguousName):
        C.copy_tone(c, hsp("Dup"), setlist="Gigs")
    assert c.calls == []


def test_resolve_target_prefers_the_setlist_over_the_pool():
    """Same name in the setlist AND elsewhere in the pool: the setlist wins,
    and the pool's second entry is not an ambiguity."""
    c = _gigs(pool=[{"cid_": 101, "name": "Twin", "posi": 0, "cctp": Cctp.PRESET},
                    {"cid_": 102, "name": "Twin", "posi": 1, "cctp": Cctp.PRESET}],
              refs={1234: [{"cid_": 501, "rcid": 101, "posi": 0,
                            "cctp": Cctp.REFERENCE}]})
    t = C.resolve_target(c, "Twin", setlist="Gigs")
    assert t == {"cid": 501, "pool_cid": 101, "name": "Twin", "posi": 0}


def test_resolve_target_absent_is_none():
    assert C.resolve_target(_gigs(), "Nope", setlist="Gigs") is None


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------

def test_remove_drops_reference_and_keeps_the_pool_preset():
    c = _gigs()
    res = C.remove_tone(c, "Back In Black", setlist="Gigs")
    assert res == {"ok": True, "name": "Back In Black", "removed_ref": 501,
                   "removed_pool": None, "errors": []}
    assert c.calls == [("remove_reference", 1234, 501)]
    assert [m["cid_"] for m in c.pool] == [101]


def test_remove_also_pool_REFUSES_when_another_setlist_references_it():
    """`--also-pool` must never orphan: deleting a pool preset another setlist
    still points at leaves that setlist referencing a dead cid, and the verb's
    own help promises it can't happen. (Adversarial review, CRITICAL 1 — the
    original single-setlist fixture passed on the broken behavior.)"""
    c = StubClient(
        pool=[{"cid_": 101, "name": "Back In Black", "posi": 0,
               "cctp": Cctp.PRESET}],
        setlists={"Gigs": 1234, "Studio": 1235},
        refs={1234: [{"cid_": 501, "rcid": 101, "posi": 0,
                      "cctp": Cctp.REFERENCE}],
              1235: [{"cid_": 502, "rcid": 101, "posi": 0,
                      "cctp": Cctp.REFERENCE}]},
    )
    res = C.remove_tone(c, "Back In Black", setlist="Gigs", also_pool=True)
    assert res["removed_ref"] == 501          # the reference still goes
    assert res["removed_pool"] is None        # the pool preset does NOT
    assert res["ok"] is False
    assert "Studio" in " ".join(res["errors"])
    assert c.pool != []                       # still there for Studio
    assert not any(k == "delete" for k, *_ in c.calls)


def test_remove_also_pool_deletes_when_nothing_else_references_it():
    c = _gigs()
    res = C.remove_tone(c, "Back In Black", setlist="Gigs", also_pool=True)
    assert res["removed_ref"] == 501 and res["removed_pool"] == 101
    assert c.calls == [("remove_reference", 1234, 501),
                       ("delete", int(Container.POOL), [101])]
    assert c.pool == []


def test_explicit_cid_that_matches_nothing_is_an_error_not_a_create(hsp):
    """--cid exists ONLY to disambiguate a duplicate name. A typo'd cid used to
    read as "absent" and CREATE a third preset (adversarial review, HIGH 6)."""
    c = StubClient(
        pool=[{"cid_": 101, "name": "Dream On", "posi": 0, "cctp": Cctp.PRESET},
              {"cid_": 102, "name": "Dream On", "posi": 1, "cctp": Cctp.PRESET}],
        setlists={"Gigs": 1234}, refs={1234: []},
    )
    with pytest.raises(ValueError, match="matches no preset"):
        C.resolve_target(c, "Dream On", setlist="Gigs", cid=10)
    # Through copy_tone the same miss surfaces as a failed run (the module's
    # catch-all turns bad input into errors[], and the CLI exits 1) — what
    # matters is that it does NOT fall through to "absent" and create a third.
    before = list(c.pool)
    res = C.copy_tone(c, hsp("Dream On"), setlist="Gigs", cid=10)
    assert res["ok"] is False
    assert "matches no preset" in " ".join(res["errors"])
    assert c.pool == before          # nothing written
    assert not any(k == "install_into_pool" for k, *_ in c.calls)


def test_remove_absent_name_reports_error_without_touching_the_device():
    c = _gigs()
    res = C.remove_tone(c, "Ghost", setlist="Gigs")
    assert res["ok"] is False and res["removed_ref"] is None
    assert "no preset named 'Ghost'" in res["errors"][0]
    assert c.calls == []


def test_remove_ambiguous_name_raises():
    c = _gigs(pool=[{"cid_": 101, "name": "Dup", "posi": 0, "cctp": Cctp.PRESET},
                    {"cid_": 102, "name": "Dup", "posi": 1, "cctp": Cctp.PRESET}],
              refs={1234: []})
    with pytest.raises(C.AmbiguousName):
        C.remove_tone(c, "Dup", setlist="Gigs")


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------

def test_move_reorders_by_resolved_reference_cid():
    c = _gigs(pool=[{"cid_": 101, "name": "Back In Black", "posi": 0,
                     "cctp": Cctp.PRESET},
                    {"cid_": 102, "name": "Dream On", "posi": 1,
                     "cctp": Cctp.PRESET}],
              refs={1234: [{"cid_": 501, "rcid": 101, "posi": 0,
                            "cctp": Cctp.REFERENCE},
                           {"cid_": 502, "rcid": 102, "posi": 1,
                            "cctp": Cctp.REFERENCE}]})
    res = C.move_tone(c, "Dream On", setlist="Gigs", to=0)
    assert res == {"ok": True, "name": "Dream On", "from": 1, "to": 0,
                   "errors": []}
    assert ("reorder", 1234, [502], 0) in c.calls


def test_move_pool_only_preset_is_an_error():
    c = _gigs(refs={1234: []})
    res = C.move_tone(c, "Back In Black", setlist="Gigs", to=0)
    assert res["ok"] is False
    assert "no preset named 'Back In Black'" in res["errors"][0]
    assert c.calls == []


# ---------------------------------------------------------------------------
# IRs
# ---------------------------------------------------------------------------

def test_copy_uploads_irs_by_default(hsp, monkeypatch):
    seen = {}

    def fake(client, body, ip, auto_irs=True):
        seen.update(ip=ip, auto_irs=auto_irs)
        return [{"hash": "ab", "ok": True, "outcome": "imported",
                 "note": "imported ab"}]

    monkeypatch.setattr(ir_upload, "sync_preset_irs", fake)
    c = _gigs()
    res = C.copy_tone(c, hsp(), setlist="Gigs")
    assert seen == {"ip": "10.0.0.99", "auto_irs": True}
    assert res["irs"][0]["outcome"] == "imported"
    assert res["errors"] == []
    assert res["ok"] is True


def test_copy_no_irs_opts_out_without_erroring(hsp, monkeypatch):
    monkeypatch.setattr(
        ir_upload, "sync_preset_irs",
        lambda client, body, ip, auto_irs=True: [
            {"hash": "ab", "ok": False, "outcome": "skipped_auto_irs_off",
             "note": "not uploaded"}] if not auto_irs else [])
    res = C.copy_tone(_gigs(), hsp(), setlist="Gigs", with_irs=False)
    assert res["ok"] is True
    assert res["irs"][0]["outcome"] == "skipped_auto_irs_off"
    assert res["errors"] == []       # opting out is not a failure


def test_copy_survives_a_failed_ir_upload(hsp, monkeypatch):
    monkeypatch.setattr(
        ir_upload, "sync_preset_irs",
        lambda client, body, ip, auto_irs=True: [
            {"hash": "ab", "ok": False, "outcome": "upload_error",
             "note": "sftp died"}])
    c = _gigs()
    res = C.copy_tone(c, hsp(), setlist="Gigs")
    assert res["ok"] is True                       # per-item, never aborting
    assert res["errors"] == ["sftp died"]
    assert [k[0] for k in c.calls] == ["install_into_pool", "reference"]


# ---------------------------------------------------------------------------
# strict emptiness (#40): a listing timeout must never read as "slot empty"
# ---------------------------------------------------------------------------

def test_every_gating_listing_is_strict(hsp):
    c = _gigs()
    C.copy_tone(c, hsp(), setlist="Gigs")
    # the post-write pool re-list (bookkeeping) is the only lenient read
    assert [s for _w, s in c.strict].count(False) == 1
    assert ("pool", True) in c.strict and (1234, True) in c.strict


def test_a_timed_out_listing_aborts_instead_of_writing(hsp):
    from helixgen.device.client import HelixError

    class Timeout(StubClient):
        def list_container(self, cid, *, strict=False):
            if strict:
                raise HelixError("listing timed out")
            return super().list_container(cid, strict=strict)

    c = Timeout(pool=[], setlists={"Gigs": 1234}, refs={1234: []})
    res = C.copy_tone(c, hsp(), setlist="Gigs")
    assert res["ok"] is False
    assert "timed out" in res["errors"][0]
    assert c.calls == []
