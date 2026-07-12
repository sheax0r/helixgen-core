"""Unit tests for device.sync.sync_library (no real device)."""
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
        # occupied: list of (posi, name) already on the device
        self._occupied = list(occupied)
        self._missing = list(missing)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_container(self, container):
        return [{"posi": p, "name": n} for p, n in self._occupied]

    def device_ir_hashes(self):
        return set()  # nothing on device → all referenced IRs "missing"

    def load_preset(self, cid):
        pass

    def get_edit_buffer(self):
        return b"template-blob"


def _patch(monkeypatch, ledger_path, *, client, cids=(101, 102, 103),
           missing_by_default=()):
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(ledger_path))
    monkeypatch.setattr(_sync, "HelixClient", lambda **k: client)
    it = iter(cids)
    monkeypatch.setattr(_sync.bridge, "install_recipe",
                        lambda *a, **k: next(it))
    monkeypatch.setattr(_sync.bridge, "check_irs",
                        lambda c, b: {"missing": list(missing_by_default), "present": set()})


def test_sync_fills_empty_slots_and_records_ledger(tmp_path, monkeypatch):
    d = tmp_path / "tones"; d.mkdir()
    _write_hsp(d / "a.hsp", "Tone A")
    _write_hsp(d / "b.hsp", "Tone B")
    ledger = tmp_path / "ledger.json"
    # device already has slot 0 occupied
    _patch(monkeypatch, ledger, client=FakeClient(occupied=[(0, "Existing")]))

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)

    assert res["ok"] is True
    # skipped occupied slot 0, filled the next empties 1 and 2
    assert [i["pos"] for i in res["installed"]] == [1, 2]
    assert [i["name"] for i in res["installed"]] == ["Tone A", "Tone B"]
    assert [i["slot"] for i in res["installed"]] == ["1B", "1C"]
    assert res["skipped"] == [] and res["errors"] == []
    # ledger persisted both placements
    led = json.loads(ledger.read_text())
    recorded = {(e["posi"], e["name"], e["cid"]) for e in led["entries"]}
    assert recorded == {(1, "Tone A", 101), (2, "Tone B", 102)}
    assert all(e["source_kind"] == "hsp" for e in led["entries"])


def test_sync_is_idempotent_skips_by_name(tmp_path, monkeypatch):
    d = tmp_path / "tones"; d.mkdir()
    _write_hsp(d / "a.hsp", "Tone A")
    _write_hsp(d / "b.hsp", "Tone B")
    ledger = tmp_path / "ledger.json"
    # "Tone A" already on the device → skip it, install only "Tone B"
    _patch(monkeypatch, ledger, client=FakeClient(occupied=[(0, "Tone A")]))

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)

    assert [i["name"] for i in res["installed"]] == ["Tone B"]
    assert res["skipped"] == [{"file": "a.hsp", "name": "Tone A",
                               "reason": "already on device"}]


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
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(ledger2))
    _write_hsp(d / "c.hsp", "Tone C")  # fresh name so it's not skipped
    res2 = _sync.sync_library(str(d), ip="9.9.9.9", exclude_irs=True)
    assert calls == []
    assert all(i["irs"] == [] for i in res2["installed"])


def test_sync_reports_when_no_empty_slot(tmp_path, monkeypatch):
    d = tmp_path / "tones"; d.mkdir()
    _write_hsp(d / "a.hsp", "Tone A")
    ledger = tmp_path / "ledger.json"
    # every slot occupied
    full = [(p, f"P{p}") for p in range(_sync.SETLIST_CAPACITY)]
    _patch(monkeypatch, ledger, client=FakeClient(occupied=full))

    res = _sync.sync_library(str(d), ip="1.2.3.4", exclude_irs=True)
    assert res["ok"] is False
    assert res["installed"] == []
    assert res["errors"][0]["error"] == "no empty slot left in setlist"


def test_sync_empty_directory(tmp_path, monkeypatch):
    d = tmp_path / "empty"; d.mkdir()
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "l.json"))
    res = _sync.sync_library(str(d), ip="1.2.3.4")
    assert res["ok"] and res["installed"] == []
    assert "no .hsp" in res["note"]


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
