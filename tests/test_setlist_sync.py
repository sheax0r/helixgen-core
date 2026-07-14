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
from helixgen.device import transcode as _tc
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


def test_plan_pool_force_updates_even_when_hash_matches():
    # #25 residual: --repush forces a content re-push for every in-scope tone
    # already in the pool, even when the recorded hash agrees (a transcoder
    # upgrade changed what .hsp -> device content produces without changing
    # the .hsp itself, so hash-based change detection never notices).
    hashes = {"New": "sha256:n", "Same": "sha256:s"}
    synced = {"Same": "sha256:s"}

    class M:
        def content_hash(self, name):
            return hashes.get(name)

    plan = ss.plan_pool(
        M(),
        tone_names=["New", "Same"],
        device_pool_names=["Same"],
        observed_hash_of=lambda n: synced.get(n),
        force=True,
    )
    # "New" isn't in the pool yet -> still an install, not an update.
    assert plan["install"] == ["New"]
    # "Same" hash matches but force=True bumps it into update anyway.
    assert plan["update"] == ["Same"]
    assert plan["skip"] == []


def test_plan_pool_force_false_is_unchanged():
    # default (no force) behaves exactly like before: hash-matching tones skip.
    hashes = {"Same": "sha256:s"}
    synced = {"Same": "sha256:s"}

    class M:
        def content_hash(self, name):
            return hashes.get(name)

    plan = ss.plan_pool(
        M(), tone_names=["Same"], device_pool_names=["Same"],
        observed_hash_of=lambda n: synced.get(n), force=False,
    )
    assert plan["update"] == []
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
    def resolve_setlist_cid(self, name, *, strict=True):
        return self._setlist_cids.get(name)

    def list_setlists(self, *, strict=False):
        return [{"cid_": c, "name": n, "cctp": Cctp.SETLIST}
                for n, c in self._setlist_cids.items()]

    def list_presets(self, container, *, strict=False):
        return [dict(m) for m in self._pool]

    def list_container(self, cid, *, strict=False):
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
    """Stub the transcoder + read_hsp so no .hsp is parsed and no models are
    resolved — every tone transcodes to a trivial blob."""
    monkeypatch.setattr(ss, "read_hsp", lambda path: {"_hsp": str(path)})
    monkeypatch.setattr(_tc, "hsp_to_sbepgsm", lambda body, strict=True: b"BLOB")
    monkeypatch.setattr(ss.bridge, "check_irs",
                        lambda client, body: {"present": set(), "missing": set()})


def _manifest(tmp_path, monkeypatch, setlists, hashes=None, synced=True):
    """Build a manifest directly (no .hsp on disk) with given membership + hashes.
    Setlists default to ``synced`` (mirrored) — the state a targeted sync leaves
    them in; pass ``synced=False`` to model local-only drafts."""
    hashes = hashes or {}
    m = SetlistManifest(tmp_path / "setlists.json")
    for sl, tones in setlists.items():
        m.create_setlist(sl)
        m.setlists_map[sl]["synced"] = synced
        for t in tones:
            m.tones[t] = {"path": f"/tones/{t}.hsp", "content_hash": hashes.get(t),
                          "doc": None, "source": "authored", "slot": "auto",
                          "device": None}
            m.setlists_map[sl]["tones"].append(t)
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


def test_repush_forces_content_update_for_hash_matching_tone(tmp_path, monkeypatch):
    # #25 residual: `device sync <setlist> --repush` re-transcodes + re-pushes
    # a tone's content even though its recorded hash matches the pool.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    m.record_observed_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:a")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"], repush=True)

    assert res["ok"] is True
    assert res["pool"]["installed"] == []
    assert res["pool"]["updated"] == ["Tone A"]
    assert res["pool"]["skipped"] == []
    # content refreshed via SetContentData into the EXISTING cid (the
    # non-activating `device restore` primitive) -- not delete+recreate.
    assert client.set_content_data_calls == [(5000, b"BLOB")]
    assert 5000 not in client.deleted
    # references + hash bookkeeping proceed exactly as a normal update would.
    assert m.observed_pool_hash("Tone A") == "sha256:a"


def test_repush_false_default_leaves_matching_hash_untouched(tmp_path, monkeypatch):
    # Without --repush, an unchanged tone is still skipped (no behavior change).
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    m.record_observed_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:a")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["updated"] == []
    assert res["pool"]["skipped"] == ["Tone A"]
    assert client.set_content_data_calls == []


