"""Feature-parity regression guard for the MCP-server removal (0.20.0).

The MCP server was removed and the CLI became the ONLY engine surface; the
agent contract that used to live in MCP tool descriptions now lives in click
help text. This module pins that contract:

  1. Every removed MCP tool maps to a CLI verb that EXISTS (click
     introspection), and that verb's help carries the designated key
     contract phrases (the agent-facing gotchas/semantics that must never
     silently drop out of `--help`).
  2. Each agent-consumed `--json` output is valid JSON with the expected
     shape.
  3. The package no longer ships `mcp_server` and the top-level help orients
     an agent (verb groups + mental models).

If you rename a verb or reword help, update the table here in the same
commit — a failure means the help-as-contract surface regressed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helixgen.cli import cli

REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve(path: list[str]):
    """Resolve a click command by its subcommand path, e.g. ["device", "sync"]."""
    cmd = cli
    for name in path:
        assert hasattr(cmd, "commands"), f"{name}: parent is not a group"
        assert name in cmd.commands, (
            f"CLI verb {' '.join(path)!r} missing (looking for {name!r}; "
            f"have {sorted(cmd.commands)})")
        cmd = cmd.commands[name]
    return cmd


def _full_help(cmd) -> str:
    """The verb's raw help string plus every option's help string,
    whitespace-normalized so phrases spanning source line breaks match."""
    opt_help = " ".join(
        getattr(p, "help", None) or "" for p in cmd.params)
    return " ".join(((cmd.help or "") + "\n" + opt_help).split())


# (removed MCP tool, CLI verb path, key contract phrases that must stay in
#  the verb's --help). Phrases are checked against the RAW help string (not
#  the wrapped render), so multi-word phrases are safe.
PARITY: list[tuple[str, list[str], list[str]]] = [
    ("list_blocks", ["list-blocks"], ["show-block", "amp"]),
    ("show_block", ["show-block"], ["case-sensitive", "before"]),
    ("generate_preset", ["generate"],
     ["Unknown param(s)", "show-block", "source of truth", "recipe-reference"]),
    ("list_irs", ["list-irs"], ["stock cab", "basename", "mapping.json"]),
    ("compute_irhash", ["irhash"], ["48 kHz", "left channel", "libsndfile"]),
    ("discover_irs", ["irhash"], ["directory", "stateless"]),
    ("register_ir", ["register-irs"], ["Stadium", "hash"]),
    ("register_irs", ["ir-scan"], ["Recursively", "cache"]),
    ("view_preset", ["view"], ["read-only", "NOT authoritative", "unknown_controllers"]),
    ("controller_mapping", ["controllers"], ["English", "reserved"]),
    ("patch_preset", ["patch"],
     ["atomically", "untouched", "set_param", "set_enabled", "add_block",
      "remove_block", "swap_model", "show-block", "pseudo-block"]),
    # --- device mirrors ---
    ("device_list_presets", ["device", "list"], ["CID", "Read-only"]),
    ("device_list_setlists", ["device", "setlists"], ["setlist containers"]),
    ("device_read_preset", ["device", "read"], ["content ref"]),
    ("device_load_preset", ["device", "load"], ["edit buffer"]),
    ("device_create_preset", ["device", "create"], ["new CID"]),
    ("device_rename_preset", ["device", "rename"], ["Rename"]),
    ("device_delete_preset", ["device", "delete"], ["Delete"]),
    ("device_set_param", ["device", "set-param"],
     ["RAW units", "NOT normalized", "device blocks", "ACTIVE tone"]),
    ("device_info", ["device", "info"], ["Read-only", "firmware"]),
    ("device_settings_list", ["device", "settings", "list"], ["page", "offline"]),
    ("device_settings_get", ["device", "settings", "get"], ["enum"]),
    ("device_settings_set", ["device", "settings", "set"], ["enum label"]),
    ("device_globaleq_list", ["device", "globaleq", "list"], ["offline"]),
    ("device_globaleq_set", ["device", "globaleq", "set"], ["qtr", "level"]),
    ("device_tuner", ["device", "tuner"], ["pitch", "Play a note"]),
    ("device_snapshot", ["device", "snapshot"], ["ACTIVE tone"]),
    ("device_blocks", ["device", "blocks"], ["coordinates", "saved"]),
    ("device_bypass", ["device", "bypass"], ["volatile", "ACTIVE tone"]),
    ("device_model", ["device", "model"], ["cross-category", "ACTIVE tone"]),
    ("device_save_preset", ["device", "save"], ["edit buffer", "empty"]),
    ("device_install_preset", ["device", "install"],
     ["Transcodes", "empty", "auto-irs", "SILENT"]),
    ("device_import_hss", ["device", "setlist", "import-hss"],
     ["NOT idempotent", "offline", "PATHLESS"]),
    ("device_export_hss", ["device", "setlist", "export-hss"],
     ["SKIPPED", "local `.hsp`"]),
    ("device_setlist_list", ["device", "setlist", "list"], ["manifest"]),
    ("device_setlist_add", ["device", "setlist", "add"],
     ["many setlists", "Idempotent"]),
    ("device_setlist_remove", ["device", "setlist", "remove"],
     ["membership", "Local-only"]),
    ("device_sync_setlist", ["device", "sync"],
     ["never orphaning", "Idempotent", "re-run", "untracked"]),
    ("device_sync_all", ["device", "sync"], ["every setlist", "--all"]),
    ("device_delete_ir", ["device", "delete-ir"],
     ["silent cab", "wedge", "lagging"]),
    ("device_rename_ir", ["device", "rename-ir"], ["hash", "display name"]),
    ("device_ir_prune", ["device", "ir-prune"],
     ["DRY-RUN", "--force", "--ignore-warnings", "protected"]),
    ("device_set_info", ["device", "set-info"],
     ["color", "notes", "non-activating"]),
    ("device_setlist_create", ["device", "setlist", "create"],
     ["already", "device"]),
    ("device_setlist_rename", ["device", "setlist", "rename"], ["manifest"]),
    ("device_setlist_delete", ["device", "setlist", "delete"],
     ["never-orphan", "pool"]),
    ("device_setlist_duplicate", ["device", "setlist", "duplicate"],
     ["shared, not copied"]),
    ("device_reorder", ["device", "reorder"],
     ["DEVICE-side", "cid", "slots reorder"]),
    ("device_meters", ["device", "meters"], ["telemetry", "Read-only"]),
    ("device_measure", ["device", "measure"],
     ["level-matching", "read-only", "PLAY STEADILY"]),
]


@pytest.mark.parametrize(
    "tool,path,phrases", PARITY, ids=[row[0] for row in PARITY])
def test_removed_mcp_tool_maps_to_cli_verb_with_contract_help(tool, path, phrases):
    cmd = _resolve(path)
    combined = _full_help(cmd)
    for phrase in phrases:
        assert phrase in combined, (
            f"{tool}: CLI verb {' '.join(path)!r} help lost the contract "
            f"phrase {phrase!r}")


def test_every_mcp_tool_is_in_the_parity_table():
    """The table above must keep covering the full removed-tool inventory."""
    assert len({row[0] for row in PARITY}) == 50


def test_mcp_server_package_is_gone():
    assert not (REPO_ROOT / "mcp_server").exists()
    assert not (REPO_ROOT / "tests" / "mcp_server").exists()
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "mcp" not in pyproject.lower()


def test_top_level_help_orients_agents():
    res = CliRunner().invoke(cli, ["--help"])
    assert res.exit_code == 0
    raw = cli.help or ""
    for phrase in ["show-block", "case-sensitive", "source of truth",
                   "MUTATE", "idempotent", "--json", "docs/CLI.md",
                   "recipe-reference"]:
        assert phrase in raw, f"top-level help lost {phrase!r}"


def test_device_group_help_carries_mental_models():
    raw = _resolve(["device"]).help or ""
    for phrase in ["READ vs WRITE", "MUTATES", "ACTIVE tone", "flaky",
                   "re-run", "idempotent", "tone library",
                   "never touches untracked", "docs/CLI.md"]:
        assert phrase in raw, f"device group help lost {phrase!r}"


# --- --json machine-readable output shapes ---------------------------------

def test_json_list_blocks_shape(hsp_library):
    res = CliRunner().invoke(
        cli, ["list-blocks", "--json", "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert {"display_name", "model_id", "category"} <= set(data[0])


def test_json_show_block_shape(hsp_library):
    res = CliRunner().invoke(
        cli, ["show-block", "Brit Amp", "--json",
              "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert {"display_name", "model_id", "category", "params"} <= set(data)


def test_json_list_irs_shape(tmp_path):
    irs_dir = tmp_path / "irs"
    irs_dir.mkdir()
    (irs_dir / "mapping.json").write_text(json.dumps({"ab" * 16: "/x/a.wav"}))
    res = CliRunner().invoke(
        cli, ["list-irs", "--json", "--irs-dir", str(irs_dir)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data and {"hash", "path"} <= set(data[0])


def test_json_controllers_shape():
    res = CliRunner().invoke(cli, ["controllers", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data and {"id", "english"} <= set(data[0])


def test_json_view_stdout(tmp_path, hsp_library):
    from helixgen.generate import generate_preset

    spec = tmp_path / "in.json"
    spec.write_text(json.dumps(
        {"name": "J", "paths": [{"blocks": [{"block": "Brit Amp"}]}]}))
    out = tmp_path / "j.hsp"
    generate_preset(spec, out, hsp_library)
    res = CliRunner().invoke(
        cli, ["view", str(out), "--library", str(hsp_library.root)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert {"name", "paths"} <= set(data)


def test_json_device_offline_verbs():
    """Offline (manifest/catalog) device verbs emit valid JSON."""
    for args, check in [
        (["device", "setlist", "list", "--json"], dict),
        (["device", "library", "--json"], list),
        (["device", "slots", "list", "--json"], list),
        (["device", "globaleq", "list", "--json"], list),
        (["device", "settings", "list", "--json"], dict),
    ]:
        res = CliRunner().invoke(cli, args)
        assert res.exit_code == 0, (args, res.output)
        assert isinstance(json.loads(res.stdout), check), args


def test_json_device_local_list(tmp_path):
    res = CliRunner().invoke(
        cli, ["device", "local-list", "--json", "--dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert isinstance(json.loads(res.stdout), list)


def test_json_device_networked_verbs(monkeypatch):
    """Networked read verbs emit valid JSON (canned fake client)."""
    import helixgen.device as device_mod

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def list_presets(self, container=-2, *, strict=False):
            return [{"cid_": 1, "name": "T", "cctp": 1000, "posi": 0}]

        def list_setlists(self, *, strict=False):
            return [{"cid_": -2, "name": "User"}]

        def get_ref(self, cid):
            return {"cid_": cid, "name": "T", "cpid": -2, "posi": 0}

        def edit_buffer_blocks(self):
            return [{"path": 0, "block": 1, "model": "HD2_AmpBrit",
                     "model_id": 1, "enabled": True}]

        def list_irs(self, strict=False):
            return [{"hash": "ab" * 16, "name": "IR", "mono": True}]

        def product_info(self):
            return {"model": "Stadium", "device_id": 1, "serial": "x",
                    "firmware": "2.0", "sd_total_bytes": 1,
                    "sd_available_bytes": 1, "raw": {}}

    monkeypatch.setattr(device_mod, "HelixClient", FakeClient)
    for args, check in [
        (["device", "list", "--json"], list),
        (["device", "setlists", "--json"], list),
        (["device", "read", "1", "--json"], dict),
        (["device", "blocks", "--json"], list),
        (["device", "list-irs", "--json"], list),
        (["device", "info", "--json"], dict),
    ]:
        res = CliRunner().invoke(cli, args)
        assert res.exit_code == 0, (args, res.output)
        assert isinstance(json.loads(res.stdout), check), args
