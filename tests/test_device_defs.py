"""Unit tests for the vendored device definitions loader (``helixgen.device.defs``).

These test the loader against the vendored ``_defs_data.json`` asset only — they
do NOT require the Helix Stadium editor app bundle to be installed.
"""
from __future__ import annotations

from helixgen.device import defs


def test_load_defs_shape():
    data = defs.load_defs()
    for key in ("models", "model_names", "model_params", "source"):
        assert key in data, f"missing top-level key: {key}"
    assert data["source"]["modeldefs"] == "p35md-1_3_0_0.bin"
    assert len(data["models"]) > 500  # ~801 in 1.3.0


def test_known_amp_model_roundtrips():
    # HD2_AmpBritPlexiBrt is a stable, real Stadium amp (numeric id 594 in 1.3.0).
    mid = defs.model_id_for("HD2_AmpBritPlexiBrt")
    assert isinstance(mid, int)
    assert defs.model_name_for(mid) == "HD2_AmpBritPlexiBrt"


def test_every_model_roundtrips():
    for name in defs.list_models():
        mid = defs.model_id_for(name)
        assert isinstance(mid, int)
        assert defs.model_name_for(mid) == name


def test_known_param_resolves_to_int_id():
    mid = defs.model_id_for("HD2_AmpBritPlexiBrt")
    # Amps expose a Drive knob.
    pid = defs.param_id_for(mid, "Drive")
    assert isinstance(pid, int)
    # param id lookup also accepts the model-id string directly
    assert defs.param_id_for("HD2_AmpBritPlexiBrt", "Drive") == pid


def test_param_meta_has_type_and_range():
    meta = defs.param_meta("HD2_AmpBritPlexiBrt", "Drive")
    assert meta is not None
    assert meta["type"] in {"f", "i", "b"}
    assert "id" in meta and isinstance(meta["id"], int)
    assert "min" in meta and "max" in meta and "def" in meta


def test_missing_model_returns_none():
    assert defs.model_id_for("NotARealModel_XYZ") is None
    assert defs.model_name_for(-1) is None
    assert defs.model_name_for(99999999) is None


def test_missing_param_returns_none():
    mid = defs.model_id_for("HD2_AmpBritPlexiBrt")
    assert defs.param_id_for(mid, "NoSuchParam") is None
    assert defs.param_meta(mid, "NoSuchParam") is None
    # unknown model -> None (not an exception)
    assert defs.param_id_for(123456789, "Drive") is None
    assert defs.param_meta("NotARealModel_XYZ", "Drive") is None


def test_list_models_sorted_and_unique():
    models = defs.list_models()
    assert models == sorted(models)
    assert len(models) == len(set(models))
