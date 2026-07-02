"""CLI test for `helixgen decompile`."""
import json
from click.testing import CliRunner
from helixgen.cli import cli
from helixgen.hsp import HSP_MAGIC
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def test_decompile_cmd_writes_spec(tmp_path, hsp_library, monkeypatch):
    preset = compose_preset(parse_spec(
        {"name": "CLI", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.9}}]}]}),
        hsp_library, source="t")
    hsp_path = tmp_path / "in.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(preset).encode())

    out = tmp_path / "out.spec.json"
    res = CliRunner().invoke(cli, [
        "decompile", str(hsp_path), "-o", str(out), "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    spec = json.loads(out.read_text())
    assert spec["name"] == "CLI"
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"
