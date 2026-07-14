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

    def mutating(self):
        import contextlib
        return contextlib.nullcontext(self)

    # reads
    def list_presets(self, container=-2, *, strict=False):
        self.calls.append(("list_presets", container))
        return CANNED_PRESETS

    def list_setlists(self, *, strict=False):
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

    # slot-emptiness gate (#40) + the writes it guards
    def find_by_pos(self, container, pos, *, strict=False):
        self.calls.append(("find_by_pos", container, pos, strict))
        return None

    def push_to_slot(self, container, pos, name, blob):
        self.calls.append(("push_to_slot", container, pos, name))
        return 900

    def save_edit_buffer_to(self, container, pos, name):
        self.calls.append(("save_edit_buffer_to", container, pos, name))
        return 901


class RaisingClient(FakeClient):
    """A fake whose reads raise HelixError to exercise error paths."""

    def list_presets(self, container=-2, *, strict=False):
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
        def list_presets(self, container=-2, *, strict=False):
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


def test_auto_upload_irs_no_mapping_aborts_command(monkeypatch, capsys):
    """A broken/unreadable local mapping.json aborts the whole `--auto-irs`
    install with a ClickException (matching the original upfront-check
    behavior) — and since the no_mapping outcome applies identically to
    every hash, nothing is echoed per-hash first."""
    import click
    import pytest
    from helixgen.cli import _auto_upload_irs
    import helixgen.ir as _ir

    def _broken_load(cls):
        raise OSError("mapping.json is corrupt")

    monkeypatch.setattr(_ir.IrMapping, "load", classmethod(_broken_load))

    with pytest.raises(click.ClickException,
                       match="mapping.json") as excinfo:
        _auto_upload_irs("1.2.3.4", ["aa11", "bb22"])
    assert "--auto-irs needs your local IR mapping.json" in str(excinfo.value)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # aborts before any per-hash echo


def test_auto_upload_irs_push_error_aborts_command(monkeypatch, capsys):
    """A hard push_ir failure (e.g. a dropped connection) still aborts the
    whole `--auto-irs` install with a ClickException — unlike `device sync`,
    which tolerates a per-IR failure and keeps going, a preset shouldn't
    silently install missing one of its referenced IRs."""
    import click
    import pytest
    from pathlib import Path
    from helixgen.cli import _auto_upload_irs
    from helixgen.device import HelixError
    import helixgen.ir as _ir
    from helixgen.device import sftp as _sftp

    monkeypatch.setattr(_ir.IrMapping, "load",
                        classmethod(lambda cls: _FakeMapping(
                            {"aa11": Path("/irs/a.wav")})))

    def _raise(ip, path, **kw):
        raise HelixError("connection dropped")

    monkeypatch.setattr(_sftp, "push_ir", _raise)

    with pytest.raises(click.ClickException, match="connection dropped"):
        _auto_upload_irs("1.2.3.4", ["aa11"])
    # the failure is echoed as a warning before the command aborts
    assert "connection dropped" in capsys.readouterr().err


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
    assert doc["setlists"] == {"helixgen": {"tones": ["Tone X"], "synced": False}}


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


def test_device_sync_repush_flag_threads_through(monkeypatch, tmp_path):
    # #25 residual: `device sync <setlist> --repush` forces content refresh
    # of already-synced tones (hash-based change detection never re-pushes
    # a tone whose .hsp is unchanged but whose transcoder output would differ).
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "helixgen", "--repush"])
    assert res.exit_code == 0, res.output
    assert seen["repush"] is True


def test_device_sync_repush_defaults_false(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "helixgen"])
    assert res.exit_code == 0, res.output
    assert seen["repush"] is False


