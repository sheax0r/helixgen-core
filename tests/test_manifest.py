"""Tests for the setlist manifest (device/manifest.py).

Pure local-file logic — NO device/network. The manifest folds the old slot
ledger into a desired-state (tones + ordered setlist membership) + observed-state
model backing ``~/.helixgen/setlists.json`` (override ``$HELIXGEN_SETLISTS``).
"""
import hashlib
import json
from pathlib import Path

import pytest

from helixgen.hsp import write_hsp
from helixgen.device.manifest import (
    MANIFEST_VERSION,
    ManifestError,
    SetlistManifest,
    default_setlists_path,
)


# -- helpers ------------------------------------------------------------------

def _make_hsp(path: Path, name: str, *, extra: str = "") -> Path:
    """Write a minimal real .hsp whose meta.name is ``name``.

    ``extra`` perturbs the body bytes (to change the content hash) without
    touching the name.
    """
    body = {"meta": {"name": name}, "_": extra}
    write_hsp(path, body)
    return path


def _sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point both env overrides at tmp so nothing touches the real home dir."""
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "setlists.json"))
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "device-slots.json"))


# -- default path -------------------------------------------------------------

def test_default_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "custom.json"))
    assert default_setlists_path() == tmp_path / "custom.json"


def test_default_path_home_fallback(monkeypatch):
    monkeypatch.delenv("HELIXGEN_SETLISTS", raising=False)
    assert default_setlists_path() == Path.home() / ".helixgen" / "setlists.json"


# -- empty load / save round-trip --------------------------------------------

def test_empty_load_when_absent(tmp_path):
    m = SetlistManifest.load(tmp_path / "none.json")
    assert m.setlists() == []
    assert m.tones_in("anything") == []


def test_empty_save_reload_roundtrip(tmp_path):
    path = tmp_path / "m.json"
    m = SetlistManifest.load(path)
    m.save()
    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == MANIFEST_VERSION
    assert on_disk["tones"] == {}
    assert on_disk["setlists"] == {}
    assert on_disk["observed"] == {"pool": {}, "setlists": {}}

    reloaded = SetlistManifest.load(path)
    assert reloaded.setlists() == []


# -- add_tone -----------------------------------------------------------------

def test_add_tone_reads_meta_name_and_hashes(tmp_path):
    hsp = _make_hsp(tmp_path / "wl.hsp", "White Limo Lead")
    m = SetlistManifest.load(tmp_path / "m.json")
    name = m.add_tone("helixgen", hsp)

    assert name == "White Limo Lead"
    assert m.tones_in("helixgen") == ["White Limo Lead"]
    assert m.tone_path("White Limo Lead") == str(hsp.resolve())
    assert m.content_hash("White Limo Lead") == _sha256_of(hsp)
    # registry source tag persisted
    m.save()
    reg = json.loads((tmp_path / "m.json").read_text())["tones"]["White Limo Lead"]
    assert reg["source"] == "import-local"


def test_add_tone_falls_back_to_filename_stem(tmp_path):
    hsp = _make_hsp(tmp_path / "Fallback_Tone.hsp", "")  # empty meta.name
    m = SetlistManifest.load(tmp_path / "m.json")
    name = m.add_tone("helixgen", hsp)
    assert name == "Fallback_Tone"


def test_add_tone_appends_membership_in_order(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Alpha")
    b = _make_hsp(tmp_path / "b.hsp", "Beta")
    c = _make_hsp(tmp_path / "c.hsp", "Gamma")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl", a)
    m.add_tone("sl", c)
    m.add_tone("sl", b, pos=1)  # insert between
    assert m.tones_in("sl") == ["Alpha", "Beta", "Gamma"]


def test_add_tone_no_duplicate_membership(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Alpha")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl", a)
    m.add_tone("sl", a)
    assert m.tones_in("sl") == ["Alpha"]


def test_add_tone_unique_name_collision_raises(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Same Name")
    b = _make_hsp(tmp_path / "b.hsp", "Same Name")  # different path, same name
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl", a)
    with pytest.raises(ManifestError):
        m.add_tone("sl", b)


def test_add_tone_same_path_updates_hash(tmp_path):
    hsp = _make_hsp(tmp_path / "t.hsp", "Tone")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl", hsp)
    first = m.content_hash("Tone")

    _make_hsp(hsp, "Tone", extra="CHANGED BYTES")  # same name, new bytes
    m.add_tone("sl", hsp)
    second = m.content_hash("Tone")
    assert first != second
    assert second == _sha256_of(hsp)
    assert m.tones_in("sl") == ["Tone"]  # still no dupe


# -- remove_tone --------------------------------------------------------------

def test_remove_tone_drops_membership_and_gcs_registry(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Alpha")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl", a)
    assert m.remove_tone("sl", "Alpha") is True
    assert m.tones_in("sl") == []
    assert m.tone_path("Alpha") is None  # registry entry garbage-collected


def test_remove_tone_miss_returns_false(tmp_path):
    m = SetlistManifest.load(tmp_path / "m.json")
    assert m.remove_tone("sl", "nope") is False


def test_remove_from_one_setlist_survives_in_other(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Shared")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl1", a)
    m.add_tone("sl2", a)
    assert m.remove_tone("sl1", "Shared") is True
    # still referenced by sl2 -> registry entry survives
    assert m.tones_in("sl1") == []
    assert m.tones_in("sl2") == ["Shared"]
    assert m.tone_path("Shared") == str(a.resolve())


# -- setlist management -------------------------------------------------------

def test_create_setlist_and_list(tmp_path):
    m = SetlistManifest.load(tmp_path / "m.json")
    m.create_setlist("empty")
    assert "empty" in m.setlists()
    assert m.tones_in("empty") == []


def test_create_setlist_idempotent(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Alpha")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl", a)
    m.create_setlist("sl")  # must not wipe existing membership
    assert m.tones_in("sl") == ["Alpha"]


# -- pathless add -------------------------------------------------------------

def test_pathless_add_has_no_path_or_hash(tmp_path):
    m = SetlistManifest.load(tmp_path / "m.json")
    m.pathless_add("Live Buffer Tone", "save", setlist="sl")
    assert m.tone_path("Live Buffer Tone") is None
    assert m.content_hash("Live Buffer Tone") is None
    assert m.tones_in("sl") == ["Live Buffer Tone"]
    m.save()
    reg = json.loads((tmp_path / "m.json").read_text())["tones"]["Live Buffer Tone"]
    assert reg["source"] == "save"
    assert reg["path"] is None
    assert reg["content_hash"] is None


# -- union_tones --------------------------------------------------------------

def test_union_tones_dedups_preserving_order(tmp_path):
    a = _make_hsp(tmp_path / "a.hsp", "Alpha")
    b = _make_hsp(tmp_path / "b.hsp", "Beta")
    c = _make_hsp(tmp_path / "c.hsp", "Gamma")
    m = SetlistManifest.load(tmp_path / "m.json")
    m.add_tone("sl1", a)
    m.add_tone("sl1", b)
    m.add_tone("sl2", b)  # dup across setlists
    m.add_tone("sl2", c)
    assert m.union_tones(["sl1", "sl2"]) == ["Alpha", "Beta", "Gamma"]


# -- observed -----------------------------------------------------------------

def test_record_and_clear_observed(tmp_path):
    m = SetlistManifest.load(tmp_path / "m.json")
    m.record_observed_pool("Alpha", cid=1000, posi=3)
    m.record_observed_setlist("sl", cid=42, refs={"Alpha": {"ref_cid": 1003, "posi": 0}})
    m.save()
    obs = json.loads((tmp_path / "m.json").read_text())["observed"]
    assert obs["pool"]["Alpha"] == {"cid": 1000, "posi": 3}
    assert obs["setlists"]["sl"]["cid"] == 42
    assert obs["setlists"]["sl"]["refs"]["Alpha"] == {"ref_cid": 1003, "posi": 0}

    m.clear_observed()
    assert json.loads(json.dumps(m.to_dict()))["observed"] == {"pool": {}, "setlists": {}}


def test_observed_pool_synced_hash_roundtrip(tmp_path):
    m = SetlistManifest.load(tmp_path / "m.json")
    # without a synced_hash the entry omits it and the reader returns None
    m.record_observed_pool("Alpha", cid=1000, posi=0)
    assert m.observed_pool_hash("Alpha") is None
    assert "synced_hash" not in m.observed["pool"]["Alpha"]

    # with a synced_hash it is stored and read back, surviving a save/reload
    m.record_observed_pool("Beta", cid=1001, posi=1, synced_hash="sha256:deadbeef")
    assert m.observed_pool_hash("Beta") == "sha256:deadbeef"
    m.save()
    reloaded = SetlistManifest.load(tmp_path / "m.json")
    assert reloaded.observed_pool_hash("Beta") == "sha256:deadbeef"
    assert reloaded.observed_pool_hash("Alpha") is None
    # unknown preset -> None
    assert m.observed_pool_hash("Nonexistent") is None


# -- migration from device-slots.json ----------------------------------------

def _ledger_fixture(path: Path, entries) -> Path:
    """Write a device-slots.json matching ledger.py's real schema."""
    path.write_text(json.dumps({"version": 1, "entries": entries}, indent=2))
    return path


