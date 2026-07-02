"""CLI tests for surgical preset edit commands (set-param, enable, disable,
add-block, remove-block, swap-model)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.generate import generate_preset
from helixgen.hsp import read_hsp


def _setup(tmp_path, hsp_library):
    """Generate a minimal preset (Tube Drive only) and return (lib_root, out_hsp_path)."""
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "C", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}
    ))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, hsp_library)
    return hsp_library.root, out


def test_cli_set_param_regenerates(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Tube Drive", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    slot = body["preset"]["flow"][0]["b01"]["slot"][0]
    assert slot["params"]["Gain"]["value"] == 0.9
    # Sidecar updated too.
    side = out.with_name(out.stem + ".spec.json")
    assert json.loads(side.read_text())["paths"][0]["blocks"][0]["params"]["Gain"] == 0.9


def test_cli_disable_block(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "disable", str(out), "Tube Drive", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["@enabled"]["value"] is False


def test_cli_unknown_block_errors(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Ghost", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code != 0
    assert "Ghost" in res.output
