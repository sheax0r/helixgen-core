"""Tests for view_preset_handler and patch_preset_handler in mcp_server.tools.

Post path-based redesign: both operate on `.hsp` **file paths** (no base64).
`patch_preset_handler` reads the file, mutates the body in place via
`helixgen.mutate` verbs, and writes it back to the same path; assertions
re-read the file and inspect the mutated body.
"""
from helixgen.generate import compose_preset
from helixgen.hsp import dumps_hsp, read_hsp
from helixgen.spec import parse_spec
from mcp_server import tools

MODEL = "stadium_xl"


def _write_preset(tmp_path, preset: dict) -> str:
    p = tmp_path / "preset.hsp"
    p.write_bytes(dumps_hsp(preset))
    return str(p)


def test_view_preset_handler(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), hsp_library, source="t")
    hsp_path = tmp_path / "m.hsp"
    hsp_path.write_bytes(dumps_hsp(preset))

    projection = tools.view_preset_handler(hsp_library, MODEL, str(hsp_path))
    assert projection["name"] == "M"
    assert projection["paths"][0]["blocks"][0]["block"] == "Tube Drive"


def test_view_preset_handler_missing_file_raises(hsp_library, tmp_path):
    import pytest
    with pytest.raises(ValueError, match="not found"):
        tools.view_preset_handler(hsp_library, MODEL, str(tmp_path / "nope.hsp"))


def test_view_preset_handler_bad_magic_raises(hsp_library, tmp_path):
    import pytest
    bad = tmp_path / "bad.hsp"
    bad.write_bytes(b"NOTMAGIC{}")
    with pytest.raises(ValueError, match="not a .hsp"):
        tools.view_preset_handler(hsp_library, MODEL, str(bad))


def test_patch_preset_handler_set_param(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9}])

    assert res == {"path": path, "warnings": []}
    body = read_hsp(path)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.9


def test_patch_preset_handler_set_param_disambiguates_by_pos(hsp_library, tmp_path):
    """Two same-named blocks in one path (e.g. dual-cab); `pos` must reach
    mutate.resolve_slot through the MCP dispatch, or this ambiguous address
    would raise MutateError instead of hitting only the 2nd block."""
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "pos": 1, "params": {"Gain": 0.1}},
            {"block": "Tube Drive", "pos": 2, "params": {"Gain": 0.2}},
        ]}]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9, "pos": 2}])

    body = read_hsp(path)
    path0 = body["preset"]["flow"][0]
    assert path0["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.1
    assert path0["b02"]["slot"][0]["params"]["Gain"]["value"] == 0.9


def test_patch_preset_handler_set_enabled_disambiguates_by_lane(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [
            {"blocks": [{"block": "Tube Drive", "lane": 0}]},
            {"blocks": [{"block": "Tube Drive", "lane": 1}]},
        ]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "set_enabled", "block": "Tube Drive", "enabled": False, "lane": 1}])

    body = read_hsp(path)
    flow = body["preset"]["flow"]
    assert flow[0]["b01"]["@enabled"]["value"] is True
    assert flow[1]["b15"]["@enabled"]["value"] is False


def test_patch_preset_handler_remove_block_disambiguates_by_pos(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [
            {"block": "Tube Drive", "pos": 1},
            {"block": "Tube Drive", "pos": 2},
        ]}]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "remove_block", "block": "Tube Drive", "pos": 2}])

    body = read_hsp(path)
    path0 = body["preset"]["flow"][0]
    keys = sorted(k for k in path0 if k.startswith("b") and k not in ("b00", "b13"))
    assert keys == ["b01"]


def test_patch_preset_handler_add_block(hsp_library, tmp_path):
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": []}]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "add_block", "block": "Tube Drive", "path": 0}])

    body = read_hsp(path)
    path0 = body["preset"]["flow"][0]
    from helixgen.hsp import translate_to_hsp
    assert path0["b01"]["slot"][0]["model"] == translate_to_hsp("HD2_DistTube")


def test_patch_preset_handler_swap_model_disambiguates_by_pos(hsp_library, tmp_path):
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
    path = _write_preset(tmp_path, preset)

    res = tools.patch_preset_handler(hsp_library, MODEL, path,
        [{"op": "swap_model", "old": "Tube Drive", "new": "Other Drive", "pos": 2}])

    from helixgen.hsp import translate_to_hsp
    body = read_hsp(path)
    path0 = body["preset"]["flow"][0]
    assert path0["b01"]["slot"][0]["model"] == translate_to_hsp("HD2_DistTube")
    assert path0["b02"]["slot"][0]["model"] == translate_to_hsp("HD2_DistOther")


def test_patch_preset_handler_unknown_op_raises(hsp_library, tmp_path):
    import pytest
    preset = compose_preset(parse_spec(
        {"name": "M", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}), hsp_library, source="t")
    path = _write_preset(tmp_path, preset)
    with pytest.raises(ValueError, match="unknown patch op"):
        tools.patch_preset_handler(hsp_library, MODEL, path, [{"op": "nope"}])
