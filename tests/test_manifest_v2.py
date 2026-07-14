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


def test_slot_universe_covers_xl_128_banks(tmp_path):
    # Regression: the Stadium XL user bank goes to 128D (512 slots). A prior
    # hardcode of 8 banks (32 slots) threw an artificial "device full".
    from helixgen.device.manifest import _SLOT_LABELS, _posi_to_slot
    assert _SLOT_LABELS[-1] == "128D"
    assert len(_SLOT_LABELS) == 512
    assert _posi_to_slot(511) == "128D"


def test_mark_on_device_accepts_high_slot(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.tones["A"] = {"path": None, "content_hash": None, "doc": None,
                    "source": "authored", "slot": None, "device": None}
    m.mark_on_device("A", "100C")   # far beyond the old 8-bank ceiling
    assert m.tones["A"]["slot"] == "100C"


def test_assign_slots_handles_more_than_32_tones(tmp_path):
    from helixgen.device.setlist_sync import assign_slots
    m = SetlistManifest(tmp_path / "s.json")
    for i in range(40):  # >32: would have raised "device full" under 8 banks
        m.tones[f"T{i}"] = {"path": None, "content_hash": None, "doc": None,
                            "source": "authored", "slot": "auto", "device": None}
    assigned = assign_slots(m, occupied=set())
    assert len(assigned) == 40
    assert m.tones["T39"]["slot"] == "10D"   # 40th free slot (posi 39)
