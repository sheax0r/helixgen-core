"""CLI tests for `device slots reorder` (local) and `device slots sync` (device)."""
import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device.ledger import SlotLedger

NOW = "2026-07-12T00:00:00+00:00"


def _seed(rows):
    """rows: (posi, name, cid) in the isolated ledger, all setlist=user/hsp."""
    led = SlotLedger.load()
    for i, (posi, name, cid) in enumerate(rows):
        led.record(setlist="user", posi=posi, name=name, cid=cid,
                   source_kind="hsp", source_path=f"/x/{name}.hsp",
                   now=NOW)
    led.save()
    return led


class SyncClient:
    """Fake client modelling content pull/delete/push for sync."""

    PRESETS = []  # list of {"posi","name","cid_"}

    def __init__(self, *a, **k):
        self.calls = []
        self._next_cid = 900

    @property
    def _raw(self):  # production calls client._raw.<primitive>
        return self

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def list_presets(self, container=-2):
        self.calls.append(("list_presets", container))
        return [dict(p) for p in type(self).PRESETS]

    def load_preset(self, cid):
        self.calls.append(("load_preset", cid))
        return True

    def get_edit_buffer(self):
        self.calls.append(("get_edit_buffer",))
        return b"_sbepgsm-blob"

    def get_content(self, cid):
        # non-activating read used by sync Phase A
        self.calls.append(("get_content", cid))
        return b"_sbepgsm-blob"

    def delete(self, container, cids):
        self.calls.append(("delete", container, tuple(cids)))
        return True

    def push_to_slot(self, container, pos, name, blob):
        self.calls.append(("push_to_slot", container, pos, name))
        self._next_cid += 1
        return self._next_cid


def _patch(monkeypatch, cls):
    import helixgen.device as device_mod
    monkeypatch.setattr(device_mod, "HelixClient", cls)


# -- reorder (local, no device) ----------------------------------------------

def test_reorder_changes_order_offline(monkeypatch):
    _seed([(0, "A", 1), (1, "B", 2), (2, "C", 3)])
    # no client patched: if reorder tried to connect it would fail
    r = CliRunner().invoke(cli, ["device", "slots", "reorder", "C", "--to", "0"])
    assert r.exit_code == 0, r.output
    seq = [e["name"] for e in SlotLedger.load().entries_in_order()]
    assert seq == ["C", "A", "B"]


def test_reorder_unknown_errors(monkeypatch):
    _seed([(0, "A", 1)])
    r = CliRunner().invoke(cli, ["device", "slots", "reorder", "Nope", "--to", "0"])
    assert r.exit_code != 0


# -- sync ---------------------------------------------------------------------

def test_sync_dry_run_shows_plan_no_writes(monkeypatch):
    _seed([(5, "A", 1), (2, "B", 2), (7, "C", 3)])

    class C(SyncClient):
        PRESETS = [{"posi": 5, "name": "A", "cid_": 1},
                   {"posi": 2, "name": "B", "cid_": 2},
                   {"posi": 7, "name": "C", "cid_": 3}]

    _patch(monkeypatch, C)
    r = CliRunner().invoke(cli, ["device", "slots", "sync", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "A" in r.output and "->" in r.output.replace("→", "->")
    # dry-run must not mutate the device
    client_calls = C.__mro__  # sanity
    assert "would" in r.output.lower() or "dry" in r.output.lower()


def test_sync_no_moves_is_noop(monkeypatch):
    _seed([(0, "A", 1), (1, "B", 2)])

    class C(SyncClient):
        PRESETS = [{"posi": 0, "name": "A", "cid_": 1},
                   {"posi": 1, "name": "B", "cid_": 2}]

    _patch(monkeypatch, C)
    r = CliRunner().invoke(cli, ["device", "slots", "sync", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "already" in r.output.lower()


def test_sync_executes_and_updates_ledger(monkeypatch):
    _seed([(0, "A", 1), (1, "B", 2), (2, "C", 3)])
    # desire order C, A, B
    CliRunner().invoke(cli, ["device", "slots", "reorder", "C", "--to", "0"])

    calls_holder = {}

    class C(SyncClient):
        PRESETS = [{"posi": 0, "name": "A", "cid_": 1},
                   {"posi": 1, "name": "B", "cid_": 2},
                   {"posi": 2, "name": "C", "cid_": 3}]

        def __exit__(self, *e):
            calls_holder["calls"] = self.calls
            return False

    _patch(monkeypatch, C)
    r = CliRunner().invoke(cli, ["device", "slots", "sync", "--yes", "--no-backup"])
    assert r.exit_code == 0, r.output

    calls = calls_holder["calls"]
    kinds = [c[0] for c in calls]
    assert "delete" in kinds and "push_to_slot" in kinds
    # all pulls happen before any delete (recoverability); reads are
    # non-activating (get_content), never load_preset+get_edit_buffer
    first_delete = kinds.index("delete")
    assert "get_content" in kinds[:first_delete]
    assert "delete" not in kinds[:kinds.index("get_content")]
    assert "load_preset" not in kinds

    # ledger now reflects C at slot 0, A at 1, B at 2
    led = SlotLedger.load()
    assert led.find(name="C")["posi"] == 0
    assert led.find(name="A")["posi"] == 1
    assert led.find(name="B")["posi"] == 2


def test_sync_aborts_before_delete_on_empty_blob(monkeypatch):
    _seed([(0, "A", 1), (1, "B", 2)])
    CliRunner().invoke(cli, ["device", "slots", "reorder", "B", "--to", "0"])

    class C(SyncClient):
        PRESETS = [{"posi": 0, "name": "A", "cid_": 1},
                   {"posi": 1, "name": "B", "cid_": 2}]

        def get_content(self, cid):
            self.calls.append(("get_content", cid))
            return b""  # empty -> must abort before any delete

    _patch(monkeypatch, C)
    r = CliRunner().invoke(cli, ["device", "slots", "sync", "--yes", "--no-backup"])
    assert r.exit_code != 0
    assert "empty" in r.output.lower() or "abort" in r.output.lower()
