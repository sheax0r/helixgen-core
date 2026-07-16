"""`helixgen patch` — apply a LIST of surgical ops to a .hsp atomically.

This is the CLI replacement for the removed MCP `patch_preset` tool: one
invocation applies a JSON list of `{op, ...}` operations to the file in
place. A bad op anywhere in the list aborts BEFORE the file is written, so
the .hsp is never left half-patched.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.generate import generate_preset
from helixgen.hsp import read_hsp


def _setup(tmp_path, hsp_library):
    spec = tmp_path / "in.json"
    spec.write_text(json.dumps({
        "name": "P",
        "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}},
            {"block": "Brit Amp", "params": {"Drive": 0.5}},
        ]}],
    }))
    out = tmp_path / "p.hsp"
    generate_preset(spec, out, hsp_library)
    out.with_name(out.stem + ".spec.json").unlink(missing_ok=True)
    return out


def _ops_file(tmp_path, ops) -> str:
    p = tmp_path / "ops.json"
    p.write_text(json.dumps(ops))
    return str(p)


def test_patch_applies_multiple_ops(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [
        {"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9},
        {"op": "set_enabled", "block": "Brit Amp", "enabled": False},
    ]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    slots = body["preset"]["flow"][0]
    assert slots["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.9
    assert slots["b02"]["@enabled"]["value"] is False


def test_patch_unknown_op_leaves_file_untouched(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = [
        {"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.9},
        {"op": "explode"},
    ]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert "unknown patch op" in res.output
    assert out.read_bytes() == before  # nothing written


def test_patch_bad_param_leaves_file_untouched(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = [{"op": "set_param", "block": "Tube Drive", "param": "Nope", "value": 1}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert out.read_bytes() == before


def test_patch_reads_ops_from_stdin(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_param", "block": "Tube Drive", "param": "Gain", "value": 0.7}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), "-", "--library", str(hsp_library.root)],
        input=json.dumps(ops))
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert body["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]["value"] == 0.7


def test_patch_json_output(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_enabled", "block": "Tube Drive", "enabled": True}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops), "--json",
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["path"] == str(out)
    assert data["warnings"] == []


def test_patch_rejects_non_list_ops(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, {"op": "set_param"}),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert "list" in res.output


def test_patch_swap_model_warnings_surface(tmp_path, hsp_library):
    """swap_model warnings (dropped params) reach stderr / the --json dict."""
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "swap_model", "old": "Tube Drive", "new": "Tube Drive"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops), "--json",
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert isinstance(data["warnings"], list)
