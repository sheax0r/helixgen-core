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
