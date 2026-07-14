"""Slot auto-assignment for the managed user population (assign_slots).

The mirror planning itself (slot-marked tones install with no setlist
membership, slot=None tones delete from the pool, never-orphan) is exercised
end-to-end against the fake client in ``test_setlist_sync.py``.
"""
from helixgen.device.manifest import SetlistManifest
from helixgen.device.setlist_sync import assign_slots


def _mk(tmp_path, tones):
    m = SetlistManifest(tmp_path / "s.json")
    for name, slot in tones.items():
        m.tones[name] = {"path": f"/x/{name}.hsp", "content_hash": f"sha256:{name}",
                         "doc": None, "source": "authored", "slot": slot,
                         "device": None}
    return m


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
