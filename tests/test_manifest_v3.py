"""Manifest v3 (intent-only) + v2->v3 auto-migration (device/manifest.py §3).

The manifest holds only committed INTENT (path/content_hash/source/slot +
setlists). Per-tone ``device`` and the top-level ``observed`` section move to
``devices/<serial>.json`` (device/observations.py). Loading a v2 manifest
migrates automatically: ``.bak-v2`` backup, strip ``doc``/``device``/
``observed``, preserve the old observed data into ``devices/legacy.json``, and
(from the legacy path) save to the new location + rename the legacy file.
"""
from __future__ import annotations

import json

import pytest

from helixgen.device.manifest import MANIFEST_VERSION, SetlistManifest


V2_DOC = {
    "version": 2,
    "tones": {
        "ToneA": {"path": "/x/tone-a.hsp", "content_hash": "sha256:aa",
                  "doc": "/x/tone-a.md", "source": "authored", "slot": "1A",
                  "device": {"cid": 1085, "posi": 0}},
        "ToneB": {"path": None, "content_hash": None, "doc": None,
                  "source": "save", "slot": "auto", "device": None},
    },
    "setlists": {
        "gigs": {"tones": ["ToneA"], "synced": False},
        "draft": {"tones": ["ToneB"], "synced": False},
    },
    "observed": {"pool": {"ToneA": {"cid": 1085, "posi": 0,
                                    "synced_hash": "sha256:aa"}},
                 "setlists": {"gigs": {"cid": 42, "refs": {}}}},
}


def test_version_is_3():
    assert MANIFEST_VERSION == 3


def test_v2_migrates_to_v3_and_new_location(tmp_home):
    legacy = tmp_home / "setlists.json"
    legacy.write_text(json.dumps(V2_DOC))

    m = SetlistManifest.load()
    assert m.version == 3

    # saved to the NEW manifest_path() location, intent-only
    saved = json.loads((tmp_home / "setlists" / "manifest.json").read_text())
    assert saved["version"] == 3
    assert "observed" not in saved
    assert "device" not in saved["tones"]["ToneA"]
    assert "doc" not in saved["tones"]["ToneA"]
    assert saved["tones"]["ToneA"]["slot"] == "1A"
    assert saved["tones"]["ToneA"]["source"] == "authored"

    # observed data preserved into devices/legacy.json
    legacy_obs = json.loads((tmp_home / "devices" / "legacy.json").read_text())
    assert legacy_obs["serial"] == "legacy"
    assert legacy_obs["tones"]["ToneA"] == {"cid": 1085, "posi": 0}
    assert legacy_obs["pool"]["ToneA"]["synced_hash"] == "sha256:aa"
    assert legacy_obs["setlists"]["gigs"]["cid"] == 42

    # a .bak-v2 backup of the old file was written before rewriting
    assert (tmp_home / "setlists.json.bak-v2").exists()
    assert json.loads((tmp_home / "setlists.json.bak-v2").read_text())["version"] == 2

    # legacy file renamed so a re-run does not re-migrate
    assert (tmp_home / "setlists.json.migrated-v2").exists()
    assert not legacy.exists()

    # the observed->synced flip is baked into intent (gigs was observed)
    assert saved["setlists"]["gigs"]["synced"] is True
    assert saved["setlists"]["draft"]["synced"] is False


def test_v2_migration_is_idempotent_rerun(tmp_home):
    (tmp_home / "setlists.json").write_text(json.dumps(V2_DOC))
    SetlistManifest.load()
    # second load reads the already-migrated v3 file, no re-migration
    m2 = SetlistManifest.load()
    assert m2.version == 3
    assert m2.tones["ToneA"]["slot"] == "1A"
    # loading v3 directly must not touch the legacy/backup files further
    assert (tmp_home / "setlists" / "manifest.json").exists()


def test_v2_in_place_override_migrates_and_backs_up(tmp_home, monkeypatch):
    # With $HELIXGEN_SETLISTS pointing at a concrete file (the common test/CI
    # case), the manifest resolves there directly and migrates IN PLACE.
    target = tmp_home / "custom.json"
    target.write_text(json.dumps(V2_DOC))
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(target))

    m = SetlistManifest.load()
    assert m.version == 3
    saved = json.loads(target.read_text())
    assert saved["version"] == 3
    assert "observed" not in saved
    # backup written next to the in-place file
    assert (tmp_home / "custom.json.bak-v2").exists()
    # in-place migration does NOT create a .migrated-v2 rename
    assert not (tmp_home / "custom.json.migrated-v2").exists()
    # observations still land in devices/legacy.json
    assert (tmp_home / "devices" / "legacy.json").exists()


