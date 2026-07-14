"""Tests for the `device_*` MCP handlers in `mcp_server.tools`.

These exercise the pure-Python handlers directly (no MCP transport, no
`server.py` import — so the suite runs even without the `mcp` SDK installed).
`helixgen.device.HelixClient` is monkeypatched with a fake context-manager
client that returns canned data, so no real device is ever contacted. The
fake still uses the *real* `helixgen.device.HelixError` so the error-mapping
path (HelixError -> ValueError) is genuinely covered.
"""
from __future__ import annotations

import pytest

import helixgen.device as device
from mcp_server import tools

MODEL = "stadium_xl"
BAD_MODEL = "not_a_helix"

# Canned device replies.
_PRESETS = [
    {"cid_": 10, "name": "Clean", "cctp": 1000, "posi": 0},
    {"cid_": 11, "name": "Lead", "cctp": 1000, "posi": 1},
]
_SETLISTS = [{"cid_": -2, "name": "User"}, {"cid_": -1, "name": "Factory"}]
_REF = {"cid_": 10, "name": "Clean", "cctp": 1000}


class FakeClient:
    """A stand-in for HelixClient: context manager, canned reads/writes.

    `raise_on` names a method that should raise the real `HelixError` (used to
    prove the handler maps it to `ValueError`). `record` captures call args so
    tests can assert the handler forwarded coordinates/containers correctly.
    """

    record: dict = {}

    def __init__(self, *args, **kwargs):
        FakeClient.record["init"] = kwargs
        self._raise_on = kwargs.pop("_raise_on", None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def mutating(self):
        import contextlib
        return contextlib.nullcontext(self)

    # production reaches raw primitives via client._raw.<name>; on this fake
    # they live directly on the instance.
    @property
    def _raw(self):
        return self

    def _maybe_raise(self, method):
        if self._raise_on == method:
            raise device.HelixError(f"boom in {method}")

    def list_presets(self, container=device.USER):
        self._maybe_raise("list_presets")
        FakeClient.record["container"] = container
        return _PRESETS

    def list_setlists(self):
        self._maybe_raise("list_setlists")
        return _SETLISTS

    def get_ref(self, cid):
        self._maybe_raise("get_ref")
        FakeClient.record["cid"] = cid
        return None if cid == 999 else _REF

    def load_preset(self, cid):
        self._maybe_raise("load_preset")
        FakeClient.record["cid"] = cid
        return True

    def create_from(self, src_cid, container, pos):
        self._maybe_raise("create_from")
        FakeClient.record.update(src_cid=src_cid, container=container, pos=pos)
        return 42

    def rename(self, cid, name):
        self._maybe_raise("rename")
        FakeClient.record.update(cid=cid, name=name)
        return True

    def delete(self, container, cids):
        self._maybe_raise("delete")
        FakeClient.record.update(container=container, cids=cids)
        return True

    def set_param(self, path, block, param_id, value):
        self._maybe_raise("set_param")
        FakeClient.record.update(path=path, block=block, param_id=param_id, value=value)
        return True

    def product_info(self):
        self._maybe_raise("product_info")
        return {"model": "stadium", "device_id": 2490368,
                "helixgen_model": "stadium_xl", "serial": "SN",
                "firmware": "1.3.2", "firmware_build": 1340,
                "firmware_date": "2026-04-13",
                "sd_total_bytes": 1, "sd_available_bytes": 1, "raw": {}}


@pytest.fixture
def fake_client(monkeypatch):
    """Patch `helixgen.device.HelixClient` with `FakeClient` and reset records."""
    FakeClient.record = {}
    monkeypatch.setattr(device, "HelixClient", FakeClient)
    return FakeClient


def _raising_client(method):
    """Return a FakeClient subclass whose `__init__` arms `_raise_on=method`."""

    class Raising(FakeClient):
        def __init__(self, *args, **kwargs):
            kwargs["_raise_on"] = method
            super().__init__(*args, **kwargs)

    return Raising


# --- reads ------------------------------------------------------------------


def test_list_presets_returns_canned(fake_client):
    result = tools.device_list_presets_handler(MODEL, ip="1.2.3.4")
    assert result == _PRESETS
    # default setlist "user" maps to the USER container.
    assert fake_client.record["container"] == device.USER


def test_list_presets_setlist_maps_to_container(fake_client):
    tools.device_list_presets_handler(MODEL, setlist="factory")
    assert fake_client.record["container"] == device.FACTORY


def test_list_presets_unknown_setlist_raises(fake_client):
    with pytest.raises(ValueError, match="unknown setlist"):
        tools.device_list_presets_handler(MODEL, setlist="bogus")


def test_list_setlists_returns_canned(fake_client):
    assert tools.device_list_setlists_handler(MODEL) == _SETLISTS


def test_read_preset_returns_ref(fake_client):
    assert tools.device_read_preset_handler(MODEL, cid=10) == _REF
    assert fake_client.record["cid"] == 10


def test_read_preset_missing_raises(fake_client):
    with pytest.raises(ValueError, match="no content at cid"):
        tools.device_read_preset_handler(MODEL, cid=999)


# --- writes -----------------------------------------------------------------


def test_load_preset_ok(fake_client):
    assert tools.device_load_preset_handler(MODEL, cid=10) == {"ok": True}
    assert fake_client.record["cid"] == 10


def test_create_preset_returns_new_cid(fake_client):
    result = tools.device_create_preset_handler(MODEL, src_cid=10, pos=3)
    assert result == {"ok": True, "cid": 42}
    assert fake_client.record["src_cid"] == 10
    assert fake_client.record["container"] == device.USER
    assert fake_client.record["pos"] == 3


def test_rename_preset_ok(fake_client):
    assert tools.device_rename_preset_handler(MODEL, cid=10, name="New") == {"ok": True}
    assert fake_client.record["name"] == "New"


def test_delete_preset_ok(fake_client):
    assert tools.device_delete_preset_handler(MODEL, cid=10) == {"ok": True}
    assert fake_client.record["container"] == device.USER
    assert fake_client.record["cids"] == [10]


def test_set_param_ok(fake_client):
    result = tools.device_set_param_handler(
        MODEL, path=0, block=1, param_id=5, value=0.5
    )
    assert result == {"ok": True}
    assert fake_client.record["path"] == 0
    assert fake_client.record["block"] == 1
    assert fake_client.record["param_id"] == 5
    assert fake_client.record["value"] == 0.5


# --- model gate + error mapping --------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: tools.device_list_presets_handler(BAD_MODEL),
        lambda: tools.device_list_setlists_handler(BAD_MODEL),
        lambda: tools.device_read_preset_handler(BAD_MODEL, cid=1),
        lambda: tools.device_load_preset_handler(BAD_MODEL, cid=1),
        lambda: tools.device_create_preset_handler(BAD_MODEL, src_cid=1, pos=0),
        lambda: tools.device_rename_preset_handler(BAD_MODEL, cid=1, name="x"),
        lambda: tools.device_delete_preset_handler(BAD_MODEL, cid=1),
        lambda: tools.device_set_param_handler(
            BAD_MODEL, path=0, block=0, param_id=0, value=0.0
        ),
    ],
)
def test_invalid_model_raises_before_touching_device(call):
    """An unsupported model is rejected by `_validate_model` (no device import)."""
    with pytest.raises(ValueError, match="unsupported model"):
        call()


