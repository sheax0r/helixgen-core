"""Unit tests for the reference-based multi-setlist sync engine
(device/setlist_sync.py).

Two layers, both device-free:

* **pure reconcile logic** (``plan_pool`` / ``plan_references`` / ``plan_gc``) —
  no client at all; just decisions over plain data.
* **the device-driving entry point** (``sync_setlists``) — exercised against a
  FAKE ``HelixClient`` that models a pool + setlists in memory. ``read_hsp``,
  the ``bridge.*`` authoring calls, and ``_upload_missing_irs`` are stubbed so
  no ``.hsp`` is parsed and no network is touched.
"""
from __future__ import annotations

import pytest

from helixgen.device import setlist_sync as ss
from helixgen.device.client import Container, Cctp
from helixgen.device.manifest import SetlistManifest


# ---------------------------------------------------------------------------
# pure reconcile logic
# ---------------------------------------------------------------------------

def test_plan_pool_install_update_skip():
    # desired content hashes per tone
    hashes = {"New": "sha256:n", "Changed": "sha256:new", "Same": "sha256:s"}
    synced = {"Changed": "sha256:old", "Same": "sha256:s"}

    class M:
        def content_hash(self, name):
            return hashes.get(name)

    plan = ss.plan_pool(
        M(),
        tone_names=["New", "Changed", "Same"],
        device_pool_names=["Changed", "Same"],
        observed_hash_of=lambda n: synced.get(n),
    )
    assert plan["install"] == ["New"]
    assert plan["update"] == ["Changed"]
    assert plan["skip"] == ["Same"]


def test_plan_references_returns_desired_order():
    assert ss.plan_references(["A", "B", "C"], device_refs={}) == ["A", "B", "C"]


def test_plan_gc_never_flags_referenced_or_manifest_union():
    # pool holds four presets; manifest union wants Keep1; Keep2 is still
    # referenced by some setlist on the device; Orphan is neither.
    deletable = ss.plan_gc(
        manifest_union_names={"Keep1"},
        device_pool_names=["Keep1", "Keep2", "Orphan", "AlsoOrphan"],
        device_referenced_names={"Keep2"},
    )
    assert deletable == ["Orphan", "AlsoOrphan"]


def test_plan_gc_never_orphan_invariant():
    # a tone dropped from one setlist's membership but still referenced by the
    # other setlist on the device is never GC'd.
    deletable = ss.plan_gc(
        manifest_union_names=set(),          # manifest no longer wants it
        device_pool_names=["Shared"],
        device_referenced_names={"Shared"},  # but a live setlist still points at it
    )
    assert deletable == []


# ---------------------------------------------------------------------------
# fake client for the device-flow tests
# ---------------------------------------------------------------------------

class FakeClient:
    """In-memory pool + setlists modelling exactly the ops sync_setlists calls."""

    def __init__(self, *, setlists=None, pool=None):
        # setlists: {name: cid}. pool: list of (name, cid, posi).
        self._setlist_cids = dict(setlists or {})
        self._pool = [dict(name=n, cid_=c, posi=p) for (n, c, p) in (pool or [])]
        # references per setlist cid: list of {cctp, rcid, cid_, posi}
        self._refs = {cid: [] for cid in self._setlist_cids.values()}
        self._next_cid = 5000
        self._next_ref = 9000
        # call recorders
        self.set_content_data_calls = []
        self.deleted = []
        self.mirror_calls = []

    # -- context / lifecycle --
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def mutating(self):
        import contextlib
        return contextlib.nullcontext(self)

    def load_preset(self, cid):
        return True

    def get_edit_buffer(self):
        return b"TEMPLATE"

    @property
    def _raw(self):
        return self

    # -- reads --
    def resolve_setlist_cid(self, name):
        return self._setlist_cids.get(name)

    def list_setlists(self):
        return [{"cid_": c, "name": n, "cctp": Cctp.SETLIST}
                for n, c in self._setlist_cids.items()]

    def list_presets(self, container):
        return [dict(m) for m in self._pool]

    def list_container(self, cid):
        return [dict(m) for m in self._refs.get(cid, [])]

    # -- writes --
    def install_into_pool(self, blob, name, *, template_blob=None, pos=None):
        cid = self._next_cid
        self._next_cid += 1
        posi = max((m["posi"] for m in self._pool), default=-1) + 1
        self._pool.append(dict(name=name, cid_=cid, posi=posi))
        return cid

    def set_content_data(self, cid, blob):
        self.set_content_data_calls.append((cid, blob))
        return True

    def mirror_setlist(self, setlist_cid, ordered_pool_cids):
        ordered_pool_cids = list(ordered_pool_cids)
        self.mirror_calls.append((setlist_cid, ordered_pool_cids))
        current = self._refs.get(setlist_cid, [])
        cur_cids = {m["rcid"] for m in current}
        added, removed = [], []
        # remove refs no longer desired
        keep = []
        for m in current:
            if m["rcid"] in ordered_pool_cids:
                keep.append(m)
            else:
                removed.append(m["cid_"])
        # add newly desired refs, rebuild in order
        new_refs = []
        for pos, pc in enumerate(ordered_pool_cids):
            existing = next((m for m in keep if m["rcid"] == pc), None)
            if existing is not None:
                existing["posi"] = pos
                new_refs.append(existing)
            else:
                rc = self._next_ref
                self._next_ref += 1
                new_refs.append(dict(cctp=Cctp.REFERENCE, rcid=pc, cid_=rc, posi=pos))
                added.append(rc)
        self._refs[setlist_cid] = new_refs
        return {"added": added, "removed": removed}

    def delete(self, container, cids):
        cids = list(cids)
        self.deleted.extend(cids)
        drop = set(cids)
        self._pool = [m for m in self._pool if m["cid_"] not in drop]
        return True