def test_repush_still_installs_missing_tones_normally(tmp_path, monkeypatch):
    # repush only forces re-push of tones ALREADY in the pool; a tone missing
    # from the pool still goes through the normal install path.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Old", "New"]},
                  hashes={"Old": "sha256:o", "New": "sha256:n"})
    m.record_observed_pool("Old", cid=5000, posi=0, synced_hash="sha256:o")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Old", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"], repush=True)

    assert res["pool"]["installed"] == ["New"]
    assert res["pool"]["updated"] == ["Old"]
    assert client.set_content_data_calls == [(5000, b"BLOB")]


def test_repush_pathless_pool_present_tone_is_per_tone_error(tmp_path, monkeypatch):
    # A pathless tone (device save/create — no local .hsp) present in the pool
    # is bumped into the update bucket by --repush, but there is nothing local
    # to transcode from: it must surface as a per-tone error (bucket-agnostic
    # wording, not "cannot install"), while other tones still repush and the
    # run never aborts.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    m.record_observed_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:a")
    m.tones["MySave"] = {"path": None, "content_hash": None, "doc": None,
                         "source": "save", "slot": "3C", "device": None}
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Tone A", 5000, 0), ("MySave", 5009, 9)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"], repush=True)

    # the pathless tone errored per-tone; the .hsp-backed tone still repushed
    assert res["ok"] is False
    assert any("MySave" in e for e in res["errors"])
    assert not any("cannot install" in e for e in res["errors"])
    assert any("no .hsp source" in e for e in res["errors"])
    assert res["pool"]["updated"] == ["Tone A"]
    assert client.set_content_data_calls == [(5000, b"BLOB")]
    # nothing was deleted or recreated for the pathless tone
    assert client.deleted == []


