import json
import os
from pathlib import Path

import pytest

from helixgen.library import Block, Library, default_library_path


def test_block_round_trips_through_dict():
    block = Block(
        model_id="HD2_AmpBrit2204Custom",
        category="amp",
        display_name="Brit 2204",
        aliases=["JCM800"],
        params={
            "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        },
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.5},
        first_seen={"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"},
    )
    as_dict = block.to_dict()
    assert as_dict["model_id"] == "HD2_AmpBrit2204Custom"
    assert as_dict["display_name"] == "Brit 2204"
    assert as_dict["aliases"] == ["JCM800"]
    restored = Block.from_dict(as_dict)
    assert restored == block


def test_default_library_path_uses_home(monkeypatch):
    monkeypatch.delenv("HELIXGEN_LIBRARY", raising=False)
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    assert default_library_path() == Path("/tmp/fake-home/.helixgen/library")


def test_default_library_path_honors_env_var(monkeypatch):
    monkeypatch.setenv("HELIXGEN_LIBRARY", "/custom/lib")
    assert default_library_path() == Path("/custom/lib")


def make_block(**overrides):
    """Helper: build a minimal Block with overrideable fields."""
    defaults = dict(
        model_id="HD2_AmpBrit2204Custom",
        category="amp",
        display_name="Brit 2204",
        aliases=[],
        params={"Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]}},
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.5},
        first_seen={"preset": "x.hlx", "firmware": "3.71", "date": "2026-05-01"},
    )
    defaults.update(overrides)
    return Block(**defaults)


def test_save_block_writes_to_category_subdir(tmp_library):
    lib = Library(tmp_library)
    block = make_block()
    lib.save_block(block)
    expected_path = tmp_library / "blocks" / "amp" / "HD2_AmpBrit2204Custom.json"
    assert expected_path.exists()


def test_load_block_round_trip(tmp_library):
    lib = Library(tmp_library)
    block = make_block()
    lib.save_block(block)
    loaded = lib.load_block("HD2_AmpBrit2204Custom")
    assert loaded == block


def test_load_block_missing_raises(tmp_library):
    lib = Library(tmp_library)
    with pytest.raises(KeyError):
        lib.load_block("HD2_NotPresent")


def test_list_blocks_returns_all(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(model_id="HD2_AmpBrit2204Custom", category="amp"))
    lib.save_block(make_block(model_id="HD2_Cab4x12", category="cab", display_name="4x12"))
    blocks = sorted(lib.list_blocks(), key=lambda b: b.model_id)
    assert [b.model_id for b in blocks] == ["HD2_AmpBrit2204Custom", "HD2_Cab4x12"]


def test_list_blocks_filters_by_category(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(model_id="HD2_AmpBrit2204Custom", category="amp"))
    lib.save_block(make_block(model_id="HD2_Cab4x12", category="cab", display_name="4x12"))
    blocks = list(lib.list_blocks(category="amp"))
    assert [b.model_id for b in blocks] == ["HD2_AmpBrit2204Custom"]


def test_find_block_by_display_name(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(model_id="HD2_AmpBrit2204Custom", display_name="Brit 2204"))
    found = lib.find_block("Brit 2204")
    assert found.model_id == "HD2_AmpBrit2204Custom"


def test_find_block_by_alias(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_AmpBrit2204Custom",
        display_name="Brit 2204",
        aliases=["JCM800"],
    ))
    assert lib.find_block("JCM800").model_id == "HD2_AmpBrit2204Custom"


def test_find_block_by_model_id(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block())
    assert lib.find_block("HD2_AmpBrit2204Custom").model_id == "HD2_AmpBrit2204Custom"


def test_find_block_missing_raises_keyerror(tmp_library):
    lib = Library(tmp_library)
    with pytest.raises(KeyError):
        lib.find_block("Nonexistent Block")


def test_find_block_ambiguous_raises_with_candidates(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_RvbPlate", category="reverb", display_name="Plate Reverb"
    ))
    lib.save_block(make_block(
        model_id="HD2_LegacyPlateReverb",
        category="reverb",
        display_name="Plate Reverb",
    ))
    with pytest.raises(LookupError) as excinfo:
        lib.find_block("Plate Reverb")
    msg = str(excinfo.value)
    assert "HD2_RvbPlate" in msg
    assert "HD2_LegacyPlateReverb" in msg


