"""CLI tests for the `helixgen device slots` group (list / --verify / restore)."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device.ledger import SlotLedger

HSP_MAGIC = b"rpshnosj"

NOW = "2026-07-12T00:00:00+00:00"


def _seed_ledger(**overrides):
    """Write a ledger with one hsp-sourced entry to the isolated path."""
    led = SlotLedger.load()
    kw = dict(setlist="user", posi=12, name="White Limo Lead", cid=147,
              source_kind="hsp", source_path="/x/white-limo.hsp", now=NOW)
    kw.update(overrides)
    led.record(**kw)
    led.save()
    return led


class FakeClient:
    def __init__(self, *a, **k):
        self.calls = []
        self.presets = getattr(type(self), "PRESETS", [])

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def list_presets(self, container=-2):
        self.calls.append(("list_presets", container))
        return self.presets

    def find_by_pos(self, container, pos):
        return None

    def load_preset(self, cid):
        return True

    def get_edit_buffer(self):
        return b"_sbepgsm-template"

    def push_to_slot(self, container, pos, name, blob):
        self.calls.append(("push_to_slot", container, pos, name))
        return 900


def _patch_client(monkeypatch, cls=FakeClient):
    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", cls)


# -- list (offline) -----------------------------------------------------------

def test_slots_list_bare_prints_entries(monkeypatch):
    _seed_ledger()
    r = CliRunner().invoke(cli, ["device", "slots"])
    assert r.exit_code == 0, r.output
    assert "4A" in r.output
    assert "White Limo Lead" in r.output


def test_slots_list_explicit_and_json(monkeypatch):
    _seed_ledger()
    r = CliRunner().invoke(cli, ["device", "slots", "list", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data[0]["name"] == "White Limo Lead"
    assert data[0]["slot_label"] == "4A"


def test_slots_list_empty_is_graceful(monkeypatch):
    r = CliRunner().invoke(cli, ["device", "slots", "list"])
    assert r.exit_code == 0, r.output


# -- verify (needs device) ----------------------------------------------------

def test_slots_verify_flags_missing(monkeypatch):
    _seed_ledger()  # entry at user/12, cid 147

    class Empty(FakeClient):
        PRESETS = []  # device has nothing -> missing

    _patch_client(monkeypatch, Empty)
    r = CliRunner().invoke(cli, ["device", "slots", "list", "--verify"])
    assert r.exit_code == 0, r.output
    assert "missing" in r.output.lower()


def test_slots_verify_ok(monkeypatch):
    _seed_ledger()

    class Match(FakeClient):
        PRESETS = [{"posi": 12, "name": "White Limo Lead", "cid_": 147}]

    _patch_client(monkeypatch, Match)
    r = CliRunner().invoke(cli, ["device", "slots", "list", "--verify", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data[0]["status"] == "ok"


# -- restore ------------------------------------------------------------------

def test_slots_restore_sbe_source_repushes(monkeypatch, tmp_path):
    sbe = tmp_path / "lead.sbe"
    sbe.write_bytes(b"_sbepgsm-blob")
    _seed_ledger(name="Lead", posi=5, source_kind="sbe", source_path=str(sbe))

    holder = {}

    class Rec(FakeClient):
        def push_to_slot(self, container, pos, name, blob):
            holder["push"] = (container, pos, name)
            return 901

    _patch_client(monkeypatch, Rec)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Lead"])
    assert r.exit_code == 0, r.output
    assert holder["push"][1] == 5  # re-pushed to the recorded slot


def test_slots_restore_hsp_source_reinstalls(monkeypatch, tmp_path):
    hsp = tmp_path / "white-limo.hsp"
    hsp.write_bytes(HSP_MAGIC + json.dumps({"meta": {"name": "t"},
                                            "preset": {"flow": []}}).encode())
    _seed_ledger(name="White Limo Lead", posi=12, source_kind="hsp",
                 source_path=str(hsp))

    import helixgen.device.bridge as bridge
    monkeypatch.setattr(bridge, "check_irs", lambda h, body: {"missing": set()})
    called = {}
    def _install(h, body, container, pos, name, blob, strict=True):
        called["install"] = (container, pos, name)
        return 950
    monkeypatch.setattr(bridge, "install_recipe", _install)

    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "White Limo Lead"])
    assert r.exit_code == 0, r.output
    assert called["install"][1] == 12


def test_slots_restore_no_local_source_errors(monkeypatch):
    _seed_ledger(name="Live Tweak", posi=3, source_kind="edit-buffer",
                 source_path=None)
    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Live Tweak"])
    assert r.exit_code != 0
    assert "no local source" in r.output.lower()


def test_slots_restore_unknown_name_errors(monkeypatch):
    _seed_ledger(name="Known", posi=1)
    _patch_client(monkeypatch)
    r = CliRunner().invoke(cli, ["device", "slots", "restore", "Nonexistent"])
    assert r.exit_code != 0
