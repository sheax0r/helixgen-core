import json
from pathlib import Path

from helixgen.device.manifest import SetlistManifest, MANIFEST_VERSION


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc))


def test_version_is_2():
    assert MANIFEST_VERSION == 2


def test_loads_native_v2(tmp_path):
    p = tmp_path / "setlists.json"
    _write(p, {
        "version": 2,
        "tones": {"A": {"path": "/x/a.hsp", "content_hash": "sha256:aa",
                        "doc": None, "source": "authored", "slot": "5A",
                        "device": {"cid": 10, "posi": 17}}},
        "setlists": {"helixgen": {"tones": ["A"], "synced": True}},
    })
    m = SetlistManifest.load(p)
    assert m.tones["A"]["slot"] == "5A"
    assert m.setlists_map["helixgen"] == {"tones": ["A"], "synced": True}


def test_migrates_v1_entries_and_list_setlists(tmp_path):
    p = tmp_path / "setlists.json"
    _write(p, {
        "version": 1,
        "tones": {"A": {"path": "/x/a.hsp", "content_hash": "sha256:aa", "source": "hsp"}},
        "setlists": {"user": ["A"], "helixgen": ["A"]},
        "observed": {"pool": {"A": {"cid": 10, "posi": 3}},
                     "setlists": {"helixgen": {"cid": 42, "refs": {"A": {"ref_cid": 99, "posi": 0}}}}},
        "entries": [{"setlist": "user", "posi": 3, "name": "A", "cid": 10,
                     "source_kind": "hsp", "source_path": "/x/a.hsp", "slot_label": "1D"}],
    })
    m = SetlistManifest.load(p)
    assert m.tones["A"]["slot"] == "1D"
    assert m.tones["A"]["device"] == {"cid": 10, "posi": 3}
    assert m.setlists_map["helixgen"] == {"tones": ["A"], "synced": True}
    assert m.setlists_map["user"]["synced"] is True
    m.save()
    on_disk = json.loads(p.read_text())
    assert on_disk["version"] == 2
    assert "entries" not in on_disk


def test_save_roundtrips_v2(tmp_path):
    p = tmp_path / "setlists.json"
    m = SetlistManifest(p)
    m.tones["A"] = {"path": None, "content_hash": None, "doc": None,
                    "source": "create", "slot": None, "device": None}
    m.setlists_map["draft"] = {"tones": ["A"], "synced": False}
    m.save()
    m2 = SetlistManifest.load(p)
    assert m2.tones["A"]["source"] == "create"
    assert m2.setlists_map["draft"] == {"tones": ["A"], "synced": False}