def test_rebuild_index_writes_json(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_AmpBrit2204Custom",
        display_name="Brit 2204",
        aliases=["JCM800"],
        category="amp",
    ))
    lib.save_block(make_block(
        model_id="HD2_Cab4x12Greenback25",
        category="cab",
        display_name="4x12 Greenback 25",
    ))

    lib.rebuild_index()

    index_path = tmp_library / "index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text())

    # name → model_id resolution
    assert index["names"]["Brit 2204"] == ["HD2_AmpBrit2204Custom"]
    assert index["names"]["JCM800"] == ["HD2_AmpBrit2204Custom"]
    assert index["names"]["4x12 Greenback 25"] == ["HD2_Cab4x12Greenback25"]

    # model_id → category
    assert index["categories"]["HD2_AmpBrit2204Custom"] == "amp"
    assert index["categories"]["HD2_Cab4x12Greenback25"] == "cab"


def test_rebuild_index_records_ambiguity(tmp_library):
    lib = Library(tmp_library)
    lib.save_block(make_block(
        model_id="HD2_RvbPlate", category="reverb", display_name="Plate Reverb"
    ))
    lib.save_block(make_block(
        model_id="HD2_LegacyPlateReverb",
        category="reverb",
        display_name="Plate Reverb",
    ))

    lib.rebuild_index()

    index = json.loads((tmp_library / "index.json").read_text())
    assert sorted(index["names"]["Plate Reverb"]) == [
        "HD2_LegacyPlateReverb",
        "HD2_RvbPlate",
    ]


def test_rebuild_index_on_empty_library(tmp_library):
    lib = Library(tmp_library)
    lib.rebuild_index()
    index = json.loads((tmp_library / "index.json").read_text())
    assert index == {"names": {}, "categories": {}}


def test_chassis_save_load_round_trip(tmp_library):
    lib = Library(tmp_library)
    chassis = {
        "version": 6,
        "schema": "L6Preset",
        "data": {"meta": {"name": ""}, "tone": {"dsp0": {"blocks": {}}}},
        "_helixgen": {"position_keys": {"dsp0": ["dsp0_block_0"], "dsp1": []}},
    }
    assert not lib.has_chassis()
    lib.save_chassis(chassis)
    assert lib.has_chassis()
    assert lib.load_chassis() == chassis


def test_load_chassis_missing_raises(tmp_library):
    lib = Library(tmp_library)
    with pytest.raises(FileNotFoundError):
        lib.load_chassis()


from helixgen.library import IngestStatus


def test_save_block_first_time_returns_new(tmp_library):
    lib = Library(tmp_library)
    status = lib.save_block_with_dedup(make_block())
    assert status == IngestStatus.NEW


def test_save_block_same_schema_returns_match(tmp_library):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    block_v2 = make_block(
        exemplar={"@model": "HD2_AmpBrit2204Custom", "Drive": 0.99},
    )
    status = lib.save_block_with_dedup(block_v2)
    assert status == IngestStatus.MATCH


def test_save_block_different_schema_writes_v2_file(tmp_library):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    new_params = {
        "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        "NewParam": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }
    block_changed = make_block(params=new_params)
    status = lib.save_block_with_dedup(block_changed)
    assert status == IngestStatus.CONFLICT
    v2_path = tmp_library / "blocks" / "amp" / "HD2_AmpBrit2204Custom.v2.json"
    assert v2_path.exists()


def test_save_block_third_conflict_writes_v3(tmp_library):
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    lib.save_block_with_dedup(make_block(params={
        "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        "NewParam": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }))
    lib.save_block_with_dedup(make_block(params={
        "TotallyDifferent": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }))
    v3_path = tmp_library / "blocks" / "amp" / "HD2_AmpBrit2204Custom.v3.json"
    assert v3_path.exists()


def test_list_blocks_excludes_conflict_variants(tmp_library):
    """list_blocks must return one canonical entry per model_id, not the .vN.json siblings."""
    lib = Library(tmp_library)
    lib.save_block_with_dedup(make_block())
    lib.save_block_with_dedup(make_block(params={
        "Drive": {"type": "float", "default": 0.5, "observed_range": [0, 1]},
        "NewParam": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }))
    lib.save_block_with_dedup(make_block(params={
        "TotallyDifferent": {"type": "float", "default": 0.0, "observed_range": [0, 1]},
    }))
    blocks = lib.list_blocks()
    assert len(blocks) == 1
    assert blocks[0].model_id == "HD2_AmpBrit2204Custom"
