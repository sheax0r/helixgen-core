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


def test_model_params_for_returns_full_table():
    # accepts a model-id string or numeric id; result is the ordered param table
    tbl = defs.model_params_for("HD2_AmpBritPlexiBrt")
    assert isinstance(tbl, dict) and "Drive" in tbl
    mid = defs.model_id_for("HD2_AmpBritPlexiBrt")
    assert defs.model_params_for(mid) == tbl
    # every entry carries a numeric param id
    for meta in tbl.values():
        assert isinstance(meta.get("id"), int)


def test_model_params_for_unknown_is_empty_dict_not_none():
    # unknown model -> {} so callers can iterate directly (never None)
    assert defs.model_params_for("NotARealModel_XYZ") == {}
    assert defs.model_params_for(99999999) == {}


def test_category_for_known_models():
    # accepts a model-id string or its numeric id
    assert defs.category_for("HD2_AmpBritPlexiBrt") == "amp"
    mid = defs.model_id_for("HD2_AmpBritPlexiBrt")
    assert defs.category_for(mid) == "amp"


def test_category_for_unknown_or_none_returns_none():
    assert defs.category_for(None) is None
    assert defs.category_for(99999999) is None
    assert defs.category_for("NotARealModel_XYZ") is None
