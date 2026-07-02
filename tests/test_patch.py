# tests/test_patch.py
import pytest
from helixgen import patch


def _spec():
    return {"name": "P", "paths": [{"blocks": [
        {"block": "Tube Drive", "params": {"Gain": 0.5}},
        {"block": "Brit Amp", "params": {"Drive": 0.6}}]}]}


def test_resolve_block_unique():
    assert patch.resolve_block(_spec(), "Brit Amp", None, None) == (0, 1)


def test_resolve_block_missing_raises():
    with pytest.raises(patch.PatchError):
        patch.resolve_block(_spec(), "Nope", None, None)


def test_resolve_block_ambiguous_requires_index():
    s = {"name": "P", "paths": [{"blocks": [
        {"block": "Tube Drive"}, {"block": "Tube Drive"}]}]}
    with pytest.raises(patch.PatchError):
        patch.resolve_block(s, "Tube Drive", None, None)
    assert patch.resolve_block(s, "Tube Drive", 0, 1) == (0, 1)


def test_set_param():
    out = patch.set_param(_spec(), "Tube Drive", "Gain", 0.9)
    assert out["paths"][0]["blocks"][0]["params"]["Gain"] == 0.9


def test_set_enabled_base():
    out = patch.set_enabled(_spec(), "Tube Drive", False)
    assert out["paths"][0]["blocks"][0]["enabled"] is False


def test_set_enabled_in_snapshot():
    s = _spec()
    s["snapshots"] = [{"name": "Lead"}]
    out = patch.set_enabled(s, "Tube Drive", False, snapshot="Lead")
    assert "Tube Drive" in out["snapshots"][0]["disable"]


def test_add_block_after():
    out = patch.add_block(_spec(), "Plate Stereo", after="Brit Amp",
                          params={"Mix": 0.2})
    names = [b["block"] for b in out["paths"][0]["blocks"]]
    assert names == ["Tube Drive", "Brit Amp", "Plate Stereo"]


def test_remove_block():
    out = patch.remove_block(_spec(), "Tube Drive")
    names = [b["block"] for b in out["paths"][0]["blocks"]]
    assert names == ["Brit Amp"]
