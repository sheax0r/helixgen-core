# tests/test_patch_swap.py
import pytest
from helixgen import patch
from helixgen.library import Block, Library


def _lib(tmp_path):
    lib = Library(root=tmp_path / "lib")
    lib.save_block(Block(model_id="HD2_AmpA", category="amp", display_name="Amp A",
        params={"Drive": {"type": "float"}, "Master": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    lib.save_block(Block(model_id="HD2_AmpB", category="amp", display_name="Amp B",
        params={"Drive": {"type": "float"}, "Presence": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    lib.save_block(Block(model_id="HD2_CabC", category="cab", display_name="Cab C",
        params={"HighCut": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    return lib


def _spec():
    return {"name": "S", "paths": [{"blocks": [
        {"block": "Amp A", "params": {"Drive": 0.8, "Master": 0.6}}]}]}


def test_swap_same_category_carries_shared_params(tmp_path):
    out, warns = patch.swap_model(_spec(), "Amp A", "Amp B", _lib(tmp_path))
    b = out["paths"][0]["blocks"][0]
    assert b["block"] == "Amp B"
    assert b["params"]["Drive"] == 0.8        # shared param carried
    assert "Master" not in b["params"]        # dropped (not on Amp B)
    assert any("Master" in w for w in warns)  # warned about the drop


def test_swap_cross_category_refused(tmp_path):
    with pytest.raises(patch.PatchError):
        patch.swap_model(_spec(), "Amp A", "Cab C", _lib(tmp_path))


def test_swap_unknown_target_refused(tmp_path):
    with pytest.raises(patch.PatchError):
        patch.swap_model(_spec(), "Amp A", "Ghost", _lib(tmp_path))


def test_swap_preserves_ir_ref(tmp_path):
    lib = Library(root=tmp_path / "lib")
    lib.save_block(Block(model_id="HX2_ImpulseResponse1", category="cab",
        display_name="IR One", params={"Mix": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    lib.save_block(Block(model_id="HX2_ImpulseResponse2", category="cab",
        display_name="IR Two", params={"Mix": {"type": "float"}},
        exemplar={}, first_seen={"preset": "_", "firmware": "_", "date": "x"}))
    spec = {"name": "S", "paths": [{"blocks": [
        {"block": "IR One", "ir": "my.wav", "params": {"Mix": 1.0}}]}]}
    out, _ = patch.swap_model(spec, "IR One", "IR Two", lib)
    assert out["paths"][0]["blocks"][0]["ir"] == "my.wav"