def _stub_bridge(monkeypatch):
    """Stub the authoring bridge + read_hsp so no .hsp is parsed and no models
    are resolved — every tone authors a trivial blob."""
    monkeypatch.setattr(ss, "read_hsp", lambda path: {"_hsp": str(path)})
    monkeypatch.setattr(ss.bridge, "hsp_to_chain", lambda body, strict=True: [])
    monkeypatch.setattr(ss.bridge, "content_from_template",
                        lambda template_blob, chain: b"BLOB")
    monkeypatch.setattr(ss.bridge, "check_irs",
                        lambda client, body: {"present": set(), "missing": set()})


def _manifest(tmp_path, monkeypatch, setlists, hashes=None):
    """Build a manifest directly (no .hsp on disk) with given membership + hashes."""
    hashes = hashes or {}
    m = SetlistManifest(tmp_path / "setlists.json")
    for sl, tones in setlists.items():
        m.create_setlist(sl)
        for t in tones:
            m.tones[t] = {"path": f"/tones/{t}.hsp", "content_hash": hashes.get(t),
                          "source": "hsp"}
            m.setlists_map[sl].append(t)
    return m


# ---------------------------------------------------------------------------
# device-flow tests
# ---------------------------------------------------------------------------

def test_full_sync_installs_then_references(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is True
    assert res["errors"] == []
    assert res["pool"]["installed"] == ["Tone A", "Tone B"]
    assert res["pool"]["updated"] == []
    assert res["pool"]["skipped"] == []
    # both tones now referenced into the setlist, in order
    assert client.mirror_calls == [(42, [5000, 5001])]
    assert res["references"]["helixgen"]["added"] == [9000, 9001]
    # observed synced_hash persisted so a re-run can skip
    assert m.observed_pool_hash("Tone A") == "sha256:a"
    assert m.observed_pool_hash("Tone B") == "sha256:b"


def test_resync_unchanged_is_noop(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    # pool already holds Tone A; observed hash matches -> skip
    m.record_observed_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:a")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["installed"] == []
    assert res["pool"]["updated"] == []
    assert res["pool"]["skipped"] == ["Tone A"]
    assert client.set_content_data_calls == []
    # references mirrored to the same single cid (no-op diff)
    assert client.mirror_calls == [(42, [5000])]


def test_changed_hash_updates_not_reinstalls(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:NEW"})
    m.record_observed_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:OLD")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["installed"] == []
    assert res["pool"]["updated"] == ["Tone A"]
    # updated via SetContentData on the EXISTING cid, not a fresh install
    assert client.set_content_data_calls == [(5000, b"BLOB")]
    assert m.observed_pool_hash("Tone A") == "sha256:NEW"


def test_unresolved_setlist_errors_without_aborting(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch,
                  {"good": ["Tone A"], "missing": ["Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})
    # only "good" exists on the device
    client = FakeClient(setlists={"good": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["good", "missing"])

    assert res["ok"] is False
    assert any("missing" in e for e in res["errors"])
    assert any("Stadium app" in e for e in res["errors"])
    # the resolvable setlist still synced
    assert res["pool"]["installed"] == ["Tone A"]
    assert "good" in res["references"]
    assert "missing" not in res["references"]


def test_gc_only_on_all_run_and_orphan_safe(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    # manifest wants only Keep; Orphan lingers in the pool referenced by nobody;
    # Shared lingers referenced by an untracked setlist on the device.
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    client = FakeClient(
        setlists={"helixgen": 42, "other": 43},
        pool=[("Keep", 5000, 0), ("Orphan", 5001, 1), ("Shared", 5002, 2)],
    )
    # "other" setlist references Shared -> Shared must never be GC'd
    client._refs[43] = [dict(cctp=Cctp.REFERENCE, rcid=5002, cid_=9500, posi=0)]
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None, gc=True)

    assert res["gc"]["deleted"] == ["Orphan"]
    assert client.deleted == [5001]


def test_no_gc_when_specific_setlist(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    client = FakeClient(
        setlists={"helixgen": 42},
        pool=[("Keep", 5000, 0), ("Orphan", 5001, 1)],
    )
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    # gc requested but a specific setlist is named -> gc ignored
    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"], gc=True)

    assert res["gc"]["deleted"] == []
    assert client.deleted == []


def test_ir_upload_happens_unless_excluded(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    monkeypatch.setattr(ss.bridge, "check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11", "bb22"}})
    calls = []
    monkeypatch.setattr(ss, "_upload_missing_irs",
                        lambda ip, hashes: calls.append((ip, hashes)) or
                        [{"hash": h, "ok": True} for h in hashes])
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="9.9.9.9", setlists=["helixgen"])
    assert calls == [("9.9.9.9", ["aa11", "bb22"])]
    assert len(res["irs"]) == 2

    # exclude_irs -> no upload
    calls.clear()
    client2 = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client2)
    res2 = ss.sync_setlists(m, ip="9.9.9.9", setlists=["helixgen"], exclude_irs=True)
    assert calls == []
    assert res2["irs"] == []


def test_connection_drop_on_one_tone_is_per_tone_error_with_hint(tmp_path, monkeypatch):
    from helixgen.device.client import HelixError

    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A", "Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})

    class DroppingClient(FakeClient):
        def install_into_pool(self, blob, name, *, template_blob=None, pos=None):
            if name == "Tone A":
                raise HelixError(
                    "device connection lost after 3 reconnect attempts; if this "
                    "persists, reboot the Helix")
            return super().install_into_pool(blob, name,
                                             template_blob=template_blob, pos=pos)

    client = DroppingClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is False
    # the dropped tone lands in errors[], the other still installs
    assert any("Tone A" in e for e in res["errors"])
    assert res["pool"]["installed"] == ["Tone B"]
    # a connection-type error surfaces the resume hint
    assert "hint" in res
    assert "re-run" in res["hint"]
    assert "reboot" in res["hint"].lower()


def test_clean_run_has_no_hint(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is True
    assert "hint" not in res


def test_non_connection_error_has_no_hint(tmp_path, monkeypatch):
    # a per-tone error that is NOT a connection drop must not set the hint.
    _stub_bridge(monkeypatch)
    from helixgen.device.bridge import UnresolvedModel

    def _boom(body, strict=True):
        raise UnresolvedModel("HD2_Whatever")

    monkeypatch.setattr(ss.bridge, "hsp_to_chain", _boom)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Bad Tone"]},
                  hashes={"Bad Tone": "sha256:x"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])
    assert res["ok"] is False
    assert "hint" not in res


def test_unresolvable_model_is_per_tone_error(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    from helixgen.device.bridge import UnresolvedModel

    def _boom(body, strict=True):
        raise UnresolvedModel("HD2_Whatever")

    monkeypatch.setattr(ss.bridge, "hsp_to_chain", _boom)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Bad Tone"]},
                  hashes={"Bad Tone": "sha256:x"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])
    assert res["ok"] is False
    assert res["pool"]["installed"] == []
    assert any("Bad Tone" in e for e in res["errors"])