def test_repush_does_not_change_references_or_ir_behavior(tmp_path, monkeypatch):
    # repush is purely a pool-content decision; reference rebuild and IR
    # upload behavior are identical to a normal sync.
    _stub_bridge(monkeypatch)
    monkeypatch.setattr(ss.bridge, "check_irs",
                        lambda client, body: {"present": set(), "missing": {"aa11"}})
    calls = []
    monkeypatch.setattr(ss, "_upload_missing_irs",
                        lambda ip, hashes: calls.append((ip, hashes)) or
                        [{"hash": h, "ok": True} for h in hashes])
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    m.record_observed_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:a")
    client = FakeClient(setlists={"helixgen": 42}, pool=[("Tone A", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"], repush=True)

    assert client.mirror_calls == [(42, [5000])]
    assert calls == [("1.2.3.4", ["aa11"])]
    assert len(res["irs"]) == 1


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
    assert any("device setlist create" in e for e in res["errors"])
    # the resolvable setlist still synced; Tone B is slot-marked ("auto"), so
    # the user-population mirror still pools it — its references arrive once
    # the setlist exists on the device.
    assert res["pool"]["installed"] == ["Tone A", "Tone B"]
    assert "good" in res["references"]
    assert "missing" not in res["references"]


# -- #39: strict setlist resolution — abort/skip, never mint a duplicate ----

def test_resolve_listing_failure_is_distinct_from_not_found_no_create_hint(
        tmp_path, monkeypatch):
    """A resolve_setlist_cid failure (simulated network timeout) must be
    reported distinctly from a genuine "not found" — critically, it must NOT
    tell the user to `device setlist create` it, since that's exactly the
    guidance that mints a duplicate when the setlist actually already exists
    but the listing just glitched (backlog #39)."""
    from helixgen.device.client import HelixError

    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch,
                  {"flaky": ["Tone A"], "good": ["Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})

    class FlakyResolve(FakeClient):
        def resolve_setlist_cid(self, name, *, strict=True):
            if name == "flaky":
                raise HelixError("no reply listing container -5 (timeout)")
            return super().resolve_setlist_cid(name, strict=strict)

    client = FlakyResolve(setlists={"good": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["flaky", "good"])

    assert res["ok"] is False
    assert any("flaky" in e and "could not verify" in e for e in res["errors"])
    assert not any("flaky" in e and "device setlist create" in e
                  for e in res["errors"])
    # the resolvable setlist still synced fully
    assert "good" in res["references"]
    assert "flaky" not in res["references"]


def test_gc_skips_deletes_when_referenced_names_listing_fails(tmp_path, monkeypatch):
    """The never-orphan gate for --gc must fail closed: if we can't verify
    what's referenced, delete NOTHING this run rather than risk treating a
    still-referenced preset as an orphan (the ir-prune precedent #39 cites)."""
    from helixgen.device.client import HelixError

    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    client = FakeClient(
        setlists={"helixgen": 42},
        pool=[("Keep", 5000, 0), ("Orphan", 5001, 1)],
    )
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    def raise_referenced(_client, _pool_by_name):
        raise HelixError("no reply listing container -5 (timeout)")

    monkeypatch.setattr(ss, "_device_referenced_names", raise_referenced)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None, gc=True)

    assert res["gc"]["deleted"] == []
    assert client.deleted == []
    assert any("could not verify" in e.lower() for e in res["errors"])


def test_unsynced_delete_skipped_when_referenced_names_listing_fails(
        tmp_path, monkeypatch):
    """Same never-orphan fail-closed rule for the (non-gc) per-tone unsynced
    delete step: a listing failure must skip the delete, not proceed on a
    silently-empty "nothing is referenced" read."""
    from helixgen.device.client import HelixError

    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Gone", slot=None, content_hash="sha256:g")
    m.record_observed_pool("Gone", cid=5001, posi=1, synced_hash="sha256:g")
    m.tones["Gone"]["slot"] = None
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Keep", 5000, 0), ("Gone", 5001, 1)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    def raise_referenced(_client, _pool_by_name):
        raise HelixError("no reply listing container -5 (timeout)")

    monkeypatch.setattr(ss, "_device_referenced_names", raise_referenced)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == []
    assert client.deleted == []
    assert "Gone" in m.tones  # untouched — still on the device per the manifest
    assert any("could not verify" in e.lower() for e in res["errors"])


def test_mirror_setlist_failure_is_per_setlist_not_fatal(tmp_path, monkeypatch):
    """mirror_setlist's own current-refs listing is now strict (#39 audit) —
    a failure there for ONE setlist must not abort every other setlist's
    reference rebuild in the same sync run."""
    from helixgen.device.client import HelixError

    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch,
                  {"flaky": ["Tone A"], "good": ["Tone B"]},
                  hashes={"Tone A": "sha256:a", "Tone B": "sha256:b"})

    class FlakyMirror(FakeClient):
        def mirror_setlist(self, setlist_cid, ordered_pool_cids):
            if setlist_cid == self._setlist_cids.get("flaky"):
                raise HelixError("no reply listing container "
                                 f"{setlist_cid} (timeout)")
            return super().mirror_setlist(setlist_cid, ordered_pool_cids)

    client = FlakyMirror(setlists={"flaky": 42, "good": 43})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["flaky", "good"])

    assert res["ok"] is False
    assert any("flaky" in e and "could not verify" in e for e in res["errors"])
    assert "flaky" not in res["references"]
    assert "good" in res["references"]
    # both tones still installed into the pool regardless of the reference
    # rebuild failure
    assert sorted(res["pool"]["installed"]) == ["Tone A", "Tone B"]


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


# ---------------------------------------------------------------------------
# managed user-population mirror (design §4): slot-marked tones install even
# with no setlist membership; slot=None tones still in the pool are deleted.
# ---------------------------------------------------------------------------

def _add_slot_only_tone(m, name, *, slot, content_hash=None):
    m.tones[name] = {"path": f"/tones/{name}.hsp", "content_hash": content_hash,
                     "doc": None, "source": "authored", "slot": slot,
                     "device": None}


def test_slot_only_tone_installs_on_targeted_sync(tmp_path, monkeypatch):
    # `device add --slot 5A` + `device sync <setlist>` must install the tone
    # even though it belongs to no setlist.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"})
    _add_slot_only_tone(m, "Solo", slot="5A", content_hash="sha256:solo")
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is True
    assert sorted(res["pool"]["installed"]) == ["Solo", "Tone A"]
    # Solo is in the pool but NOT referenced into the setlist
    assert client.mirror_calls == [(42, [5000])]
    assert m.tones["Solo"]["device"] is not None


def test_slot_only_auto_tone_installs_on_all_run(tmp_path, monkeypatch):
    # `device add` (slot auto) + `device sync --all` with no setlists at all.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {}, hashes={})
    _add_slot_only_tone(m, "Solo", slot="auto", content_hash="sha256:solo")
    client = FakeClient()
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None)

    assert res["pool"]["installed"] == ["Solo"]
    # the "auto" slot was resolved to a concrete label (assign_slots runs
    # before the reconcile; the manifest never persists "auto" past a sync)
    assert m.tones["Solo"]["slot"] == "1A"


def test_unsynced_tone_deleted_from_pool_on_sync(tmp_path, monkeypatch):
    # `device unsync <tone>` (slot=None) + sync deletes it from the device
    # while keeping the library registration.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Gone", slot=None, content_hash="sha256:g")
    m.record_observed_pool("Gone", cid=5001, posi=1, synced_hash="sha256:g")
    m.tones["Gone"]["slot"] = None  # record_observed_pool must not resurrect it
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Keep", 5000, 0), ("Gone", 5001, 1)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == ["Gone"]
    assert client.deleted == [5001]
    assert "Gone" in m.tones            # library registration survives
    assert m.tones["Gone"]["device"] is None


def test_unsynced_tone_still_referenced_is_never_orphaned(tmp_path, monkeypatch):
    # never-orphan: a slot=None tone still referenced by a live device setlist
    # is NOT deleted from the pool.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Shared", slot=None, content_hash="sha256:s")
    client = FakeClient(setlists={"helixgen": 42, "other": 43},
                        pool=[("Keep", 5000, 0), ("Shared", 5002, 2)])
    client._refs[43] = [dict(cctp=Cctp.REFERENCE, rcid=5002, cid_=9500, posi=0)]
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == []
    assert client.deleted == []


def test_slot_null_member_of_target_setlist_is_installed_not_deleted(tmp_path, monkeypatch):
    # a slot-less member of a setlist being synced is installed for the
    # references, never bucketed into the mirror delete.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Draft"]},
                  hashes={"Draft": "sha256:d"})
    m.tones["Draft"]["slot"] = None
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["installed"] == ["Draft"]
    assert res["pool"]["deleted"] == []
    assert client.deleted == []


def test_gc_spares_slot_only_tones(tmp_path, monkeypatch):
    # --all --gc must not collect a pool preset that a slot-only tone wants.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Solo", slot="5A", content_hash="sha256:solo")
    m.record_observed_pool("Solo", cid=5003, posi=3, synced_hash="sha256:solo")
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Keep", 5000, 0), ("Orphan", 5001, 1),
                              ("Solo", 5003, 3)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None, gc=True)

    assert res["gc"]["deleted"] == ["Orphan"]
    assert client.deleted == [5001]
    assert "Solo" not in res["gc"]["deleted"]


