"""CLI test for `helixgen view` (replaces the old `decompile` command under
the .hsp-canonical redesign — the view is a read-only, non-authoritative
projection off the .hsp; there is no sidecar)."""
import json
from click.testing import CliRunner
from helixgen.cli import cli
from helixgen.hsp import HSP_MAGIC
from helixgen.generate import compose_preset
from helixgen.spec import parse_spec


def test_view_cmd_writes_projection(tmp_path, hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "CLI", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.9}}]}]}),
        hsp_library, source="t")
    hsp_path = tmp_path / "in.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(preset).encode())

    out = tmp_path / "out.spec.json"
    res = CliRunner().invoke(cli, [
        "view", str(hsp_path), "-o", str(out), "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    spec = json.loads(out.read_text())
    assert spec["name"] == "CLI"
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert "non-authoritative" in res.output


def test_view_cmd_prints_to_stdout(tmp_path, hsp_library):
    preset = compose_preset(parse_spec(
        {"name": "CLI", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.9}}]}]}),
        hsp_library, source="t")
    hsp_path = tmp_path / "in.hsp"
    hsp_path.write_bytes(HSP_MAGIC + json.dumps(preset).encode())

    res = CliRunner().invoke(cli, [
        "view", str(hsp_path), "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    spec = json.loads(res.output)
    assert spec["name"] == "CLI"
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"


def test_decompile_cmd_removed():
    """`decompile` no longer exists as a CLI command — replaced by `view`."""
    res = CliRunner().invoke(cli, ["decompile", "--help"])
    assert res.exit_code != 0
