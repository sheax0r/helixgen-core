import pytest

from helixgen.device.manifest import SetlistManifest, ManifestError
from helixgen.hsp import write_hsp


def _hsp(dirpath, name):
    p = dirpath / f"{name}.hsp"
    write_hsp(p, {"meta": {"name": name}})
    return p


def test_register_tone_adds_offdevice(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    n = m.register_tone(_hsp(tmp_path, "Alpha"), source="authored")
    assert n == "Alpha"
    assert m.tones["Alpha"]["slot"] is None
    assert m.tones["Alpha"]["source"] == "authored"


def test_mark_on_device_and_unsync_cascade(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    m.add_to_setlist("helixgen", "Alpha")
    m.set_setlist_synced("helixgen", True)
    assert m.tones["Alpha"]["slot"] == "auto"
    pulled = m.unsync("Alpha")
    assert m.tones["Alpha"]["slot"] is None
    assert "helixgen" in pulled
    assert "Alpha" not in m.tones_in("helixgen")


def test_unsync_keeps_membership_in_unsynced_setlist(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    m.mark_on_device("Alpha")
    m.add_to_setlist("draft", "Alpha")
    m.unsync("Alpha")
    assert "Alpha" in m.tones_in("draft")


def test_register_duplicate_name_different_path_rejected(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    other = tmp_path / "sub"
    other.mkdir()
    p2 = other / "Alpha.hsp"
    write_hsp(p2, {"meta": {"name": "Alpha"}})
    with pytest.raises(ManifestError):
        m.register_tone(p2)


def test_add_to_synced_setlist_marks_on_device(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Beta"))
    m.create_setlist("live")
    m.set_setlist_synced("live", True)
    m.add_to_setlist("live", "Beta")
    assert m.tones["Beta"]["slot"] == "auto"
