"""CLI tests for surgical .hsp edit commands (set-param, enable, disable,
add-block, remove-block, swap-model) under the .hsp-canonical redesign.

These verbs now read the `.hsp` directly, mutate the body in place via
`helixgen.mutate`, and write it straight back out -- no `.spec.json` sidecar
is read or written at any point.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.generate import generate_preset
from helixgen.hsp import read_hsp


def _sidecar(hsp_path):
    return hsp_path.with_name(hsp_path.stem + ".spec.json")


def _setup(tmp_path, hsp_library):
    """Generate a minimal preset (Tube Drive only) and return (lib_root, out_hsp_path)."""
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "C", "paths": [{"blocks": [{"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}
    ))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, hsp_library)
    # generate_preset (the legacy compile path, still used for .hlx) writes a
    # sidecar; delete it so these tests only assert on what the *new* edit
    # verbs do to the .hsp itself.
    _sidecar(out).unlink(missing_ok=True)
    return hsp_library.root, out


def test_cli_set_param_mutates_hsp_no_sidecar(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Tube Drive", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    slot = body["preset"]["flow"][0]["b01"]["slot"][0]
    assert slot["params"]["Gain"]["value"] == 0.9
    assert not _sidecar(out).exists()


def test_cli_set_param_snapshot_writes_override_slot(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Tube Drive", "Gain", "0.9",
        "--snapshot", "1", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    wrapped = read_hsp(out)["preset"]["flow"][0]["b01"]["slot"][0]["params"]["Gain"]
    # slot 1 overridden; other slots densified to the base; base untouched
    # (active snapshot is 0)
    assert wrapped["snapshots"] == [0.5, 0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    assert wrapped["value"] == 0.5


def test_cli_set_param_snapshot_output_level(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "output", "level",
        "--snapshot", "1", "--library", str(lib_root), "--", "-3"])
    assert res.exit_code == 0, res.output
    wrapped = read_hsp(out)["preset"]["flow"][0]["b13"]["slot"][0]["params"]["gain"]
    assert wrapped["snapshots"][1] == -3.0
    assert wrapped["snapshots"][0] == wrapped["value"]


def test_cli_enable_block(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    CliRunner().invoke(cli, [
        "disable", str(out), "Tube Drive", "--library", str(lib_root)])
    res = CliRunner().invoke(cli, [
        "enable", str(out), "Tube Drive", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert body["preset"]["flow"][0]["b01"]["@enabled"]["value"] is True
    assert not _sidecar(out).exists()


def test_cli_disable_block(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "disable", str(out), "Tube Drive", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert body["preset"]["flow"][0]["b01"]["@enabled"]["value"] is False
    assert not _sidecar(out).exists()


def test_cli_add_block(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "add-block", str(out), "Brit Amp", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    models = [
        body["preset"]["flow"][0][k]["slot"][0]["model"]
        for k in sorted(body["preset"]["flow"][0])
        if k.startswith("b") and k not in ("b00", "b13") and k[1:].isdigit()
    ]
    assert "HD2_AmpBrit" in models
    assert not _sidecar(out).exists()


def test_cli_remove_block(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "remove-block", str(out), "Tube Drive", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert "b01" not in body["preset"]["flow"][0]
    assert not _sidecar(out).exists()


def test_cli_swap_model_same_category_only(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    # Tube Drive (drive) -> Brit Amp (amp) is a category mismatch; should fail.
    res = CliRunner().invoke(cli, [
        "swap-model", str(out), "Tube Drive", "Brit Amp", "--library", str(lib_root)])
    assert res.exit_code != 0
    assert not _sidecar(out).exists()


def test_cli_unknown_block_errors(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Ghost", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code != 0
    assert "Ghost" in res.output
    assert not _sidecar(out).exists()


def test_cli_index_option_removed(tmp_path, hsp_library):
    """mutate's addressing is (path, lane, pos) -- the old --index option is gone."""
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-param", str(out), "Tube Drive", "Gain", "0.9", "--index", "0",
        "--library", str(lib_root)])
    assert res.exit_code != 0
