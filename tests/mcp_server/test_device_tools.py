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
    assert doc["setlists"] == {"helixgen": ["My Tone"]}
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
        MODEL, "helixgen", ip="1.2.3.4", exclude_irs=True, template_cid=7)
    assert out == _CANNED_SYNC
    assert seen["setlists"] == ["helixgen"]
    assert seen["ip"] == "1.2.3.4"
    assert seen["exclude_irs"] is True
    assert seen["template_cid"] == 7
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
