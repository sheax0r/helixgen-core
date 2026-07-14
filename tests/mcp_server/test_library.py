import json

from mcp_server.tools import generate_preset_handler


def test_generate_handler_auto_registers(mcp_library, monkeypatch, tmp_path):
    monkeypatch.setenv("HELIXGEN_SETLISTS", str(tmp_path / "s.json"))
    out = tmp_path / "o.hsp"
    result = generate_preset_handler(
        mcp_library, "stadium_xl",
        recipe={"name": "MCP Reg", "paths": [{"blocks": []}]},
        out_path=str(out))
    assert result["path"] == str(out)
    from helixgen.device.manifest import SetlistManifest
    m = SetlistManifest.load()
    assert "MCP Reg" in m.tones
    assert m.tones["MCP Reg"]["slot"] is None
    assert m.tones["MCP Reg"]["source"] == "authored"
