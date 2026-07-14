from helixgen.device.manifest import SetlistManifest
from helixgen.device.setlist_sync import plan_mirror, assign_slots


def _mk(tmp_path, tones):
    m = SetlistManifest(tmp_path / "s.json")
    for name, slot in tones.items():
        m.tones[name] = {"path": f"/x/{name}.hsp", "content_hash": f"sha256:{name}",
                         "doc": None, "source": "authored", "slot": slot,
                         "device": None}
    return m


def test_plan_mirror_installs_updates_deletes_and_ignores_untracked(tmp_path):
    m = _mk(tmp_path, {"A": "5A", "B": "auto", "C": None})
    device = [
        {"name": "A", "posi": 4, "cid": 10, "content_hash": "sha256:A"},
        {"name": "C", "posi": 6, "cid": 12, "content_hash": "sha256:C"},
        {"name": "X", "posi": 7, "cid": 99, "content_hash": "sha256:X"},
    ]
    managed = set(m.tones)
    plan = plan_mirror(m, device, managed)
    assert "B" in [p["name"] for p in plan["install"]]
    assert "C" in [p["name"] for p in plan["delete"]]
    assert "A" in [p["name"] for p in plan["skip"]]
    assert all(p["name"] != "X" for b in plan.values() for p in b)


def test_plan_mirror_repushes_changed_hash(tmp_path):
    m = _mk(tmp_path, {"A": "5A"})
    device = [{"name": "A", "posi": 4, "cid": 10, "content_hash": "sha256:STALE"}]
    plan = plan_mirror(m, device, set(m.tones))
    assert [p["name"] for p in plan["repush"]] == ["A"]
    assert plan["repush"][0]["cid"] == 10


def test_assign_slots_avoids_untracked_and_occupied(tmp_path):
    m = _mk(tmp_path, {"B": "auto"})
    occupied = {"1A", "1B"}
    assigned = assign_slots(m, occupied)
    assert assigned["B"] == "1C"
    assert m.tones["B"]["slot"] == "1C"


def test_assign_slots_skips_concrete_managed_slots(tmp_path):
    m = _mk(tmp_path, {"A": "1A", "B": "auto"})
    assigned = assign_slots(m, set())
    assert assigned["B"] == "1B"  # 1A taken by A