def test_write_handler_maps_helixerror_to_valueerror(monkeypatch):
    """A HelixError from the client surfaces to the caller as ValueError."""
    monkeypatch.setattr(device, "HelixClient", _raising_client("set_param"))
    with pytest.raises(ValueError, match="device error"):
        tools.device_set_param_handler(
            MODEL, path=0, block=0, param_id=0, value=0.0
        )


def test_read_handler_maps_helixerror_to_valueerror(monkeypatch):
    """The read path also wraps HelixError as ValueError."""
    monkeypatch.setattr(device, "HelixClient", _raising_client("list_presets"))
    with pytest.raises(ValueError, match="device error"):
        tools.device_list_presets_handler(MODEL)


# -- create/delete reach the raw primitives via client._raw -------------------

def test_create_preset_uses_raw_create_from(fake_client):
    """device_create_preset must call the privatized _raw.create_from."""
    result = tools.device_create_preset_handler(MODEL, src_cid=10, pos=3)
    assert result == {"ok": True, "cid": 42}


def test_delete_preset_uses_raw_delete(fake_client):
    """device_delete_preset must call the privatized _raw.delete."""
    assert tools.device_delete_preset_handler(MODEL, cid=10) == {"ok": True}
    assert fake_client.record["cids"] == [10]


# -- setlist manifest handlers (local, no device) -----------------------------

