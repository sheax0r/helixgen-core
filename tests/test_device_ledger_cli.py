"""CLI tests: device write-path commands record placements in the slot ledger.

Never touch a real device — monkeypatch ``helixgen.device.HelixClient`` with a
fake, and (for install) the ``bridge`` content functions. The ledger is
isolated per-test via the autouse ``HELIXGEN_DEVICE_SLOTS`` conftest fixture.
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device.ledger import SlotLedger

HSP_MAGIC = b"rpshnosj"


class FakeClient:
    """Stand-in HelixClient covering the write-path methods."""

    def __init__(self, *args, **kwargs):
        self.calls = []

    @property
    def _raw(self):  # production calls client._raw.<primitive>
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def find_by_pos(self, container, pos):
        return None  # slot empty

    def get_edit_buffer(self):
        return b"_sbepgsm-template"

    def load_preset(self, cid):
        return True

    def save_edit_buffer_to(self, container, pos, name):
        self.calls.append(("save_edit_buffer_to", container, pos, name))
        return 501

    def push_to_slot(self, container, pos, name, blob):
        self.calls.append(("push_to_slot", container, pos, name))
        return 502

    def create_from(self, src_cid, container, pos):
        self.calls.append(("create_from", src_cid, container, pos))
        return 503

    def rename(self, cid, name):
        return True

    def delete(self, container, cids):
        return True


def _patch_client(monkeypatch):
    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", FakeClient)


def _ledger():
    return SlotLedger.load()  # honors HELIXGEN_DEVICE_SLOTS from the autouse fixture


# -- save ---------------------------------------------------------------------

def test_save_records_placement(monkeypatch):
    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "save", "Clean Verse",
                                 "--setlist", "user", "--pos", "4"])
    assert r.exit_code == 0, r.output
    e = _ledger().find(setlist="user", posi=4)
    assert e is not None
    assert e["name"] == "Clean Verse"
    assert e["cid"] == 501
    assert e["source_kind"] == "edit-buffer"
    assert e["source_path"] is None
    assert e["slot_label"] == "2A"  # posi 4 -> 2A


# -- push ---------------------------------------------------------------------

def test_push_records_placement(monkeypatch, tmp_path):
    _patch_client(monkeypatch)
    sbe = tmp_path / "backup.sbe"
    sbe.write_bytes(b"_sbepgsm-blob")
    r = CliRunner().invoke(cli, ["device", "push", str(sbe), "Restored Lead",
                                 "--setlist", "user", "--pos", "5"])
    assert r.exit_code == 0, r.output
    e = _ledger().find(setlist="user", posi=5)
    assert e["name"] == "Restored Lead"
    assert e["cid"] == 502
    assert e["source_kind"] == "sbe"
    assert Path(e["source_path"]).name == "backup.sbe"


# -- create -------------------------------------------------------------------

def test_create_records_placement(monkeypatch):
    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "create", "--from", "101",
                                 "--setlist", "user", "--pos", "6"])
    assert r.exit_code == 0, r.output
    e = _ledger().find(setlist="user", posi=6)
    assert e["cid"] == 503
    assert e["source_kind"] == "copy"


# -- install ------------------------------------------------------------------

def test_install_records_placement(monkeypatch, tmp_path):
    _patch_client(monkeypatch)
    # stub the heavy content bridge — we're testing the ledger hook, not bridge
    import helixgen.device.bridge as bridge
    monkeypatch.setattr(bridge, "check_irs", lambda h, body: {"missing": set()})
    monkeypatch.setattr(bridge, "install_recipe",
                        lambda h, body, container, pos, name, blob, strict=True: 777)

    hsp = tmp_path / "White Limo Lead.hsp"
    hsp.write_bytes(HSP_MAGIC + json.dumps({"meta": {"name": "t"},
                                            "preset": {"flow": []}}).encode())
    r = CliRunner().invoke(cli, ["device", "install", str(hsp), "White Limo Lead",
                                 "--setlist", "user", "--pos", "12"])
    assert r.exit_code == 0, r.output
    e = _ledger().find(setlist="user", posi=12)
    assert e["name"] == "White Limo Lead"
    assert e["cid"] == 777
    assert e["source_kind"] == "hsp"
    assert Path(e["source_path"]).name == "White Limo Lead.hsp"
    assert e["slot_label"] == "4A"


# -- rename / delete keep the ledger in sync ----------------------------------

def test_rename_updates_ledger(monkeypatch):
    _patch_client(monkeypatch)
    CliRunner().invoke(cli, ["device", "save", "Old Name", "--pos", "0"])
    e = _ledger().find(setlist="user", posi=0)
    cid = e["cid"]

    r = CliRunner().invoke(cli, ["device", "rename", str(cid), "New Name"])
    assert r.exit_code == 0, r.output
    assert _ledger().find(setlist="user", posi=0)["name"] == "New Name"


def test_delete_removes_from_ledger(monkeypatch):
    _patch_client(monkeypatch)
    CliRunner().invoke(cli, ["device", "save", "Doomed", "--pos", "0"])
    cid = _ledger().find(setlist="user", posi=0)["cid"]

    r = CliRunner().invoke(cli, ["device", "delete", str(cid), "--yes"])
    assert r.exit_code == 0, r.output
    assert _ledger().find(setlist="user", posi=0) is None
