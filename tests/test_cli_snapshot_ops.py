"""CLI tests for the in-place snapshot-scene verbs `set-snapshot` /
`remove-snapshot` and their `patch` ops.

These prove the verbs read the `.hsp`, mutate in place, write back, round-trip
through `view`, synthesize the snapshots meta when absent, and keep `patch`
atomic (a bad op leaves the file byte-for-byte untouched).
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.generate import generate_preset
from helixgen.hsp import read_hsp, write_hsp


def _setup(tmp_path, hsp_library):
    spec = tmp_path / "in.json"
    spec.write_text(json.dumps(
        {"name": "C", "paths": [{"blocks": [
            {"block": "Tube Drive", "params": {"Gain": 0.5}}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec, out, hsp_library)
    out.with_name(out.stem + ".spec.json").unlink(missing_ok=True)
    return hsp_library.root, out


def _view(lib_root, out):
    res = CliRunner().invoke(cli, ["view", str(out), "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def test_set_snapshot_creates_and_roundtrips_through_view(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-snapshot", str(out), "--index", "1", "--name", "Lead",
        "--param", "Tube Drive", "Gain", "0.9", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    doc = _view(lib_root, out)
    lead = next(s for s in (doc.get("snapshots") or []) if s.get("name") == "Lead")
    assert lead["params"]["Tube Drive"]["Gain"] == 0.9


def test_set_snapshot_synthesizes_meta_when_absent(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    body = read_hsp(out)
    body["preset"].pop("snapshots", None)      # a preset with no snapshots meta
    write_hsp(out, body)
    res = CliRunner().invoke(cli, [
        "set-snapshot", str(out), "--index", "2", "--name", "Solo",
        "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    snaps = read_hsp(out)["preset"]["snapshots"]
    assert len(snaps) == 8
    assert snaps[2]["name"] == "Solo" and snaps[2]["valid"] is True


def test_set_snapshot_create_requires_name(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-snapshot", str(out), "--index", "1", "--library", str(lib_root)])
    assert res.exit_code != 0
    assert "name" in res.output.lower()


def test_set_snapshot_active(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "set-snapshot", str(out), "--index", "3", "--name", "Big",
        "--active", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    assert read_hsp(out)["preset"]["params"]["activesnapshot"] == 3


def test_remove_snapshot_unconfigures(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    CliRunner().invoke(cli, [
        "set-snapshot", str(out), "--index", "1", "--name", "Lead",
        "--library", str(lib_root)])
    res = CliRunner().invoke(cli, [
        "remove-snapshot", str(out), "--name", "Lead", "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    snaps = read_hsp(out)["preset"]["snapshots"]
    assert snaps[1]["name"] == "Snap 2" and snaps[1]["valid"] is False


def test_remove_snapshot_unconfigured_errors(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    res = CliRunner().invoke(cli, [
        "remove-snapshot", str(out), "--index", "4", "--library", str(lib_root)])
    assert res.exit_code != 0
    assert "not configured" in res.output.lower()


def test_patch_list_with_set_snapshot_is_atomic(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    before = out.read_bytes()
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "set_snapshot", "index": 1, "name": "Good"},
        {"op": "set_snapshot", "index": 99, "name": "Bad"},  # out of range
    ]))
    res = CliRunner().invoke(cli, [
        "patch", str(out), str(ops), "--library", str(lib_root)])
    assert res.exit_code != 0
    assert out.read_bytes() == before     # file untouched — atomic abort


def test_patch_set_snapshot_then_remove(tmp_path, hsp_library):
    lib_root, out = _setup(tmp_path, hsp_library)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "set_snapshot", "index": 1, "name": "Lead"},
        {"op": "set_snapshot", "index": 2, "name": "Clean"},
        {"op": "remove_snapshot", "name": "Lead"},
    ]))
    res = CliRunner().invoke(cli, [
        "patch", str(out), str(ops), "--library", str(lib_root)])
    assert res.exit_code == 0, res.output
    snaps = read_hsp(out)["preset"]["snapshots"]
    assert snaps[1]["valid"] is False and snaps[1]["name"] == "Snap 2"
    assert snaps[2]["name"] == "Clean"