def test_device_sync_repush_with_all(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    seen = {}
    _patch_sync(monkeypatch, seen)
    res = CliRunner().invoke(cli, ["device", "sync", "--all", "--repush"])
    assert res.exit_code == 0, res.output
    assert seen["setlists"] is None
    assert seen["repush"] is True


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


# -- IR maintenance: delete-ir / rename-ir / ir-prune -------------------------

CANNED_IRS = [
    {"cid_": 1159, "name": "YA KW 412 M25 Mix 05", "hash": "aa" * 16,
     "mono": False, "posi": 0},
    {"cid_": 1160, "name": "ZZC-test", "hash": "bb" * 16,
     "mono": False, "posi": 1},
]


class IrClient(FakeClient):
    deleted = []

    def list_irs(self, strict=False):
        self.calls.append(("list_irs",))
        return [dict(m) for m in CANNED_IRS]

    def delete_irs(self, cids):
        IrClient.deleted.append(list(cids))
        return True


def test_device_delete_ir_by_name_with_yes(monkeypatch):
    IrClient.deleted = []
    _patch_client(monkeypatch, IrClient)
    result = CliRunner().invoke(
        cli, ["device", "delete-ir", "zzc-test", "--yes"])
    assert result.exit_code == 0, result.output
    assert IrClient.deleted == [[1160]]
    assert "ZZC-test" in result.output


def test_device_delete_ir_requires_confirmation(monkeypatch):
    IrClient.deleted = []
    _patch_client(monkeypatch, IrClient)
    result = CliRunner().invoke(
        cli, ["device", "delete-ir", "ZZC-test"], input="n\n")
    assert result.exit_code != 0
    assert IrClient.deleted == []


def test_device_delete_ir_unknown_errors(monkeypatch):
    _patch_client(monkeypatch, IrClient)
    result = CliRunner().invoke(cli, ["device", "delete-ir", "nope", "--yes"])
    assert result.exit_code != 0
    assert "no device IR" in result.output


def test_device_rename_ir(monkeypatch):
    seen = {}

    class Recorder(IrClient):
        def rename(self, cid, name):
            seen["args"] = (cid, name)
            return True

    _patch_client(monkeypatch, Recorder)
    result = CliRunner().invoke(
        cli, ["device", "rename-ir", "ZZC-test", "ZZC-renamed"])
    assert result.exit_code == 0, result.output
    assert seen["args"] == (1160, "ZZC-renamed")


def test_device_ir_prune_dry_run_default(monkeypatch):
    from helixgen.device import maintenance as mt
    seen = {}
    canned = {
        "ok": True, "dry_run": True, "device_irs": 2,
        "referenced": [{"cid_": 1159, "name": "used", "hash": "aa" * 16,
                        "presets": ["P"]}],
        "protected": [], "deleted": [],
        "orphans": [{"cid_": 1160, "name": "ZZC-test", "hash": "bb" * 16}],
        "errors": [],
    }
    monkeypatch.setattr(mt, "ir_prune", lambda **kw: seen.update(kw) or canned)
    result = CliRunner().invoke(cli, ["device", "ir-prune"])
    assert result.exit_code == 0, result.output
    assert seen["execute"] is False and seen["force"] is False
    assert "dry-run" in result.output.lower()
    assert "ZZC-test" in result.output


def test_device_ir_prune_yes_and_json(monkeypatch):
    from helixgen.device import maintenance as mt
    seen = {}
    canned = {"ok": True, "dry_run": False, "device_irs": 2, "referenced": [],
              "protected": [], "orphans": [{"cid_": 1160, "name": "Z",
                                            "hash": "bb" * 16}],
              "deleted": [{"cid_": 1160, "name": "Z", "hash": "bb" * 16}],
              "errors": []}
    monkeypatch.setattr(mt, "ir_prune", lambda **kw: seen.update(kw) or canned)
    result = CliRunner().invoke(
        cli, ["device", "ir-prune", "--yes", "--only", "Z", "--json"])
    assert result.exit_code == 0, result.output
    assert seen["execute"] is True and seen["only"] == "Z"
    assert json.loads(result.output)["deleted"][0]["name"] == "Z"


# -- preset color / notes: set-info -------------------------------------------

def test_device_set_info_batch_color(monkeypatch):
    from helixgen.device import maintenance as mt
    calls = []
    _patch_client(monkeypatch, FakeClient)
    monkeypatch.setattr(
        mt, "set_preset_info",
        lambda client, cid, **kw: calls.append((cid, kw)) or {"color": True})
    result = CliRunner().invoke(
        cli, ["device", "set-info", "101", "102", "--color", "red"])
    assert result.exit_code == 0, result.output
    assert calls == [(101, {"color": "red", "notes": None}),
                     (102, {"color": "red", "notes": None})]


def test_device_set_info_requires_color_or_notes(monkeypatch):
    _patch_client(monkeypatch, FakeClient)
    result = CliRunner().invoke(cli, ["device", "set-info", "101"])
    assert result.exit_code != 0
    assert "--color" in result.output or "--notes" in result.output


def test_device_set_info_notes(monkeypatch):
    from helixgen.device import maintenance as mt
    calls = []
    _patch_client(monkeypatch, FakeClient)
    monkeypatch.setattr(
        mt, "set_preset_info",
        lambda client, cid, **kw: calls.append((cid, kw)) or {"notes": True})
    result = CliRunner().invoke(
        cli, ["device", "set-info", "101", "--notes", "hello there"])
    assert result.exit_code == 0, result.output
    assert calls == [(101, {"color": None, "notes": "hello there"})]


# -- device-side setlist create / rename / delete / duplicate -----------------

class SetlistClient(FakeClient):
    """Fake with the device-side setlist surface."""

    SETLISTS = {"helixgen": 988, "Mike": 1014}
    created = []
    deleted = []
    duplicated = []

    def resolve_setlist_cid(self, name):
        self.calls.append(("resolve", name))
        return type(self).SETLISTS.get(name)

    def create_setlist(self, name, pos=None):
        type(self).created.append(name)
        return 1186

    def delete_setlist(self, cid):
        type(self).deleted.append(cid)
        return True

    def duplicate_setlist_refs(self, src, dst):
        type(self).duplicated.append((src, dst))
        return 3


def _reset_setlist_client():
    SetlistClient.created = []
    SetlistClient.deleted = []
    SetlistClient.duplicated = []


def test_device_setlist_create_on_device(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()
    _patch_client(monkeypatch, SetlistClient)
    result = CliRunner().invoke(cli, ["device", "setlist", "create", "ZZC-new"])
    assert result.exit_code == 0, result.output
    assert SetlistClient.created == ["ZZC-new"]
    assert "1186" in result.output


def test_device_setlist_create_existing_errors(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()
    _patch_client(monkeypatch, SetlistClient)
    result = CliRunner().invoke(cli, ["device", "setlist", "create", "helixgen"])
    assert result.exit_code != 0
    assert "already exists" in result.output
    assert SetlistClient.created == []


def test_device_setlist_rename_device_and_manifest(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()
    seen = {}

    class Recorder(SetlistClient):
        def rename(self, cid, name):
            seen["args"] = (cid, name)
            return True

    # seed a manifest record under the old name
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    m.create_setlist("helixgen")
    m.save()

    _patch_client(monkeypatch, Recorder)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "rename", "helixgen", "gigs"])
    assert result.exit_code == 0, result.output
    assert seen["args"] == (988, "gigs")
    m2 = SetlistManifest.load()
    assert "gigs" in m2.setlists() and "helixgen" not in m2.setlists()


def test_device_setlist_rename_unknown_errors(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()
    _patch_client(monkeypatch, SetlistClient)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "rename", "nope", "x"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_device_setlist_delete_confirms_and_deletes(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()
    _patch_client(monkeypatch, SetlistClient)
    refused = CliRunner().invoke(
        cli, ["device", "setlist", "delete", "Mike"], input="n\n")
    assert refused.exit_code != 0 and SetlistClient.deleted == []
    result = CliRunner().invoke(
        cli, ["device", "setlist", "delete", "Mike", "--yes"])
    assert result.exit_code == 0, result.output
    assert SetlistClient.deleted == [1014]
    assert "pool" in result.output.lower()  # never-orphan reassurance


def test_device_setlist_duplicate_creates_missing_target(monkeypatch, tmp_path):
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()

    class Creator(SetlistClient):
        def create_setlist(self, name, pos=None):
            type(self).created.append(name)
            type(self).SETLISTS = dict(type(self).SETLISTS, **{name: 1189})
            return 1189

    Creator.SETLISTS = {"helixgen": 988, "Mike": 1014}
    _patch_client(monkeypatch, Creator)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "duplicate", "helixgen", "ZZC-copy"])
    assert result.exit_code == 0, result.output
    assert Creator.created == ["ZZC-copy"]
    assert Creator.duplicated == [(988, 1189)]
    assert "3" in result.output  # copied count


# -- #39: strict setlist resolution — abort, never mint a duplicate ----------

class RaisingSetlistClient(SetlistClient):
    """A fake whose ``resolve_setlist_cid`` raises HelixError, simulating a
    network timeout/undecodable listing (backlog #39) instead of silently
    reading as "setlist absent"."""

    def resolve_setlist_cid(self, name, *, strict=True):
        self.calls.append(("resolve", name))
        raise HelixError("no reply listing container -5 (timeout or "
                         "connection drop); refusing to treat it as empty")


def test_device_setlist_create_aborts_on_listing_failure_no_duplicate(monkeypatch, tmp_path):
    """A timeout resolving the setlists root must abort `device setlist
    create` — never silently proceed to create a (possibly duplicate) setlist
    because the listing looked empty."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()
    _patch_client(monkeypatch, RaisingSetlistClient)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "create", "helixgen"])
    assert result.exit_code != 0
    assert "no reply" in result.output.lower() or "timeout" in result.output.lower()
    assert "already exists" not in result.output.lower()
    assert RaisingSetlistClient.created == []  # no duplicate minted


def test_device_setlist_duplicate_aborts_on_dst_listing_failure_no_duplicate(
        monkeypatch, tmp_path):
    """The `duplicate` dst-resolve is the exact #39 scenario: a failed listing
    of the destination must never be read as "dst absent" and auto-create a
    second setlist with that name."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()

    class SrcOkDstRaises(SetlistClient):
        def resolve_setlist_cid(self, name, *, strict=True):
            self.calls.append(("resolve", name))
            if name == "helixgen":
                return type(self).SETLISTS.get(name)
            raise HelixError("no reply listing container -5")

    _patch_client(monkeypatch, SrcOkDstRaises)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "duplicate", "helixgen", "ZZC-copy"])
    assert result.exit_code != 0
    assert SrcOkDstRaises.created == []  # never auto-created a duplicate dst
    assert SrcOkDstRaises.duplicated == []


