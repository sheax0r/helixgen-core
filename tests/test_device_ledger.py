"""Tests for the device slot ledger (device/ledger.py).

Pure-module tests — no device. The ledger records which tone helixgen put in
which device slot, so placements can be listed offline, drift-checked, and
restored.
"""
import json
from pathlib import Path

import pytest

from helixgen.device.ledger import (
    LEDGER_VERSION,
    SlotLedger,
    default_ledger_path,
)


def _rec(led, **kw):
    """record() with sensible defaults for terse tests."""
    base = dict(setlist="user", posi=0, name="Tone", cid=100,
                source_kind="hsp", source_path="/x/tone.hsp", now="2026-07-12T00:00:00+00:00")
    base.update(kw)
    return led.record(**base)


# -- default path -------------------------------------------------------------

def test_default_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "custom.json"))
    assert default_ledger_path() == tmp_path / "custom.json"


def test_default_path_home_fallback(monkeypatch):
    monkeypatch.delenv("HELIXGEN_DEVICE_SLOTS", raising=False)
    assert default_ledger_path() == Path.home() / ".helixgen" / "device-slots.json"


# -- record / upsert / order --------------------------------------------------

def test_record_appends_with_order_and_slot_label(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    e = _rec(led, posi=12, name="White Limo", cid=147)
    assert e["order"] == 0
    assert e["slot_label"] == "4A"  # posi 12 -> 4A
    assert e["setlist"] == "user" and e["posi"] == 12
    assert e["source_kind"] == "hsp" and e["source_path"] == "/x/tone.hsp"
    assert e["created_at"] == "2026-07-12T00:00:00+00:00"


def test_record_multiple_get_increasing_order(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=0, name="A")
    _rec(led, posi=1, name="B")
    _rec(led, posi=2, name="C")
    orders = [e["order"] for e in led.entries_in_order()]
    assert orders == [0, 1, 2]


def test_record_same_slot_upserts_keeps_order(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=5, name="Old", cid=1)
    _rec(led, posi=6, name="Other", cid=2)
    _rec(led, posi=5, name="New", cid=9, now="2026-07-12T01:00:00+00:00")
    slot5 = led.find(setlist="user", posi=5)
    assert slot5["name"] == "New" and slot5["cid"] == 9
    assert slot5["order"] == 0  # order preserved on upsert
    assert slot5["updated_at"] == "2026-07-12T01:00:00+00:00"
    assert len(led.entries) == 2  # upsert, not append


# -- rename / remove ----------------------------------------------------------

def test_rename_by_cid(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=3, name="Before", cid=42)
    assert led.rename(cid=42, new_name="After") is True
    assert led.find(setlist="user", posi=3)["name"] == "After"


def test_rename_miss_returns_false(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    assert led.rename(cid=999, new_name="X") is False


def test_remove_by_cid_and_redensify(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=0, name="A", cid=1)
    _rec(led, posi=1, name="B", cid=2)
    _rec(led, posi=2, name="C", cid=3)
    assert led.remove(cid=2) is True
    remaining = [(e["name"], e["order"]) for e in led.entries_in_order()]
    assert remaining == [("A", 0), ("C", 1)]  # order re-densified 0..1


def test_remove_miss_returns_false(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    assert led.remove(cid=5) is False


# -- persistence / tolerance --------------------------------------------------

def test_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "l.json"
    led = SlotLedger.load(path)
    _rec(led, posi=4, name="Persist", cid=7)
    led.save()

    reloaded = SlotLedger.load(path)
    assert reloaded.find(setlist="user", posi=4)["name"] == "Persist"
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == LEDGER_VERSION
    assert isinstance(on_disk["entries"], list)


def test_load_missing_is_empty(tmp_path):
    assert SlotLedger.load(tmp_path / "nope.json").entries == []


def test_load_corrupt_is_empty(tmp_path):
    p = tmp_path / "l.json"
    p.write_text("{ not json")
    assert SlotLedger.load(p).entries == []


def test_load_unknown_version_is_empty(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"version": 999, "entries": [{"name": "x"}]}))
    assert SlotLedger.load(p).entries == []


def test_save_is_atomic_on_failure(tmp_path, monkeypatch):
    import helixgen.device.ledger as mod
    path = tmp_path / "l.json"
    led = SlotLedger.load(path)
    _rec(led, posi=0, name="Good", cid=1)
    led.save()
    original = path.read_bytes()

    monkeypatch.setattr(mod.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    _rec(led, posi=1, name="New", cid=2)
    with pytest.raises(OSError):
        led.save()
    assert path.read_bytes() == original
    assert not (tmp_path / "l.json.tmp").exists()


# -- verify (pure over device state) -----------------------------------------

def _dev(setlist, posi, name, cid):
    return {"setlist": setlist, "posi": posi, "name": name, "cid_": cid}


def test_verify_ok_and_missing_and_changed(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=0, name="Alpha", cid=10)
    _rec(led, posi=1, name="Beta", cid=11)
    _rec(led, posi=2, name="Gamma", cid=12)

    device = [
        _dev("user", 0, "Alpha", 10),      # ok
        _dev("user", 1, "Somebody Else", 99),  # changed (slot reused)
        # posi 2 absent -> missing
    ]
    status = {r.get("name"): r["status"] for r in led.verify(device) if r["status"] != "untracked"}
    assert status["Alpha"] == "ok"
    assert status["Beta"] == "changed"
    assert status["Gamma"] == "missing"


def test_verify_detects_moved(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=0, name="Alpha", cid=10)
    # same cid now lives at a different posi
    device = [_dev("user", 5, "Alpha", 10)]
    result = led.verify(device)
    moved = [r for r in result if r.get("name") == "Alpha"][0]
    assert moved["status"] == "moved"


def test_verify_flags_untracked(tmp_path):
    led = SlotLedger.load(tmp_path / "l.json")
    _rec(led, posi=0, name="Alpha", cid=10)
    device = [
        _dev("user", 0, "Alpha", 10),
        _dev("user", 7, "Mystery", 77),  # not in ledger
    ]
    untracked = [r for r in led.verify(device) if r["status"] == "untracked"]
    assert len(untracked) == 1
    assert untracked[0]["name"] == "Mystery"
    assert untracked[0]["slot_label"] == "2D"  # posi 7 -> 2D
