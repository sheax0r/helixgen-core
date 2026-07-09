"""Tests for view_preset_handler and patch_preset_handler in mcp_server.tools.

Post hsp-canonical-redesign: both operate directly on base64-encoded `.hsp`
blobs (no spec round-trip). `patch_preset_handler` decodes, mutates the body
in place via `helixgen.mutate` verbs, and re-encodes; assertions decode the
returned blob and inspect the mutated body.
"""
import base64
import json

from helixgen.generate import compose_preset
from helixgen.hsp import HSP_MAGIC, dumps_hsp
from helixgen.spec import parse_spec
from mcp_server import tools

MODEL = "stadium_xl"


def _hsp_b64(preset: dict) -> str:
    return base64.b64encode(dumps_hsp(preset)).decode()


def _decode_body(hsp_b64: str) -> dict:
    raw = base64.b64decode(hsp_b64)
    assert raw[:len(HSP_MAGIC)] == HSP_MAGIC
    return json.loads(raw[len(HSP_MAGIC):].decode("utf-8"))


def test_view_preset_handler(hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), hsp_library, source="t")
    projection = tools.view_preset_handler(hsp_library, MODEL, _hsp_b64(preset))
    assert projection["name"] == "M"
    assert projection["paths"][0]["blocks"][0]["block"] == "Tube Drive"


def test_patch_preset_handler_set_param(hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}), hsp_library, source="t")

    res = tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset),
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9}])

    assert res["warnings"] == []
    body = _decode_body(res["hsp_b64"])
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.9


def test_patch_preset_handler_set_param_disambiguates_by_pos(hsp_library):
    """Two same-named blocks in one path (e.g. dual-cab); `pos` must reach
    mutate.resolve_slot through the MCP dispatch, or this ambiguous address
    would raise MutateError instead of hitting only the 2nd block."""
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "pos": 1, "params": {"Gain": 0.1}},
            {"block": "Tube Drive", "pos": 2, "params": {"Gain": 0.2}},
        ]}]}), hsp_library, source="t")

    res = tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset),
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9, "pos": 2}])

    body = _decode_body(res["hsp_b64"])
    path0 = body["preset"]["flow"][0]
    assert path0["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.1
    assert path0["b02"]["slot"][0]["params"]["Gain"]["value"] == 0.9


def test_patch_preset_handler_set_enabled_disambiguates_by_lane(hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [
            {"blocks": [{"block": "Tube Drive", "lane": 0}]},
            {"blocks": [{"block": "Tube Drive", "lane": 1}]},
        ]}), hsp_library, source="t")

    res = tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset),
        [{"op": "set_enabled", "block": "Tube Drive", "enabled": False, "lane": 1}])

    body = _decode_body(res["hsp_b64"])
    flow = body["preset"]["flow"]
    assert flow[0]["b01"]["@enabled"]["value"] is True
    assert flow[1]["b15"]["@enabled"]["value"] is False


def test_patch_preset_handler_remove_block_disambiguates_by_pos(hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "pos": 1},
            {"block": "Tube Drive", "pos": 2},
        ]}]}), hsp_library, source="t")

    res = tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset),
        [{"op": "remove_block", "block": "Tube Drive", "pos": 2}])

    body = _decode_body(res["hsp_b64"])
    path0 = body["preset"]["flow"][0]
    keys = sorted(k for k in path0 if k.startswith("b") and k not in ("b00", "b13"))
    assert keys == ["b01"]


def test_patch_preset_handler_add_block(hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": []}]}), hsp_library, source="t")

    res = tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset),
        [{"op": "add_block", "block": "Tube Drive", "path": 0}])

    body = _decode_body(res["hsp_b64"])
    path0 = body["preset"]["flow"][0]
    from helixgen.hsp import translate_to_hsp
    assert path0["b01"]["slot"][0]["model"] == translate_to_hsp("HD2_DistTube")


def test_patch_preset_handler_swap_model_disambiguates_by_pos(hsp_library):
    from helixgen.library import Block

    hsp_library.save_block(Block(
        model_id="HD2_DistOther", category="drive", display_name="Other Drive",
        params={"Gain": {"type": "float"}},
        exemplar={"@model": "HD2_DistOther", "@type": "fx", "@enabled": True, "Gain": 0.5},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-06-28"}))

    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "pos": 1},
            {"block": "Tube Drive", "pos": 2},
        ]}]}), hsp_library, source="t")

    res = tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset),
        [{"op": "swap_model", "old": "Tube Drive", "new": "Other Drive", "pos": 2}])

    from helixgen.hsp import translate_to_hsp
    body = _decode_body(res["hsp_b64"])
    path0 = body["preset"]["flow"][0]
    assert path0["b01"]["slot"][0]["model"] == translate_to_hsp("HD2_DistTube")
    assert path0["b02"]["slot"][0]["model"] == translate_to_hsp("HD2_DistOther")


def test_patch_preset_handler_unknown_op_raises(hsp_library):
    import pytest
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), hsp_library, source="t")
    with pytest.raises(ValueError, match="unknown patch op"):
        tools.patch_preset_handler(hsp_library, MODEL, _hsp_b64(preset), [{"op": "nope"}])
