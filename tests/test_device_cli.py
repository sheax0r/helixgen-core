"""CLI tests for the `helixgen device` command group.

These never touch a real device: they monkeypatch ``helixgen.device.HelixClient``
with a fake context manager whose methods return canned data.
"""
import json

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device import HelixError

CANNED_PRESETS = [
    {"cid_": 101, "name": "Clean Machine", "cctp": 1000, "posi": 0},
    {"cid_": 102, "name": "Lead Tone", "cctp": 1000, "posi": 1},
]


class FakeClient:
    """Stand-in for HelixClient. Records calls; returns canned data."""

    def __init__(self, *args, **kwargs):
        self.init_args = (args, kwargs)
        self.calls = []

    # context-manager protocol
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self):
        return self

    def close(self):
        pass

    # reads
    def list_presets(self, container=-2):
        self.calls.append(("list_presets", container))
        return CANNED_PRESETS

    def list_setlists(self):
        self.calls.append(("list_setlists",))
        return [{"cid_": -2, "name": "User"}, {"cid_": -1, "name": "Factory"}]

    def get_ref(self, cid):
        self.calls.append(("get_ref", cid))
        return {"cid_": cid, "name": "Lead Tone", "cpid": -2, "posi": 1}

    def get_edit_buffer(self):
        self.calls.append(("get_edit_buffer",))
        return b"_sbepgsm-fake-content-blob"

    # writes
    def load_preset(self, cid):
        self.calls.append(("load_preset", cid))
        return True

    def create_from(self, src_cid, container, pos):
        self.calls.append(("create_from", src_cid, container, pos))
        return 999

    def rename(self, cid, name):
        self.calls.append(("rename", cid, name))
        return True

    def delete(self, container, cids):
        self.calls.append(("delete", container, list(cids)))
        return True

    def set_param(self, path, block, param_id, value):
        self.calls.append(("set_param", path, block, param_id, value))
        return True


class RaisingClient(FakeClient):
    """A fake whose reads raise HelixError to exercise error paths."""

    def list_presets(self, container=-2):
        raise HelixError("boom: device unreachable")


def _patch_client(monkeypatch, cls):
    """Patch the HelixClient symbol commands import lazily from helixgen.device."""
    import helixgen.device as device_mod

    monkeypatch.setattr(device_mod, "HelixClient", cls)


def test_device_group_registers():
    result = CliRunner().invoke(cli, ["device", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "setlists", "read", "load", "create",
                "rename", "delete", "set-param", "pull"):
        assert sub in result.output


def test_device_list_json(monkeypatch):
    _patch_client(monkeypatch, FakeClient)
    result = CliRunner().invoke(cli, ["device", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == CANNED_PRESETS


def test_device_list_human(monkeypatch):
    _patch_client(monkeypatch, FakeClient)
    result = CliRunner().invoke(cli, ["device", "list"])
    assert result.exit_code == 0
    assert "cid=101" in result.output
    assert "Clean Machine" in result.output
    assert "1A" in result.output  # slot_label(0)


def test_device_rename_reports_success(monkeypatch):
    seen = {}

    class Recorder(FakeClient):
        def rename(self, cid, name):
            seen["args"] = (cid, name)
            return True

    _patch_client(monkeypatch, Recorder)
    result = CliRunner().invoke(cli, ["device", "rename", "102", "New Name"])
    assert result.exit_code == 0
    assert seen["args"] == (102, "New Name")
    assert "renamed" in result.output.lower()


def test_device_error_path_nonzero_exit(monkeypatch):
    _patch_client(monkeypatch, RaisingClient)
    result = CliRunner().invoke(cli, ["device", "list"])
    assert result.exit_code != 0
    assert "boom" in result.output


def test_device_setlist_maps_to_constant(monkeypatch):
    holder = {}

    class Recorder(FakeClient):
        def list_presets(self, container=-2):
            holder["container"] = container
            return CANNED_PRESETS

    _patch_client(monkeypatch, Recorder)
    result = CliRunner().invoke(cli, ["device", "list", "--setlist", "throwaway"])
    assert result.exit_code == 0
    assert holder["container"] == -5  # THROWAWAY


def test_device_pull_writes_blob(monkeypatch, tmp_path):
    _patch_client(monkeypatch, FakeClient)
    out = tmp_path / "backup.sbe"
    result = CliRunner().invoke(cli, ["device", "pull", "101", str(out)])
    assert result.exit_code == 0
    assert out.read_bytes() == b"_sbepgsm-fake-content-blob"
    assert "wrote" in result.output.lower()


def test_device_delete_requires_confirmation(monkeypatch):
    _patch_client(monkeypatch, FakeClient)
    # Decline the prompt -> aborted, nonzero exit.
    result = CliRunner().invoke(cli, ["device", "delete", "101"], input="n\n")
    assert result.exit_code != 0


def test_device_delete_yes_skips_prompt(monkeypatch):
    holder = {}

    class Recorder(FakeClient):
        def delete(self, container, cids):
            holder["args"] = (container, list(cids))
            return True

    _patch_client(monkeypatch, Recorder)
    result = CliRunner().invoke(cli, ["device", "delete", "101", "--yes"])
    assert result.exit_code == 0
    assert holder["args"] == (-2, [101])
