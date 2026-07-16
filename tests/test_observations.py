"""Tests for per-device observed state (device/observations.py).

Pure local-file logic: no device, no network. Observations live in
``~/.helixgen/devices/<serial>.json`` and are rebuilt wholesale by every sync.
"""
from __future__ import annotations

import json
import os

import pytest

from helixgen import home
from helixgen.device.observations import (
    DeviceObservations,
    load_observations,
    lookup_name_by_cid,
    lookup_tone,
    save_observations,
)


def test_missing_file_loads_empty(tmp_home):
    obs = load_observations("SN-ABSENT")
    assert obs.serial == "SN-ABSENT"
    assert obs.tones == {}
    assert obs.pool == {}
    assert obs.setlists == {}


def test_observations_save_load_lookup(tmp_home):
    obs = DeviceObservations(serial="SN-1")
    obs.record_pool("Tone A", cid=5000, posi=0, synced_hash="sha256:a")
    obs.record_setlist("gigs", 42, {"Tone A": {"ref_cid": 9, "posi": 0}})
    save_observations(obs)

    # file written under devices/<serial>.json
    path = home.devices_dir() / "SN-1.json"
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 1
    assert on_disk["serial"] == "SN-1"
    assert on_disk["tones"]["Tone A"] == {"cid": 5000, "posi": 0}
    assert on_disk["pool"]["Tone A"]["synced_hash"] == "sha256:a"
    assert on_disk["setlists"]["gigs"]["cid"] == 42

    # round-trip
    back = load_observations("SN-1")
    assert back.pool_hash("Tone A") == "sha256:a"
    assert back.tone_placement("Tone A") == {"cid": 5000, "posi": 0}

    # cross-device lookup finds it
    assert lookup_tone("Tone A") == {"cid": 5000, "posi": 0}
    assert lookup_tone("Nope") is None
    assert lookup_name_by_cid(5000) == "Tone A"
    assert lookup_name_by_cid(9999) is None


def test_pool_hash_without_synced_hash_is_none(tmp_home):
    obs = DeviceObservations(serial="SN-2")
    obs.record_pool("Alpha", cid=1, posi=0)
    assert obs.pool_hash("Alpha") is None
    assert "synced_hash" not in obs.pool["Alpha"]
    assert obs.pool_hash("Missing") is None


def test_clear_pool_forgets_both_maps(tmp_home):
    obs = DeviceObservations(serial="SN-3")
    obs.record_pool("Alpha", cid=1, posi=0, synced_hash="sha256:x")
    obs.clear_pool("Alpha")
    assert "Alpha" not in obs.pool
    assert "Alpha" not in obs.tones


def test_lookup_prefers_newest_file(tmp_home):
    # Two devices both know "Shared" at different cids; the newest-modified
    # file wins (a live device's observations are the freshest).
    old = DeviceObservations(serial="OLD")
    old.record_pool("Shared", cid=100, posi=1)
    save_observations(old)

    new = DeviceObservations(serial="NEW")
    new.record_pool("Shared", cid=200, posi=2)
    save_observations(new)

    # make NEW strictly newer than OLD
    old_path = home.devices_dir() / "OLD.json"
    new_path = home.devices_dir() / "NEW.json"
    os.utime(old_path, (1_000, 1_000))
    os.utime(new_path, (2_000, 2_000))

    assert lookup_tone("Shared") == {"cid": 200, "posi": 2}
    assert lookup_name_by_cid(200) == "Shared"


def test_save_is_atomic_no_partial_temp(tmp_home):
    obs = DeviceObservations(serial="SN-4")
    obs.record_pool("A", cid=1, posi=0)
    save_observations(obs)
    # no leftover .tmp
    leftovers = list(home.devices_dir().glob("*.tmp"))
    assert leftovers == []


def test_serial_with_unsafe_chars_is_sanitized(tmp_home):
    obs = DeviceObservations(serial="ip-192.168.4.84")
    obs.record_pool("A", cid=1, posi=0)
    save_observations(obs)
    assert (home.devices_dir() / "ip-192.168.4.84.json").exists()

    # a serial with a path separator can never escape devices/
    obs2 = DeviceObservations(serial="../../evil")
    obs2.record_pool("B", cid=2, posi=0)
    save_observations(obs2)
    files = {p.name for p in home.devices_dir().glob("*.json")}
    assert any(name != "ip-192.168.4.84.json" for name in files)
    # nothing landed outside devices/
    assert not (home.helixgen_home().parent / "evil.json").exists()


def test_corrupt_file_loads_empty(tmp_home):
    home.devices_dir().mkdir(parents=True, exist_ok=True)
    (home.devices_dir() / "SN-BAD.json").write_text("{ not json")
    obs = load_observations("SN-BAD")
    assert obs.tones == {}
