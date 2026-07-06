"""Tests for decompile_preset_handler and patch_preset_handler in mcp_server.tools."""
import base64, json
from helixgen.hsp import HSP_MAGIC
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec
from mcp_server import tools

MODEL = "stadium_xl"


def test_decompile_preset_handler(hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), hsp_library, source="t")
    blob = base64.b64encode(HSP_MAGIC + json.dumps(preset).encode()).decode()
    spec = tools.decompile_preset_handler(hsp_library, MODEL, blob)
    assert spec["name"] == "M"
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"


def test_patch_preset_handler_set_param(hsp_library):
    spec = {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}
    res = tools.patch_preset_handler(hsp_library, MODEL, spec,
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9}])
    assert res["spec"]["paths"][0]["blocks"][0]["params"]["Gain"] == 0.9
    assert res["warnings"] == []


def test_patch_preset_handler_set_param_disambiguates_by_pos(hsp_library):
    """Two same-named blocks in one path (e.g. dual-cab); `pos` must reach
    patch.py's resolve_block() through the MCP dispatch, or this ambiguous
    address would raise PatchError instead of hitting only the 2nd block."""
    spec = {"name": "M", "paths": [{"blocks": [
        {"block": "Tube Drive", "pos": 1, "params": {"Gain": 0.1}},
        {"block": "Tube Drive", "pos": 2, "params": {"Gain": 0.2}},
    ]}]}
    res = tools.patch_preset_handler(hsp_library, MODEL, spec,
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9, "pos": 2}])
    blocks = res["spec"]["paths"][0]["blocks"]
    assert blocks[0]["params"]["Gain"] == 0.1
    assert blocks[1]["params"]["Gain"] == 0.9


def test_patch_preset_handler_set_enabled_disambiguates_by_lane(hsp_library):
    spec = {"name": "M", "paths": [
        {"blocks": [{"block": "Tube Drive", "lane": 0}]},
        {"blocks": [{"block": "Tube Drive", "lane": 1}]},
    ]}
    res = tools.patch_preset_handler(hsp_library, MODEL, spec,
        [{"op": "set_enabled", "block": "Tube Drive", "enabled": False, "lane": 1}])
    paths = res["spec"]["paths"]
    assert "enabled" not in paths[0]["blocks"][0]
    assert paths[1]["blocks"][0]["enabled"] is False


def test_patch_preset_handler_remove_block_disambiguates_by_pos(hsp_library):
    spec = {"name": "M", "paths": [{"blocks": [
        {"block": "Tube Drive", "pos": 1},
        {"block": "Tube Drive", "pos": 2},
    ]}]}
    res = tools.patch_preset_handler(hsp_library, MODEL, spec,
        [{"op": "remove_block", "block": "Tube Drive", "pos": 2}])
    blocks = res["spec"]["paths"][0]["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["pos"] == 1


def test_patch_preset_handler_add_block_carries_lane_and_pos(hsp_library):
    spec = {"name": "M", "paths": [{"blocks": []}]}
    res = tools.patch_preset_handler(hsp_library, MODEL, spec,
        [{"op": "add_block", "block": "Tube Drive", "path": 0, "lane": 1, "pos": 1}])
    entry = res["spec"]["paths"][0]["blocks"][0]
    assert entry == {"block": "Tube Drive", "lane": 1, "pos": 1}


def test_patch_preset_handler_swap_model_disambiguates_by_pos(hsp_library):
    from helixgen.library import Block

    hsp_library.save_block(Block(
        model_id="HD2_DistOther", category="drive", display_name="Other Drive",
        params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistOther", "@type": "fx", "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))

    spec = {"name": "M", "paths": [{"blocks": [
        {"block": "Tube Drive", "pos": 1},
        {"block": "Tube Drive", "pos": 2},
    ]}]}
    res = tools.patch_preset_handler(hsp_library, MODEL, spec,
        [{"op": "swap_model", "old": "Tube Drive", "new": "Other Drive", "pos": 2}])
    blocks = res["spec"]["paths"][0]["blocks"]
    assert blocks[0]["block"] == "Tube Drive"
    assert blocks[1]["block"] == "Other Drive"