def test_device_setlist_rename_aborts_on_new_name_listing_failure(monkeypatch, tmp_path):
    """The rename target-name check is also a #39 site: if we can't verify
    NEW_NAME is free, abort rather than risk renaming onto a collision."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()

    class SrcOkNewNameRaises(SetlistClient):
        def resolve_setlist_cid(self, name, *, strict=True):
            self.calls.append(("resolve", name))
            if name == "helixgen":
                return type(self).SETLISTS.get(name)
            raise HelixError("no reply listing container -5")

    _patch_client(monkeypatch, SrcOkNewNameRaises)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "rename", "helixgen", "gigs"])
    assert result.exit_code != 0
    assert "no reply" in result.output.lower()


# -- review #37 fixes ----------------------------------------------------------

def _patch_sftp_noop(monkeypatch, removed=None):
    from helixgen.device import sftp as sftp_mod

    class _NoopSftp:
        def __init__(self, ip, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def remove_ir_file(self, name):
            if removed is not None:
                removed.append(name)

    monkeypatch.setattr(sftp_mod, "HelixSFTP", _NoopSftp)


def test_device_delete_ir_wedged_hash_reachable_from_cli(monkeypatch):
    """The advertised wedge cleanup must work via the CLI (finding 2): a hash
    absent from the -11 listing but resolving in the path index is cleaned
    when --force-wedge is given."""
    removed = []
    _patch_sftp_noop(monkeypatch, removed)
    wedged = "dd" * 16

    class WedgeClient(IrClient):
        def ir_path_for_hash(self, h):
            return ("/data/stadium-family-fw/ir/ZZC-w.wav"
                    if h == wedged else None)

    _patch_client(monkeypatch, WedgeClient)
    result = CliRunner().invoke(
        cli, ["device", "delete-ir", wedged, "--force-wedge", "--yes"])
    assert result.exit_code == 0, result.output
    assert removed == ["ZZC-w.wav"]
    assert IrClient.deleted == [] or WedgeClient.deleted == []


def test_device_delete_ir_wedged_hash_without_flag_errors(monkeypatch):
    """Without --force-wedge the same state errors, protecting a healthy
    just-imported IR whose listing is merely lagging (finding 3)."""
    removed = []
    _patch_sftp_noop(monkeypatch, removed)
    wedged = "dd" * 16

    class WedgeClient(IrClient):
        def ir_path_for_hash(self, h):
            return "/data/stadium-family-fw/ir/ZZC-w.wav"

    _patch_client(monkeypatch, WedgeClient)
    result = CliRunner().invoke(cli, ["device", "delete-ir", wedged, "--yes"])
    assert result.exit_code != 0
    assert "force-wedge" in result.output
    assert removed == []


def test_device_set_info_continues_past_failures(monkeypatch):
    """Batch set-info applies to every cid and reports failures at the end
    instead of aborting on the first (finding 5)."""
    from helixgen.device import maintenance as mt
    calls = []

    def flaky(client, cid, **kw):
        calls.append(cid)
        if cid == 101:
            raise HelixError("device refused")
        return {"color": True}

    _patch_client(monkeypatch, FakeClient)
    monkeypatch.setattr(mt, "set_preset_info", flaky)
    result = CliRunner().invoke(
        cli, ["device", "set-info", "101", "102", "--color", "red"])
    assert result.exit_code != 0
    assert calls == [101, 102]  # kept going after 101 failed
    assert "102" in result.output and "failed" in result.output.lower()


def test_device_setlist_duplicate_records_created_target(monkeypatch, tmp_path):
    """An auto-created duplicate target is recorded in the local manifest,
    like `setlist create` (finding 7)."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    _reset_setlist_client()

    class Creator(SetlistClient):
        def create_setlist(self, name, pos=None):
            type(self).created.append(name)
            type(self).SETLISTS = dict(type(self).SETLISTS, **{name: 1189})
            return 1189

    Creator.SETLISTS = {"helixgen": 988}
    _patch_client(monkeypatch, Creator)
    result = CliRunner().invoke(
        cli, ["device", "setlist", "duplicate", "helixgen", "ZZC-copy"])
    assert result.exit_code == 0, result.output
    from helixgen.device.manifest import SetlistManifest
    assert "ZZC-copy" in SetlistManifest.load().setlists()

