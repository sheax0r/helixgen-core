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


def test_patch_missing_required_field_reports_op_and_field(tmp_path, hsp_library):
    """A missing op field must report the op index + field, not a bare
    KeyError (review #6), and leave the file untouched."""
    out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = [{"op": "set_param"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert "op 0 (set_param)" in res.output
    assert "missing required field" in res.output
    assert out.read_bytes() == before


# --- controller ops (set_/remove_ footswitch + expression; bead hgc-9n4) -----
#
# These are the ONLY way to add/change a footswitch or EXP assignment on an
# existing .hsp — patch previously had no controller op, so an agent adding a
# wah/whammy/EXP to a saved tone dead-ended (the skill forbids the
# view→generate round-trip). The wiring verbs already existed in mutate.py;
# these ops register them as patch ops + add the inverse unwire verbs.


def _ctrl(body, bnn_key, *, param=None):
    """The controller dict on a block's @enabled bypass wrapper, or on one of
    its params when `param` is given (None if unwired)."""
    slot = body["preset"]["flow"][0][bnn_key]
    if param is not None:
        return (slot["slot"][0]["params"].get(param) or {}).get("controller")
    return (slot.get("@enabled") or {}).get("controller")


def test_patch_set_footswitch_writes_bypass_controller(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_footswitch", "switch": "FS2", "block": "Brit Amp",
            "label": "AMP", "color": "red"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    ctrl = _ctrl(body, "b02")
    assert ctrl is not None and ctrl["type"] == "targetbypass"
    # Source registered + scribble strip carries the label/color.
    src = str(ctrl["source"])
    entry = body["preset"]["sources"][src]
    assert entry["fs_label"] == "AMP"


def test_patch_set_footswitch_param_toggle(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_footswitch", "switch": "FS3", "block": "Tube Drive",
            "param": "Gain", "min": 0.2, "max": 0.9}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    ctrl = _ctrl(read_hsp(out), "b01", param="Gain")
    assert ctrl is not None and ctrl["type"] == "param"
    assert ctrl["min"] == 0.2 and ctrl["max"] == 0.9


def test_patch_set_expression_sweeps_param(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_expression", "pedal": "EXP1",
            "targets": [{"block": "Tube Drive", "param": "Gain"}]}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    ctrl = _ctrl(read_hsp(out), "b01", param="Gain")
    assert ctrl is not None and ctrl["type"] == "param"


def test_patch_set_footswitch_is_last_wins(tmp_path, hsp_library):
    """Re-issuing set_footswitch on the same block edits the assignment."""
    out = _setup(tmp_path, hsp_library)
    ops = [
        {"op": "set_footswitch", "switch": "FS2", "block": "Brit Amp",
         "behavior": "latching"},
        {"op": "set_footswitch", "switch": "FS2", "block": "Brit Amp",
         "behavior": "momentary"},
    ]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    assert _ctrl(read_hsp(out), "b02")["behavior"] == "momentary"


def test_patch_remove_footswitch_round_trips(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    wire = [{"op": "set_footswitch", "switch": "FS2", "block": "Brit Amp"}]
    CliRunner().invoke(cli, ["patch", str(out), _ops_file(tmp_path, wire),
                             "--library", str(hsp_library.root)])
    src = str(_ctrl(read_hsp(out), "b02")["source"])
    unwire = [{"op": "remove_footswitch", "switch": "FS2"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, unwire),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    body = read_hsp(out)
    assert _ctrl(body, "b02") is None  # controller gone
    assert src not in body["preset"]["sources"]  # source pruned


def test_patch_remove_expression_round_trips(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    wire = [{"op": "set_expression", "pedal": "EXP1",
             "targets": [{"block": "Tube Drive", "param": "Gain"}]}]
    CliRunner().invoke(cli, ["patch", str(out), _ops_file(tmp_path, wire),
                             "--library", str(hsp_library.root)])
    unwire = [{"op": "remove_expression", "pedal": "EXP1"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, unwire),
        "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    assert _ctrl(read_hsp(out), "b01", param="Gain") is None


def test_patch_remove_footswitch_errors_when_absent(tmp_path, hsp_library):
    """A no-op removal is an error (the switch name is almost certainly wrong),
    and the file is left untouched."""
    out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = [{"op": "remove_footswitch", "switch": "FS4"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert "remove" in res.output.lower()
    assert out.read_bytes() == before


def test_patch_reserved_footswitch_aborts_untouched(tmp_path, hsp_library):
    """FS6 (MODE) is reserved — the op errors and nothing is written."""
    out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = [{"op": "set_footswitch", "switch": "FS6", "block": "Brit Amp"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert out.read_bytes() == before


def test_patch_controller_op_missing_field_reported(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_expression", "pedal": "EXP1"}]  # no targets
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert "op 0 (set_expression)" in res.output
    assert "targets" in res.output


def test_patch_set_expression_bad_targets_is_clean_error(tmp_path, hsp_library):
    """Malformed `targets` (a string, not a list of dicts) must give a clean
    op-level MutateError, never a bare TypeError traceback (review MEDIUM)."""
    out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = [{"op": "set_expression", "pedal": "EXP1", "targets": "Gain"}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert not isinstance(res.exception, TypeError), res.exception
    assert "op 0 (set_expression)" in res.output
    assert out.read_bytes() == before


def test_patch_set_expression_target_missing_field_is_clean_error(tmp_path, hsp_library):
    out = _setup(tmp_path, hsp_library)
    ops = [{"op": "set_expression", "pedal": "EXP1", "targets": [{"param": "Gain"}]}]
    res = CliRunner().invoke(cli, [
        "patch", str(out), _ops_file(tmp_path, ops),
        "--library", str(hsp_library.root)])
    assert res.exit_code != 0
    assert "missing required field" in res.output
