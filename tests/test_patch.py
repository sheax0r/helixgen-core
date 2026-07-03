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


def test_verbs_do_not_mutate_input():
    import copy
    s = _spec()
    snapshot = copy.deepcopy(s)
    patch.set_param(s, "Tube Drive", "Gain", 0.9)
    patch.remove_block(s, "Brit Amp")
    assert s == snapshot


def test_set_enabled_reenable_in_snapshot():
    s = _spec()
    s["snapshots"] = [{"name": "Lead", "disable": ["Tube Drive"]}]
    out = patch.set_enabled(s, "Tube Drive", True, snapshot="Lead")
    assert "Tube Drive" not in out["snapshots"][0]["disable"]


def test_set_enabled_missing_snapshot_raises():
    with pytest.raises(patch.PatchError):
        patch.set_enabled(_spec(), "Tube Drive", False, snapshot="Ghost")


def test_add_block_after_scoped_to_path():
    s = {"name": "P", "paths": [
        {"blocks": [{"block": "A"}, {"block": "B"}]},
        {"blocks": [{"block": "X"}, {"block": "Y"}]}]}
    # "Y" lives only on path 1; adding after "Y" on path 0 must error, not
    # silently insert at Y's index into path 0.
    with pytest.raises(patch.PatchError):
        patch.add_block(s, "Z", path=0, after="Y")
    # after on the correct path inserts in the right place:
    out = patch.add_block(s, "Z", path=1, after="X")
    assert [b["block"] for b in out["paths"][1]["blocks"]] == ["X", "Z", "Y"]


def test_resolve_block_by_pos():
    spec = {"name": "n", "paths": [{"blocks": [
        {"block": "With Pan", "pos": 1}, {"block": "With Pan", "pos": 2}]}]}
    assert patch.resolve_block(spec, "With Pan", None, None, pos=2) == (0, 1)
    with pytest.raises(patch.PatchError):
        patch.resolve_block(spec, "With Pan", None, None)  # ambiguous


def test_resolve_block_by_lane():
    spec = {"name": "n", "paths": [
        {"blocks": [{"block": "Tube Drive", "lane": 0}]},
        {"blocks": [{"block": "Tube Drive", "lane": 1}]},
    ]}
    assert patch.resolve_block(spec, "Tube Drive", None, None, lane=1) == (1, 0)
    with pytest.raises(patch.PatchError):
        patch.resolve_block(spec, "Tube Drive", None, None)  # ambiguous
