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

    # production reaches the raw primitives via client._raw.<name>; on this
    # fake they live directly on the instance.
    @property
    def _raw(self):
        return self

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

    def get_content(self, cid):
        # non-activating read used by `pull` / `backup` / sync Phase A
        self.calls.append(("get_content", cid))
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


def test_device_pull_is_non_activating(monkeypatch, tmp_path):
    # pull must read via the non-activating get_content and NEVER load_preset
    captured = {}

    class Recorder(FakeClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured["client"] = self

    _patch_client(monkeypatch, Recorder)
    out = tmp_path / "backup.sbe"
    result = CliRunner().invoke(cli, ["device", "pull", "101", str(out)])
    assert result.exit_code == 0
    calls = captured["client"].calls
    assert ("get_content", 101) in calls
    assert not any(c[0] == "load_preset" for c in calls)


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


# -- device setlist: local manifest membership --------------------------------

def _fresh_manifest_env(monkeypatch, tmp_path):
    """Point the manifest + legacy ledger at empty tmp paths so `load()` starts
    empty and never reads the real user's ~/.helixgen files."""
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "setlists.json"))
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "device-slots.json"))


def _make_hsp(path, name):
    from helixgen.hsp import write_hsp
    write_hsp(path, {"meta": {"name": name}})
    return path


def test_device_setlist_group_registers(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    result = CliRunner().invoke(cli, ["device", "setlist", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "add", "remove", "create-local"):
        assert sub in result.output


def test_device_setlist_add_and_list(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    hsp = _make_hsp(tmp_path / "tone.hsp", "White Limo Lead")
    add = CliRunner().invoke(cli, ["device", "setlist", "add", "helixgen", str(hsp)])
    assert add.exit_code == 0, add.output
    assert "White Limo Lead" in add.output

    lst = CliRunner().invoke(cli, ["device", "setlist", "list"])
    assert lst.exit_code == 0
    assert "helixgen  (1 tone)" in lst.output
    assert "White Limo Lead" in lst.output


def test_device_setlist_list_json(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    hsp = _make_hsp(tmp_path / "tone.hsp", "Tone X")
    CliRunner().invoke(cli, ["device", "setlist", "add", "helixgen", str(hsp)])
    lst = CliRunner().invoke(cli, ["device", "setlist", "list", "--json"])
    assert lst.exit_code == 0
    doc = json.loads(lst.output)
    assert doc["setlists"] == {"helixgen": ["Tone X"]}


def test_device_setlist_remove(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    hsp = _make_hsp(tmp_path / "tone.hsp", "Tone X")
    CliRunner().invoke(cli, ["device", "setlist", "add", "helixgen", str(hsp)])
    rm = CliRunner().invoke(cli, ["device", "setlist", "remove", "helixgen", "Tone X"])
    assert rm.exit_code == 0
    assert "removed" in rm.output.lower()
    # gone now
    rm2 = CliRunner().invoke(cli, ["device", "setlist", "remove", "helixgen", "Tone X"])
    assert rm2.exit_code != 0


def test_device_setlist_create_local(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli, ["device", "setlist", "create-local", "helixgen"])
    assert res.exit_code == 0
    lst = CliRunner().invoke(cli, ["device", "setlist", "list"])
    assert "helixgen  (0 tones)" in lst.output


# -- device sync (manifest-driven, reference-based) ---------------------------

def _patch_sync(monkeypatch, seen, result=None):
    """Stub `setlist_sync.sync_setlists` to record kwargs and return canned."""
    from helixgen.device import setlist_sync as _ss
    canned = result or {
        "ok": True, "setlists": ["helixgen"],
        "pool": {"installed": ["Tone A"], "updated": [], "skipped": ["Tone B"]},
        "references": {"helixgen": {"added": [9000], "removed": []}},
        "gc": {"deleted": []}, "irs": [], "errors": [],
    }
    monkeypatch.setattr(_ss, "sync_setlists",
                        lambda manifest, **kw: seen.update(kw) or canned)


def test_device_sync_one_setlist_prints_summary(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "helixgen"])
    assert res.exit_code == 0, res.output
    assert seen["setlists"] == ["helixgen"]
    assert seen["gc"] is False
    assert "pool: 1 installed, 0 updated, 1 skipped" in res.output
    assert "setlist 'helixgen': +1 references, -0 references" in res.output
    assert "synced 1 setlist(s): helixgen" in res.output


def test_device_sync_all_flag(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "--all", "--gc"])
    assert res.exit_code == 0, res.output
    assert seen["setlists"] is None
    assert seen["gc"] is True


def test_device_sync_requires_exactly_one_of_setlist_or_all(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    # neither
    r1 = CliRunner().invoke(cli, ["device", "sync"])
    assert r1.exit_code != 0
    assert "exactly one" in r1.output
    # both
    r2 = CliRunner().invoke(cli, ["device", "sync", "helixgen", "--all"])
    assert r2.exit_code != 0
    assert "exactly one" in r2.output
    assert seen == {}  # engine never called


def test_device_sync_gc_ignored_without_all(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "helixgen", "--gc"])
    assert res.exit_code == 0, res.output
    assert "ignored" in res.output  # warning surfaced (stderr merged by CliRunner)
    assert seen["gc"] is False


def test_device_sync_json_passthrough(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "helixgen", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["ok"] is True


def test_device_sync_errors_surface(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen, result={
        "ok": False, "setlists": [], "pool": {"installed": [], "updated": [], "skipped": []},
        "references": {}, "gc": {"deleted": []}, "irs": [],
        "errors": ["setlist 'helixgen' not found on device; create it in the Stadium app first"],
    })
    res = CliRunner().invoke(cli, ["device", "sync", "helixgen"])
    assert res.exit_code == 0, res.output
    assert "not found on device" in res.output
