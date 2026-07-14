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


def test_rename_setlist_preserves_order_and_record(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.register_tone(_hsp(tmp_path, "Alpha"))
    m.create_setlist("first")
    m.add_to_setlist("mid", "Alpha")
    m.create_setlist("last")
    assert m.rename_setlist("mid", "gigs") is True
    assert m.setlists() == ["first", "gigs", "last"]
    assert m.tones_in("gigs") == ["Alpha"]
    # observed record follows too
    m.record_observed_setlist("first", 1, {})
    m.record_observed_setlist("gigs", 2, {"Alpha": {"ref_cid": 9, "posi": 0}})
    m.rename_setlist("gigs", "shows")
    assert "shows" in m.observed["setlists"]
    assert "gigs" not in m.observed["setlists"]


def test_rename_setlist_unknown_returns_false(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    assert m.rename_setlist("nope", "x") is False


def test_rename_setlist_collision_rejected(tmp_path):
    m = SetlistManifest(tmp_path / "s.json")
    m.create_setlist("a")
    m.create_setlist("b")
    with pytest.raises(ManifestError):
        m.rename_setlist("a", "b")