def test_untracked_same_name_pool_preset_is_never_deleted(tmp_path, monkeypatch):
    # A manifest tone with slot=None and NO prior-placement evidence (device
    # null, no observed entry — e.g. every freshly-generated tone) must not
    # cause deletion of a same-named pool preset helixgen never placed.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Ghost", slot=None, content_hash="sha256:g")
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Keep", 5000, 0), ("Ghost", 7000, 1)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == []
    assert client.deleted == []


def test_pathless_slot_tone_absent_from_pool_is_not_an_error(tmp_path, monkeypatch):
    # A pathless tone (device save/create) marked with a slot but absent from
    # the pool has nothing local to install from — skip silently, don't poison
    # every sync with a permanent error.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.tones["MySave"] = {"path": None, "content_hash": None, "doc": None,
                         "source": "save", "slot": "3C", "device": None}
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is True
    assert res["errors"] == []
    assert res["pool"]["installed"] == ["Keep"]


def test_pathless_slot_tone_present_in_pool_is_skipped(tmp_path, monkeypatch):
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    m.tones["MySave"] = {"path": None, "content_hash": None, "doc": None,
                         "source": "save", "slot": "3C", "device": None}
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Keep", 5000, 0), ("MySave", 5009, 9)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["ok"] is True
    assert "MySave" in res["pool"]["skipped"]


def test_all_run_skips_unsynced_draft_setlists(tmp_path, monkeypatch):
    # A local-only draft (synced=False) is never touched on the device — even
    # when a device setlist with the same name exists (design §4). Its stale
    # same-name device references must survive an --all run.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"live": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    m.create_setlist("draft")           # empty local draft, synced=False
    client = FakeClient(setlists={"live": 42, "draft": 43},
                        pool=[("Keep", 5000, 0), ("UserPreset", 6000, 1)])
    client._refs[43] = [dict(cctp=Cctp.REFERENCE, rcid=6000, cid_=9500, posi=0)]
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None, gc=True)

    assert res["setlists"] == ["live"]
    assert all(call[0] != 43 for call in client.mirror_calls)  # draft untouched
    assert res["gc"]["deleted"] == []                          # UserPreset survives
    assert res["errors"] == []


def test_all_run_reports_skipped_nonempty_drafts(tmp_path, monkeypatch):
    # --all names the non-empty drafts it skipped so a user who never opted
    # a setlist into mirroring learns why it isn't syncing.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"live": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    m.create_setlist("wip")
    m.setlists_map["wip"]["tones"].append("Keep")   # non-empty draft
    m.create_setlist("empty-draft")
    client = FakeClient(setlists={"live": 42}, pool=[("Keep", 5000, 0)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=None)

    assert res["skipped_draft_setlists"] == ["wip"]


def test_targeted_sync_marks_setlist_synced(tmp_path, monkeypatch):
    # Explicitly syncing a draft opts it into mirroring (synced=True), so
    # subsequent --all runs maintain it.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Tone A"]},
                  hashes={"Tone A": "sha256:a"}, synced=False)
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert m.is_synced("helixgen") is True


