"""--json output modes for the core (non-device) agent-facing verbs.

The CLI is the ONLY engine surface (the MCP server was removed in 0.20.0);
agents consume these verbs' stdout programmatically, so each must offer a
machine-readable JSON mode.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from helixgen.cli import cli


def test_list_blocks_json(hsp_library):
    res = CliRunner().invoke(
        cli, ["list-blocks", "--json", "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert isinstance(data, list) and data
    names = {b["display_name"] for b in data}
    assert {"Tube Drive", "Brit Amp"} <= names
    row = next(b for b in data if b["display_name"] == "Tube Drive")
    assert row["model_id"] == "HD2_DistTube"
    assert row["category"] == "drive"


def test_list_blocks_json_category_filter(hsp_library):
    res = CliRunner().invoke(
        cli, ["list-blocks", "--json", "--category", "amp",
              "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert {b["category"] for b in data} == {"amp"}


def test_list_blocks_json_empty_library(tmp_library):
    res = CliRunner().invoke(
        cli, ["list-blocks", "--json", "--library", str(tmp_library)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == []


def test_show_block_json(hsp_library):
    res = CliRunner().invoke(
        cli, ["show-block", "Tube Drive", "--json",
              "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["display_name"] == "Tube Drive"
    assert data["model_id"] == "HD2_DistTube"
    assert data["category"] == "drive"
    assert "Gain" in data["params"]
    assert data["params"]["Gain"]["type"] == "float"


def test_list_irs_json(tmp_path, monkeypatch):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    (irs_dir / "mapping.json").write_text(json.dumps({
        "aa" * 16: "/x/a.wav", "bb" * 16: "/x/b.wav"}))
    res = CliRunner().invoke(cli, ["list-irs", "--json", "--irs-dir", str(irs_dir)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data == [
        {"hash": "aa" * 16, "path": "/x/a.wav"},
        {"hash": "bb" * 16, "path": "/x/b.wav"},
    ]


def test_list_irs_json_empty(tmp_path):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    res = CliRunner().invoke(cli, ["list-irs", "--json", "--irs-dir", str(irs_dir)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == []


def test_view_stdout_is_json(tmp_path, hsp_library):
    """`view` (no -o) prints the recipe-shape projection as JSON on stdout."""
    from helixgen.generate import generate_preset

    spec = tmp_path / "in.json"
    spec.write_text(json.dumps(
        {"name": "V", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "v.hsp"
    generate_preset(spec, out, hsp_library)
    res = CliRunner().invoke(
        cli, ["view", str(out), "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["name"] == "V"
    assert data["paths"]