def _fresh_manifest(monkeypatch, tmp_path):
    """Point the manifest + legacy ledger at empty tmp paths so load() starts
    empty and never reads the real user's files."""
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "setlists.json"))
    monkeypatch.setenv("HELIXGEN_DEVICE_SLOTS", str(tmp_path / "device-slots.json"))


def _write_hsp(path, name):
    from helixgen.hsp import write_hsp
    write_hsp(path, {"meta": {"name": name}})
    return path


def test_setlist_add_then_list(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    hsp = _write_hsp(tmp_path / "tone.hsp", "My Tone")
    out = tools.device_setlist_add_handler(MODEL, "helixgen", str(hsp))
    assert out["ok"] is True
    assert out["tone"] == "My Tone"
    assert out["tones"] == ["My Tone"]

    doc = tools.device_setlist_list_handler(MODEL)
    assert doc["setlists"] == {"helixgen": {"tones": ["My Tone"], "synced": False}}
    assert "My Tone" in doc["tones"]


def test_setlist_add_bad_model_raises(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    hsp = _write_hsp(tmp_path / "tone.hsp", "My Tone")
    with pytest.raises(ValueError, match="unsupported model"):
        tools.device_setlist_add_handler(BAD_MODEL, "helixgen", str(hsp))


def test_setlist_remove(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    hsp = _write_hsp(tmp_path / "tone.hsp", "My Tone")
    tools.device_setlist_add_handler(MODEL, "helixgen", str(hsp))
    out = tools.device_setlist_remove_handler(MODEL, "helixgen", "My Tone")
    assert out["ok"] is True
    assert out["tones"] == []


def test_setlist_remove_absent_reports_not_ok(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    out = tools.device_setlist_remove_handler(MODEL, "helixgen", "Nope")
    assert out["ok"] is False


# -- reference-based sync handlers (engine monkeypatched) ---------------------

_CANNED_SYNC = {"ok": True, "setlists": ["helixgen"],
                "pool": {"installed": ["A"], "updated": [], "skipped": []},
                "references": {"helixgen": {"added": [1], "removed": []}},
                "gc": {"deleted": []}, "irs": [], "errors": []}


def test_sync_setlist_calls_engine(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    from helixgen.device import setlist_sync as _ss
    seen = {}
    monkeypatch.setattr(_ss, "sync_setlists",
                        lambda manifest, **kw: seen.update(kw) or _CANNED_SYNC)
    out = tools.device_sync_setlist_handler(
        MODEL, "helixgen", ip="1.2.3.4", exclude_irs=True)
    assert out == _CANNED_SYNC
    assert seen["setlists"] == ["helixgen"]
    assert seen["ip"] == "1.2.3.4"
    assert seen["exclude_irs"] is True
    assert "gc" not in seen  # single-setlist sync never passes gc


def test_sync_all_calls_engine_with_gc(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    from helixgen.device import setlist_sync as _ss
    seen = {}
    monkeypatch.setattr(_ss, "sync_setlists",
                        lambda manifest, **kw: seen.update(kw) or _CANNED_SYNC)
    out = tools.device_sync_all_handler(MODEL, ip="1.2.3.4", gc=True)
    assert out == _CANNED_SYNC
    assert seen["setlists"] is None
    assert seen["gc"] is True


def test_sync_setlist_bad_model_raises(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="unsupported model"):
        tools.device_sync_setlist_handler(BAD_MODEL, "helixgen")


def test_sync_setlist_maps_helixerror_to_valueerror(monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    from helixgen.device import setlist_sync as _ss

    def _boom(manifest, **kw):
        raise device.HelixError("unreachable")

    monkeypatch.setattr(_ss, "sync_setlists", _boom)
    with pytest.raises(ValueError, match="device error"):
        tools.device_sync_setlist_handler(MODEL, "helixgen")


# -- IR maintenance + preset info + device-side setlist ops (library polish) --

_IRS = [
    {"cid_": 1159, "name": "YA KW 412", "hash": "aa" * 16, "posi": 0},
    {"cid_": 1160, "name": "ZZC-test", "hash": "bb" * 16, "posi": 1},
]


class PolishClient(FakeClient):
    """FakeClient + the IR/setlist maintenance surface."""

    SETLISTS = {"helixgen": 988}

    def list_irs(self, strict=False):
        self._maybe_raise("list_irs")
        return [dict(m) for m in _IRS]

    def delete_irs(self, cids):
        self._maybe_raise("delete_irs")
        FakeClient.record["deleted_irs"] = list(cids)
        return True

    def resolve_setlist_cid(self, name):
        return type(self).SETLISTS.get(name)

    def create_setlist(self, name, pos=None):
        self._maybe_raise("create_setlist")
        FakeClient.record["created_setlist"] = name
        return 1186

    def delete_setlist(self, cid):
        FakeClient.record["deleted_setlist"] = cid
        return True

    def duplicate_setlist_refs(self, src, dst):
        FakeClient.record["duplicated"] = (src, dst)
        return 2


@pytest.fixture
def polish_client(monkeypatch):
    FakeClient.record = {}
    PolishClient.SETLISTS = {"helixgen": 988}
    monkeypatch.setattr(device, "HelixClient", PolishClient)
    return PolishClient


def test_device_delete_ir_resolves_and_deletes(polish_client, monkeypatch):
    # keep the backing-file removal hermetic (no real SFTP)
    removed = []

    class _NoopSftp:
        def __init__(self, ip, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def remove_ir_file(self, name):
            removed.append(name)

    from helixgen.device import sftp as sftp_mod
    monkeypatch.setattr(sftp_mod, "HelixSFTP", _NoopSftp)
    res = tools.device_delete_ir_handler(MODEL, name_or_hash="ZZC-test")
    assert res == {"ok": True, "cid": 1160, "name": "ZZC-test",
                   "hash": "bb" * 16, "file_removed": True}
    assert FakeClient.record["deleted_irs"] == [1160]
    assert removed == ["ZZC-test.wav"]


def test_device_delete_ir_unknown_raises(polish_client):
    with pytest.raises(ValueError, match="no device IR"):
        tools.device_delete_ir_handler(MODEL, name_or_hash="nope")


def test_device_rename_ir(polish_client):
    res = tools.device_rename_ir_handler(
        MODEL, name_or_hash="bb" * 16, new_name="ZZC-2")
    assert res["ok"] is True and res["cid"] == 1160
    assert FakeClient.record["name"] == "ZZC-2"


def test_device_ir_prune_forwards_args(monkeypatch):
    from helixgen.device import maintenance as mt
    seen = {}
    canned = {"ok": True, "dry_run": True, "device_irs": 2, "referenced": [],
              "protected": [], "orphans": [], "deleted": [], "errors": []}
    monkeypatch.setattr(mt, "ir_prune", lambda **kw: seen.update(kw) or canned)
    res = tools.device_ir_prune_handler(MODEL, execute=True, force=True,
                                        only="ZZC-test")
    assert res is canned
    assert seen["execute"] and seen["force"] and seen["only"] == "ZZC-test"


def test_device_set_info_batches_cids(polish_client, monkeypatch):
    from helixgen.device import maintenance as mt
    calls = []
    monkeypatch.setattr(
        mt, "set_preset_info",
        lambda client, cid, **kw: calls.append((cid, kw)) or {"color": True})
    res = tools.device_set_info_handler(MODEL, cids=[10, 11], color="red")
    assert res == {"ok": True,
                   "results": [{"cid": 10, "color": True},
                               {"cid": 11, "color": True}]}
    assert [c[0] for c in calls] == [10, 11]


def test_device_set_info_requires_something(polish_client):
    with pytest.raises(ValueError):
        tools.device_set_info_handler(MODEL, cids=[10])


def test_device_setlist_create_on_device(polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    res = tools.device_setlist_create_handler(MODEL, name="ZZC-new")
    assert res == {"ok": True, "cid": 1186, "name": "ZZC-new"}
    assert FakeClient.record["created_setlist"] == "ZZC-new"


def test_device_setlist_create_existing_raises(polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        tools.device_setlist_create_handler(MODEL, name="helixgen")


def test_device_setlist_rename_on_device(polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    res = tools.device_setlist_rename_handler(
        MODEL, name="helixgen", new_name="gigs")
    assert res == {"ok": True, "cid": 988, "name": "gigs"}
    assert FakeClient.record["name"] == "gigs"


def test_device_setlist_rename_missing_raises(polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not found"):
        tools.device_setlist_rename_handler(MODEL, name="nope", new_name="x")


def test_device_setlist_delete_on_device(polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    res = tools.device_setlist_delete_handler(MODEL, name="helixgen")
    assert res == {"ok": True, "cid": 988, "name": "helixgen"}
    assert FakeClient.record["deleted_setlist"] == 988


def test_device_setlist_duplicate_creates_target(polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    res = tools.device_setlist_duplicate_handler(
        MODEL, src="helixgen", dst="ZZC-copy")
    assert res == {"ok": True, "src_cid": 988, "dst_cid": 1186,
                   "created": True, "copied": 2}
    assert FakeClient.record["duplicated"] == (988, 1186)


# -- review #37 fixes ----------------------------------------------------------

def test_device_delete_ir_forwards_force_wedge(polish_client, monkeypatch):
    from helixgen.device import maintenance as mt
    seen = {}
    monkeypatch.setattr(
        mt, "delete_device_ir",
        lambda client, q, ip, force_wedge=False: seen.update(
            q=q, force_wedge=force_wedge) or {"ok": True, "cid": None,
                                              "name": "x", "hash": "dd" * 16,
                                              "file_removed": True})
    res = tools.device_delete_ir_handler(
        MODEL, name_or_hash="dd" * 16, force_wedge=True)
    assert res["file_removed"] is True and res["cid"] is None
    assert seen == {"q": "dd" * 16, "force_wedge": True}


def test_device_set_info_continues_past_failures(polish_client, monkeypatch):
    from helixgen.device import maintenance as mt
    calls = []

    def flaky(client, cid, **kw):
        calls.append(cid)
        if cid == 10:
            raise device.HelixError("refused")
        return {"color": True}

    monkeypatch.setattr(mt, "set_preset_info", flaky)
    res = tools.device_set_info_handler(MODEL, cids=[10, 11], color="red")
    assert calls == [10, 11]
    assert res["ok"] is False
    assert res["results"][0]["cid"] == 10 and "error" in res["results"][0]
    assert res["results"][1] == {"cid": 11, "color": True}


def test_device_setlist_duplicate_records_created_target(
        polish_client, monkeypatch, tmp_path):
    _fresh_manifest(monkeypatch, tmp_path)
    res = tools.device_setlist_duplicate_handler(
        MODEL, src="helixgen", dst="ZZC-copy")
    assert res["created"] is True
    from helixgen.device.manifest import SetlistManifest
    assert "ZZC-copy" in SetlistManifest.load().setlists()

# --- device info ---------------------------------------------------------------

def test_device_info_returns_curated(fake_client):
    info = tools.device_info_handler(MODEL)
    assert info["firmware"] == "1.3.2"
    assert info["helixgen_model"] == "stadium_xl"


def test_device_info_bad_model(fake_client):
    with pytest.raises(ValueError):
        tools.device_info_handler(BAD_MODEL)


def test_device_info_maps_helixerror(monkeypatch):
    monkeypatch.setattr(device, "HelixClient", _raising_client("product_info"))
    with pytest.raises(ValueError, match="device error"):
        tools.device_info_handler(MODEL)