def test_unresolved_target_members_are_not_mirror_deleted(tmp_path, monkeypatch):
    # Members of a setlist that failed to resolve on the device must not fall
    # into the mirror-delete bucket in the same run.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch,
                  {"good": ["Keep"], "missing": ["Draft"]},
                  hashes={"Keep": "sha256:k", "Draft": "sha256:d"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    m.record_observed_pool("Draft", cid=5001, posi=1, synced_hash="sha256:d")
    m.tones["Draft"]["slot"] = None
    client = FakeClient(setlists={"good": 42},
                        pool=[("Keep", 5000, 0), ("Draft", 5001, 1)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["good", "missing"])

    assert "Draft" not in res["pool"]["deleted"]
    assert client.deleted == []


def test_never_orphan_delete_skip_is_reported(tmp_path, monkeypatch):
    # When never-orphan blocks an unsync delete, the result says so instead of
    # silently doing nothing.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Shared", slot=None, content_hash="sha256:s")
    m.record_observed_pool("Shared", cid=5002, posi=2, synced_hash="sha256:s")
    m.tones["Shared"]["slot"] = None
    client = FakeClient(setlists={"helixgen": 42, "other": 43},
                        pool=[("Keep", 5000, 0), ("Shared", 5002, 2)])
    client._refs[43] = [dict(cctp=Cctp.REFERENCE, rcid=5002, cid_=9500, posi=0)]
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == []
    assert res["pool"]["delete_skipped"] == ["Shared"]


def test_stale_target_reference_is_removed_then_tone_deleted(tmp_path, monkeypatch):
    # Ordering pin: the unsync delete runs AFTER the reference rebuild, so a
    # stale reference on the target setlist itself doesn't never-orphan-block
    # the delete.
    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    _add_slot_only_tone(m, "Gone", slot=None, content_hash="sha256:g")
    m.record_observed_pool("Gone", cid=5001, posi=1, synced_hash="sha256:g")
    m.tones["Gone"]["slot"] = None
    client = FakeClient(setlists={"helixgen": 42},
                        pool=[("Keep", 5000, 0), ("Gone", 5001, 1)])
    # stale ref to Gone on the TARGET setlist
    client._refs[42] = [dict(cctp=Cctp.REFERENCE, rcid=5001, cid_=9400, posi=0)]
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == ["Gone"]
    assert 5001 in client.deleted


def test_helix_error_mid_delete_continues_with_other_tones(tmp_path, monkeypatch):
    from helixgen.device.client import HelixError

    _stub_bridge(monkeypatch)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Keep"]},
                  hashes={"Keep": "sha256:k"})
    m.record_observed_pool("Keep", cid=5000, posi=0, synced_hash="sha256:k")
    for name, cid, posi in [("Gone1", 5001, 1), ("Gone2", 5002, 2)]:
        _add_slot_only_tone(m, name, slot=None, content_hash=f"sha256:{name}")
        m.record_observed_pool(name, cid=cid, posi=posi,
                               synced_hash=f"sha256:{name}")
        m.tones[name]["slot"] = None

    class FailFirstDelete(FakeClient):
        def delete(self, container, cids):
            if 5001 in cids:
                raise HelixError("boom")
            return super().delete(container, cids)

    client = FailFirstDelete(setlists={"helixgen": 42},
                             pool=[("Keep", 5000, 0), ("Gone1", 5001, 1),
                                   ("Gone2", 5002, 2)])
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])

    assert res["pool"]["deleted"] == ["Gone2"]
    assert any("Gone1" in e for e in res["errors"])


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

    monkeypatch.setattr(_tc, "hsp_to_sbepgsm", _boom)
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

    monkeypatch.setattr(_tc, "hsp_to_sbepgsm", _boom)
    m = _manifest(tmp_path, monkeypatch, {"helixgen": ["Bad Tone"]},
                  hashes={"Bad Tone": "sha256:x"})
    client = FakeClient(setlists={"helixgen": 42})
    monkeypatch.setattr(ss, "HelixClient", lambda **k: client)

    res = ss.sync_setlists(m, ip="1.2.3.4", setlists=["helixgen"])
    assert res["ok"] is False
    assert res["pool"]["installed"] == []
    assert any("Bad Tone" in e for e in res["errors"])