def test_v3_round_trip(tmp_home):
    path = tmp_home / "setlists" / "manifest.json"
    path.parent.mkdir(parents=True)
    m = SetlistManifest(path)
    m.tones["ToneA"] = {"path": "/x/a.hsp", "content_hash": "sha256:a",
                        "source": "authored", "slot": "1A"}
    m.tones["ToneB"] = {"path": None, "content_hash": None,
                        "source": "save", "slot": "auto", "auto_marked": True}
    m.setlists_map["gigs"] = {"tones": ["ToneA"], "synced": True}
    m.save()

    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 3
    assert "observed" not in on_disk
    assert "device" not in on_disk["tones"]["ToneA"]
    assert "doc" not in on_disk["tones"]["ToneA"]

    m2 = SetlistManifest.load(path)
    assert m2.tones["ToneA"]["slot"] == "1A"
    assert m2.tones["ToneB"]["auto_marked"] is True
    assert m2.setlists_map["gigs"] == {"tones": ["ToneA"], "synced": True}


def test_register_tone_has_no_doc_kwarg(tmp_home):
    import inspect
    sig = inspect.signature(SetlistManifest.register_tone)
    assert "doc" not in sig.parameters


def test_v3_tone_record_has_no_doc_or_device(tmp_home):
    from helixgen.hsp import write_hsp
    hsp = tmp_home / "t.hsp"
    write_hsp(hsp, {"meta": {"name": "T"}})
    m = SetlistManifest(tmp_home / "m.json")
    m.register_tone(hsp, source="authored")
    rec = m.tones["T"]
    assert "doc" not in rec
    assert "device" not in rec
    assert set(rec) <= {"path", "content_hash", "source", "slot", "auto_marked"}


def test_v1_chain_migrates_to_v3(tmp_home):
    # v1 -> v2 -> v3 chain still works: entries become slots, observed pool
    # becomes device observations, list setlists become {tones, synced}.
    v1 = {
        "version": 1,
        "tones": {"A": {"path": "/x/a.hsp", "content_hash": "sha256:aa",
                        "source": "hsp"}},
        "setlists": {"user": ["A"], "helixgen": ["A"]},
        "observed": {"pool": {"A": {"cid": 10, "posi": 3}},
                     "setlists": {"helixgen": {"cid": 42, "refs": {}}}},
        "entries": [{"setlist": "user", "posi": 3, "name": "A", "cid": 10,
                     "source_kind": "hsp", "source_path": "/x/a.hsp",
                     "slot_label": "1D"}],
    }
    (tmp_home / "setlists.json").write_text(json.dumps(v1))

    m = SetlistManifest.load()
    assert m.version == 3
    assert m.tones["A"]["slot"] == "1D"
    assert "device" not in m.tones["A"]
    assert m.setlists_map["helixgen"]["synced"] is True
    assert m.setlists_map["user"]["synced"] is True

    # v1's observed pool cid/posi preserved into devices/legacy.json
    legacy_obs = json.loads((tmp_home / "devices" / "legacy.json").read_text())
    assert legacy_obs["tones"]["A"] == {"cid": 10, "posi": 3}
    # v1 backup written
    assert (tmp_home / "setlists.json.bak-v1").exists()


def test_fresh_empty_v3_when_nothing_present(tmp_home):
    m = SetlistManifest.load()
    assert m.version == 3
    assert m.setlists() == []
    assert m.tones == {}
    # a truly fresh load writes nothing until save()
    assert not (tmp_home / "setlists" / "manifest.json").exists()
    assert not (tmp_home / "devices").exists()


def test_to_dict_omits_observed(tmp_home):
    m = SetlistManifest(tmp_home / "m.json")
    d = m.to_dict()
    assert d["version"] == 3
    assert "observed" not in d
    assert set(d) == {"version", "tones", "setlists"}
