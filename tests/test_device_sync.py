"""Unit tests for device.sync.sync_library (no real device).

``sync_library`` mirrors a library directory onto the target setlist: it deletes
every preset already in the setlist, then installs the library fresh. The library
on disk is the source of truth; only the target setlist is touched; and an empty
or unreadable library never deletes anything.
"""
from __future__ import annotations

import json

import pytest

from helixgen.device import sync as _sync
from helixgen.hsp import write_hsp


def _write_hsp(path, name):
    write_hsp(path, {"meta": {"name": name}, "preset": {"flow": [{}]}})


class FakeClient:
    """Stand-in for HelixClient: canned container contents + edit buffer."""

    def __init__(self, occupied=(), missing=()):
        # occupied: list of (posi, name) already on the device; give each a cid
        self._occupied = [(p, n, 900 + i) for i, (p, n) in enumerate(occupied)]
        self._missing = list(missing)
        self.deleted = []  # cids passed to delete()

    # production reaches the raw primitives via client._raw.<name>; on this
    # fake they live directly on the instance.
    @property
    def _raw(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_container(self, container):
        return [{"posi": p, "name": n, "cid_": c} for p, n, c in self._occupied]

    def delete(self, container, cids):
        cids = list(cids)
        self.deleted.extend(cids)
        drop = set(cids)
        self._occupied = [(p, n, c) for p, n, c in self._occupied if c not in drop]
        return True

    def device_ir_hashes(self):
        return set()  # nothing on device → all referenced IRs "missing"

    def load_preset(self, cid):
        pass

    def get_edit_buffer(self):
        return b"template-blob"


def _patch(monkeypatch, ledger_path, *, client, cids=(101, 102, 103),
           missing_by_default=()):
    # The slot ledger is folded into the setlist manifest file now, so its
    # `entries` land in $HELIXGEN_SETLISTS (one file), not device-slots.json.
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(ledger_path))
    monkeypatch.setattr(_sync, "HelixClient", lambda **k: client)
    it = iter(cids)
    monkeypatch.setattr(_sync.bridge, "install_recipe",
                        lambda *a, **k: next(it))
    monkeypatch.setattr(_sync.bridge, "check_irs",
                        lambda c, b: {"missing": list(missing_by_default), "present": set()})


def test_sync_mirrors_setlist_to_library(tmp_path, monkeypatch):
    d = tmp_path / "tones"; d.mkdir()
    _write_hsp(d / "a.hsp", "Tone A")
    _write_hsp(d / "b.hsp", "Tone B")
    ledger = tmp_path / "ledger.json"
    # device holds an unmanaged preset AND a stale copy of a library tone
    client = FakeClient(occupied=[(0, "Existing"), (1, "Tone A")])
    _patch(monkeypatch, ledger, client=client)

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)

    assert res["ok"] is True
    # BOTH existing presets deleted — library is authoritative (delete unmanaged,
    # overwrite managed == delete + reinstall)
    assert {e["name"] for e in res["deleted"]} == {"Existing", "Tone A"}
    assert sorted(client.deleted) == [900, 901]
    # library installed fresh into the now-empty slots (arbitrary fill order)
    assert [i["name"] for i in res["installed"]] == ["Tone A", "Tone B"]
    assert [i["pos"] for i in res["installed"]] == [0, 1]
    assert [i["slot"] for i in res["installed"]] == ["1A", "1B"]
    assert res["errors"] == []
    # ledger holds exactly the new placements (stale entries replaced)
    led = json.loads(ledger.read_text())
    recorded = {(e["name"], e["cid"]) for e in led["entries"]}
    assert recorded == {("Tone A", 101), ("Tone B", 102)}
    assert all(e["source_kind"] == "hsp" for e in led["entries"])


def test_sync_deletes_nothing_when_device_empty(tmp_path, monkeypatch):
    d = tmp_path / "tones"; d.mkdir()
    _write_hsp(d / "a.hsp", "Tone A")
    ledger = tmp_path / "ledger.json"
    client = FakeClient()  # empty device
    _patch(monkeypatch, ledger, client=client)

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)

    assert res["deleted"] == [] and client.deleted == []
    assert [i["name"] for i in res["installed"]] == ["Tone A"]


def test_sync_empty_library_leaves_device_untouched(tmp_path, monkeypatch):
    """Safety guard: an empty library must never wipe the device."""
    d = tmp_path / "empty"; d.mkdir()
    ledger = tmp_path / "ledger.json"
    client = FakeClient(occupied=[(0, "Keep Me")])
    _patch(monkeypatch, ledger, client=client)

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)

    assert res["installed"] == [] and res["deleted"] == []
    assert client.deleted == []  # device never contacted for deletion
    assert "nothing to mirror" in res["note"]


def test_sync_all_unreadable_leaves_device_untouched(tmp_path, monkeypatch):
    """If every .hsp fails to read, bail before deleting anything."""
    d = tmp_path / "tones"; d.mkdir()
    (d / "a.hsp").write_bytes(b"not a valid hsp")
    ledger = tmp_path / "ledger.json"
    client = FakeClient(occupied=[(0, "Keep Me")])
    _patch(monkeypatch, ledger, client=client)

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)

    assert res["ok"] is False
    assert res["installed"] == [] and res["deleted"] == []
    assert client.deleted == []
    assert res["errors"] and "read failed" in res["errors"][0]["error"]


def test_sync_uploads_referenced_irs_unless_excluded(tmp_path, monkeypatch):
    d = tmp_path / "tones"; d.mkdir()
    _write_hsp(d / "a.hsp", "Tone A")
    ledger = tmp_path / "ledger.json"
    _patch(monkeypatch, ledger, client=FakeClient(),
           missing_by_default=["aa11", "bb22"])
    calls = []
    monkeypatch.setattr(_sync, "_upload_missing_irs",
                        lambda ip, hashes: calls.append((ip, hashes)) or
                        [{"hash": h, "ok": True} for h in hashes])

    res = _sync.sync_library(str(d), ip="9.9.9.9", exclude_irs=False)
    assert calls == [("9.9.9.9", ["aa11", "bb22"])]
    assert len(res["installed"][0]["irs"]) == 2

    # with exclude_irs, no IR upload happens
    calls.clear()
    ledger2 = tmp_path / "ledger2.json"
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(ledger2))
    res2 = _sync.sync_library(str(d), ip="9.9.9.9", exclude_irs=True)
    assert calls == []
    assert all(i["irs"] == [] for i in res2["installed"])


def test_sync_bad_directory_raises(tmp_path):
    from helixgen.device import HelixError
    with pytest.raises(HelixError, match="not a directory"):
        _sync.sync_library(str(tmp_path / "nope"), ip="1.2.3.4")


def test_setlist_container_maps_names():
    from helixgen.device.client import USER, FACTORY, THROWAWAY
    assert _sync.setlist_container("user") == USER
    assert _sync.setlist_container("factory") == FACTORY
    assert _sync.setlist_container("throwaway") == THROWAWAY
    from helixgen.device import HelixError
    with pytest.raises(HelixError, match="unknown setlist"):
        _sync.setlist_container("bogus")
