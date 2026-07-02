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
