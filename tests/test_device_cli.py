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


# -- auto-load IRs (_auto_upload_irs) -----------------------------------------

class _FakeMapping:
    def __init__(self, table):
        self._t = table  # hash -> path (or missing)

    @classmethod
    def _make(cls, table):
        m = cls(table)
        return m

    def resolve_by_hash(self, hh):
        if hh not in self._t:
            raise KeyError(hh)
        return self._t[hh]


def _patch_auto(monkeypatch, table, push_results):
    """Patch IrMapping.load + sftp.push_ir for _auto_upload_irs tests."""
    import helixgen.ir as _ir
    from helixgen.device import sftp as _sftp

    monkeypatch.setattr(_ir.IrMapping, "load",
                        classmethod(lambda cls: _FakeMapping(table)))
    calls = []

    def fake_push(ip, path, **kw):
        calls.append((ip, str(path)))
        return push_results.pop(0)

    monkeypatch.setattr(_sftp, "push_ir", fake_push)
    return calls


def test_auto_upload_irs_registered_ok(monkeypatch, capsys):
    from pathlib import Path
    from helixgen.cli import _auto_upload_irs
    _patch_auto(monkeypatch, {"aa11": Path("/irs/a.wav")},
                [{"ok": True, "registered": True, "hash_match": True,
                  "helixgen_hash": "aa11", "device_hash": "aa11",
                  "name": "a", "already": False}])
    _auto_upload_irs("1.2.3.4", ["aa11"])
    out = capsys.readouterr()
    assert "imported IR a (aa11)" in out.out
    assert "warning" not in (out.out + out.err).lower()


def test_auto_upload_irs_registered_but_hash_mismatch_warns(monkeypatch, capsys):
    """Registered instantly, but the device's hash != the preset's hash — the
    cab won't resolve, so warn (the irhash-algorithm edge case)."""
    from pathlib import Path
    from helixgen.cli import _auto_upload_irs
    _patch_auto(monkeypatch, {"aa11": Path("/irs/a.wav")},
                [{"ok": True, "registered": True, "hash_match": False,
                  "helixgen_hash": "aa11", "device_hash": "ZZZZ",
                  "name": "a", "already": False}])
    _auto_upload_irs("1.2.3.4", ["aa11"])
    err = capsys.readouterr().err
    assert "won't resolve" in err and "aa11" in err and "ZZZZ" in err


def test_auto_upload_irs_uploaded_not_yet_registered_warns(monkeypatch, capsys):
    from pathlib import Path
    from helixgen.cli import _auto_upload_irs
    _patch_auto(monkeypatch, {"aa11": Path("/irs/a.wav")},
                [{"ok": True, "registered": False, "helixgen_hash": "aa11",
                  "name": "a", "already": False}])
    _auto_upload_irs("1.2.3.4", ["aa11"])
    err = capsys.readouterr().err
    assert "not yet" in err and "aa11" in err


def test_auto_upload_irs_already_present(monkeypatch, capsys):
    from pathlib import Path
    from helixgen.cli import _auto_upload_irs
    _patch_auto(monkeypatch, {"bb22": Path("/irs/b.wav")},
                [{"ok": True, "already": True, "helixgen_hash": "bb22"}])
    _auto_upload_irs("1.2.3.4", ["bb22"])
    assert "already on device" in capsys.readouterr().out


def test_auto_upload_irs_not_registered_locally(monkeypatch, capsys):
    from helixgen.cli import _auto_upload_irs
    _patch_auto(monkeypatch, {}, [])  # resolve_by_hash raises -> skipped
    _auto_upload_irs("1.2.3.4", ["nope"])
    assert "not found locally" in capsys.readouterr().err


# -- device sync (bulk-sync a directory of .hsp tones) ------------------------

def test_device_sync_installs_and_prints_summary(tmp_path, monkeypatch):
    from helixgen.device import sync as _sync
    seen = {}
    summary = {
        "ok": True, "setlist": "user", "directory": str(tmp_path),
        "deleted": [{"name": "Old Tone", "cid": 900, "slot": "1A"}],
        "installed": [
            {"file": "a.hsp", "name": "Tone A", "pos": 1, "slot": "1B",
             "cid": 101, "irs": [{"hash": "aa"}]},
        ],
        "errors": [],
    }
    monkeypatch.setattr(_sync, "sync_library",
                        lambda directory, **kw: seen.update(directory=directory, **kw) or summary)
    result = CliRunner().invoke(cli, ["device", "sync", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "deleted 1A: 'Old Tone' (cid 900)" in result.output
    assert "installed 1B: 'Tone A' (cid 101)  (+1 IRs)" in result.output
    assert "mirrored 1 tone to user (1 removed)" in result.output
    assert seen["directory"] == str(tmp_path)
    assert seen["exclude_irs"] is False


def test_device_sync_exclude_irs_flag(tmp_path, monkeypatch):
    from helixgen.device import sync as _sync
    seen = {}
    monkeypatch.setattr(_sync, "sync_library",
                        lambda directory, **kw: seen.update(**kw) or
                        {"ok": True, "setlist": "user", "deleted": [], "installed": [], "errors": []})
    CliRunner().invoke(cli, ["device", "sync", str(tmp_path), "--exclude-irs"])
    assert seen["exclude_irs"] is True


def test_device_sync_defaults_to_preset_output_dir(monkeypatch, tmp_path):
    from helixgen.device import sync as _sync
    from helixgen import preferences as _prefs

    class _P:
        preset_output_dir = str(tmp_path)
    monkeypatch.setattr(_prefs, "load_preferences", lambda *a, **k: _P())
    seen = {}
    monkeypatch.setattr(_sync, "sync_library",
                        lambda directory, **kw: seen.update(directory=directory) or
                        {"ok": True, "setlist": "user", "deleted": [], "installed": [], "errors": []})
    result = CliRunner().invoke(cli, ["device", "sync"])
    assert result.exit_code == 0, result.output
    assert seen["directory"] == str(tmp_path)


def test_device_sync_no_dir_no_pref_errors(monkeypatch):
    from helixgen import preferences as _prefs

    class _P:
        preset_output_dir = None
    monkeypatch.setattr(_prefs, "load_preferences", lambda *a, **k: _P())
    result = CliRunner().invoke(cli, ["device", "sync"])
    assert result.exit_code != 0
    assert "preset_output_dir" in result.output
