"""`helixgen changes` renders the change registry as a changelog (Task 2)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from helixgen import changes
from helixgen.cli import cli


@pytest.fixture
def one_entry(monkeypatch):
    entry = changes.ChangeEntry(
        id="fs-bypass-snapshot-assign",
        landed_in="0.84.0",
        kind="bugfix",
        summary="footswitch-bypass blocks are snapshot-assigned",
        research="pre-0.84 wire_footswitch left @enabled.snapshots unset",
        detect=lambda ctx: changes.Finding(False, [], "unchanged"),
        apply=lambda ctx: changes.Finding(False, [], "unchanged"),
    )
    monkeypatch.setattr(changes, "REGISTRY", [entry])
    return entry


def test_changes_lists_registry_entries(one_entry):
    res = CliRunner().invoke(cli, ["changes"])
    assert res.exit_code == 0, res.output
    assert "fs-bypass-snapshot-assign" in res.output
    assert "0.84.0" in res.output
    assert "bugfix" in res.output
    assert "snapshot-assigned" in res.output


def test_changes_since_excludes_entries_at_or_below_version(one_entry):
    assert "fs-bypass-snapshot-assign" not in CliRunner().invoke(
        cli, ["changes", "--since", "0.84.0"]).output
    assert "fs-bypass-snapshot-assign" in CliRunner().invoke(
        cli, ["changes", "--since", "0.80.0"]).output


def test_changes_json_shape(one_entry):
    res = CliRunner().invoke(cli, ["changes", "--json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    assert rows[0]["id"] == "fs-bypass-snapshot-assign"
    assert rows[0]["landed_in"] == "0.84.0"
    assert rows[0]["kind"] == "bugfix"
    assert rows[0]["research"].startswith("pre-0.84")


def test_changes_empty_registry(monkeypatch):
    monkeypatch.setattr(changes, "REGISTRY", [])
    res = CliRunner().invoke(cli, ["changes"])
    assert res.exit_code == 0, res.output