def test_migration_from_device_slots(tmp_path, monkeypatch):
    hsp = _make_hsp(tmp_path / "wl.hsp", "White Limo Lead")
    slots = tmp_path / "device-slots.json"
    _ledger_fixture(slots, [
        {
            "order": 0, "name": "White Limo Lead", "setlist": "user", "posi": 1,
            "slot_label": "1B", "cid": 1000, "source_kind": "hsp",
            "source_path": str(hsp), "model": "stadium_xl",
            "created_at": "2026-07-12T00:00:00+00:00",
            "updated_at": "2026-07-12T00:00:00+00:00",
        },
        {
            "order": 1, "name": "Buffer Save", "setlist": "user", "posi": 0,
            "slot_label": "1A", "cid": 1001, "source_kind": "edit-buffer",
            "source_path": None, "model": "stadium_xl",
            "created_at": "2026-07-12T00:00:00+00:00",
            "updated_at": "2026-07-12T00:00:00+00:00",
        },
    ])
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(slots))

    manifest_path = tmp_path / "setlists.json"  # absent -> triggers migration
    m = SetlistManifest.load(manifest_path)

    # tones registry
    assert m.tone_path("White Limo Lead") == str(hsp)
    assert m.content_hash("White Limo Lead") == _sha256_of(hsp)  # hsp source recomputed
    assert m.tone_path("Buffer Save") is None
    assert m.content_hash("Buffer Save") is None  # non-hsp -> no hash

    # membership in posi order (posi 0 before posi 1)
    assert m.tones_in("user") == ["Buffer Save", "White Limo Lead"]

    # observed placement
    d = m.to_dict()
    assert d["observed"]["pool"]["White Limo Lead"] == {"cid": 1000, "posi": 1}
    assert d["observed"]["setlists"]["user"]["refs"]["White Limo Lead"] == {
        "ref_cid": None, "posi": 1,
    }

    # old file left in place, untouched
    assert slots.exists()
    assert json.loads(slots.read_text())["version"] == 1


def test_no_migration_when_manifest_present(tmp_path, monkeypatch):
    # both files exist -> manifest wins, ledger ignored
    _ledger_fixture(tmp_path / "device-slots.json", [
        {"order": 0, "name": "FromLedger", "setlist": "user", "posi": 0,
         "slot_label": "1A", "cid": 1, "source_kind": "hsp",
         "source_path": None, "model": None},
    ])
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "device-slots.json"))

    manifest_path = tmp_path / "setlists.json"
    manifest_path.write_text(json.dumps({
        "version": MANIFEST_VERSION, "tones": {}, "setlists": {"user": []},
        "observed": {"pool": {}, "setlists": {}},
    }))
    m = SetlistManifest.load(manifest_path)
    assert "FromLedger" not in [t for s in m.setlists() for t in m.tones_in(s)]
    assert m.setlists() == ["user"]


def test_load_corrupt_is_empty(tmp_path):
    p = tmp_path / "m.json"
    p.write_text("{ not json")
    m = SetlistManifest.load(p)
    assert m.setlists() == []


def test_load_unknown_version_is_empty(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"version": 999, "tones": {"x": {}}}))
    m = SetlistManifest.load(p)
    assert m.tone_path("x") is None