# --- device info --------------------------------------------------------------

CANNED_INFO = {
    "model": "stadium", "device_id": 2490368, "helixgen_model": "stadium_xl",
    "serial": "47292244582131381", "firmware": "1.3.2",
    "firmware_build": 1340, "firmware_date": "2026-04-13",
    "sd_total_bytes": 23340777472, "sd_available_bytes": 23338147840,
    "raw": {"clid": 14},
}


class InfoClient(FakeClient):
    def product_info(self):
        self.calls.append(("product_info",))
        return dict(CANNED_INFO)


def test_device_info_human(monkeypatch):
    _patch_client(monkeypatch, InfoClient)
    result = CliRunner().invoke(cli, ["device", "info"])
    assert result.exit_code == 0, result.output
    assert "stadium (stadium_xl)" in result.output
    assert "1.3.2" in result.output and "build 1340" in result.output
    assert "47292244582131381" in result.output
    assert "23.3 GB free of 23.3 GB" in result.output


def test_device_info_json(monkeypatch):
    _patch_client(monkeypatch, InfoClient)
    result = CliRunner().invoke(cli, ["device", "info", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == CANNED_INFO


# -- device reorder ------------------------------------------------------------

class ReorderClient(FakeClient):
    """Fake with the surface `reorder.reorder_setlist_item` needs."""

    SETLISTS = {"throwaway": 1234}
    CONTAINERS = {1234: [
        {"cid_": 501, "posi": 0, "cctp": 1003, "rcid": 100},
        {"cid_": 502, "posi": 1, "cctp": 1003, "rcid": 101},
    ]}
    POOL = [{"cid_": 100, "name": "Clean Machine"},
            {"cid_": 101, "name": "Lead Tone"}]

    def resolve_setlist_cid(self, name, *, strict=True):
        self.calls.append(("resolve_setlist_cid", name))
        return type(self).SETLISTS.get(name)

    def list_setlists(self, *, strict=False):
        self.calls.append(("list_setlists",))
        return [{"cid_": c, "name": n} for n, c in type(self).SETLISTS.items()]

    def list_container(self, cid, *, strict=False):
        self.calls.append(("list_container", cid))
        return type(self).CONTAINERS.get(cid, [])

    def list_presets(self, container=-2, *, strict=False):
        self.calls.append(("list_presets", container))
        return type(self).POOL

    def reorder_container(self, container, moved_cids, new_pos):
        self.calls.append(("reorder_container", container, list(moved_cids), new_pos))
        return [{"cid_": c, "posi": i} for i, c in enumerate(moved_cids)]


def test_device_reorder_by_preset_name(monkeypatch):
    _patch_client(monkeypatch, ReorderClient)
    result = CliRunner().invoke(
        cli, ["device", "reorder", "throwaway", "Lead Tone", "--to", "0"])
    assert result.exit_code == 0, result.output
    assert "moved cid 502" in result.output
    assert "position 0" in result.output


def test_device_reorder_by_literal_cid(monkeypatch):
    _patch_client(monkeypatch, ReorderClient)
    result = CliRunner().invoke(
        cli, ["device", "reorder", "throwaway", "501", "--to", "1"])
    assert result.exit_code == 0, result.output
    assert "moved cid 501" in result.output


def test_device_reorder_unknown_setlist_errors(monkeypatch):
    _patch_client(monkeypatch, ReorderClient)
    result = CliRunner().invoke(
        cli, ["device", "reorder", "ghost", "x", "--to", "0"])
    assert result.exit_code != 0
    assert "no setlist named" in result.output


def test_device_reorder_unknown_preset_name_errors(monkeypatch):
    _patch_client(monkeypatch, ReorderClient)
    result = CliRunner().invoke(
        cli, ["device", "reorder", "throwaway", "Nope", "--to", "0"])
    assert result.exit_code != 0
    assert "no preset named" in result.output


def test_device_reorder_setlists_keyword(monkeypatch):
    class RootReorderClient(ReorderClient):
        CONTAINERS = {
            **ReorderClient.CONTAINERS,
            -5: [{"cid_": 988, "posi": 0, "cctp": 1001, "name": "helixgen"},
                 {"cid_": 1014, "posi": 1, "cctp": 1001, "name": "Mike"}],
        }

    _patch_client(monkeypatch, RootReorderClient)
    result = CliRunner().invoke(
        cli, ["device", "reorder", "setlists", "Mike", "--to", "0"])
    assert result.exit_code == 0, result.output
    assert "moved cid 1014" in result.output


# -- #40: strict slot-emptiness gate — abort before any write ----------------

class RaisingFindByPosClient(FakeClient):
    """A fake whose ``find_by_pos`` raises HelixError ONLY when called with
    ``strict=True`` — simulating a listing timeout (backlog #40) instead of
    silently reading the slot as empty. Raising unconditionally (regardless of
    ``strict``) would let a test pass even if the production call site
    regressed to the lenient default, so a lenient call instead returns None
    (slot "empty") and lets the write proceed — the abort tests below would
    then fail on an unexpected write, catching that regression. Records the
    received ``strict`` value and any write attempted afterward on CLASS-level
    lists — the CLI instantiates a fresh instance per invocation, so
    per-instance ``calls``/state can't be inspected after the fact."""

    STRICT_SEEN: list = []
    WRITE_CALLS: list = []

    def find_by_pos(self, container, pos, *, strict=False):
        type(self).STRICT_SEEN.append(strict)
        if strict:
            raise HelixError("no reply listing container -2 (timeout or "
                             "connection drop); refusing to treat it as empty")
        return None

    def push_to_slot(self, container, pos, name, blob):
        type(self).WRITE_CALLS.append(("push_to_slot", container, pos, name))
        return 900

    def save_edit_buffer_to(self, container, pos, name):
        type(self).WRITE_CALLS.append(("save_edit_buffer_to", container, pos, name))
        return 901


def test_device_save_aborts_on_listing_failure_no_write(monkeypatch, tmp_path):
    """A timeout checking slot emptiness must abort `device save` before any
    /CreateContent-equivalent write — never silently treat the unconfirmed
    slot as empty and save into it."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    RaisingFindByPosClient.WRITE_CALLS = []
    RaisingFindByPosClient.STRICT_SEEN = []
    _patch_client(monkeypatch, RaisingFindByPosClient)
    result = CliRunner().invoke(
        cli, ["device", "save", "X", "--pos", "3"])
    assert result.exit_code != 0
    assert "no reply" in result.output.lower() or "timeout" in result.output.lower()
    assert RaisingFindByPosClient.WRITE_CALLS == []
    # prove the abort came from a strict=True call, not an accidental
    # lenient-default one that happened to raise anyway
    assert RaisingFindByPosClient.STRICT_SEEN == [True]


def test_device_push_aborts_on_listing_failure_no_write(monkeypatch, tmp_path):
    """Same #40 gate for `device push` (installs an .sbe backup)."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    infile = tmp_path / "backup.sbe"
    infile.write_bytes(b"_sbepgsm-fake")
    RaisingFindByPosClient.WRITE_CALLS = []
    RaisingFindByPosClient.STRICT_SEEN = []
    _patch_client(monkeypatch, RaisingFindByPosClient)
    result = CliRunner().invoke(
        cli, ["device", "push", str(infile), "X", "--pos", "3"])
    assert result.exit_code != 0
    assert "no reply" in result.output.lower() or "timeout" in result.output.lower()
    assert RaisingFindByPosClient.WRITE_CALLS == []
    assert RaisingFindByPosClient.STRICT_SEEN == [True]


def test_device_install_aborts_on_listing_failure_no_write(monkeypatch, tmp_path):
    """Same #40 gate for `device install` (transcodes a .hsp straight onto the
    device) — the emptiness check runs before transcoding, so a listing
    timeout aborts before any device write is attempted."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    hsp = _make_hsp(tmp_path / "tone.hsp", "White Limo Lead")
    RaisingFindByPosClient.WRITE_CALLS = []
    RaisingFindByPosClient.STRICT_SEEN = []
    _patch_client(monkeypatch, RaisingFindByPosClient)
    result = CliRunner().invoke(
        cli, ["device", "install", str(hsp), "White Limo Lead", "--pos", "3"])
    assert result.exit_code != 0
    assert "no reply" in result.output.lower() or "timeout" in result.output.lower()
    assert RaisingFindByPosClient.WRITE_CALLS == []
    assert RaisingFindByPosClient.STRICT_SEEN == [True]


def test_device_slots_restore_sbe_aborts_on_listing_failure_no_write(
        monkeypatch, tmp_path):
    """#40 gate for the `device slots restore` .sbe branch — the one
    hardened call site that previously had no dedicated regression test."""
    _fresh_manifest_env(monkeypatch, tmp_path)
    from helixgen.device.manifest import SetlistManifest

    sbe = tmp_path / "lead.sbe"
    sbe.write_bytes(b"_sbepgsm-fake")
    m = SetlistManifest.load()
    m.tones["Lead"] = {"path": str(sbe), "content_hash": None, "doc": None,
                       "source": "push", "slot": "2B", "device": None}
    m.save()

    RaisingFindByPosClient.WRITE_CALLS = []
    RaisingFindByPosClient.STRICT_SEEN = []
    _patch_client(monkeypatch, RaisingFindByPosClient)
    result = CliRunner().invoke(cli, ["device", "slots", "restore", "Lead"])
    assert result.exit_code != 0
    assert "no reply" in result.output.lower() or "timeout" in result.output.lower()
    assert RaisingFindByPosClient.WRITE_CALLS == []
    assert RaisingFindByPosClient.STRICT_SEEN == [True]
