"""Live library verbs: list-blocks / show-block / controllers against the
user's REAL block library (read-only; no device needed)."""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.live, pytest.mark.library]


def test_list_blocks_text(cli):
    code, out, err = cli("list-blocks")
    assert code == 0, err or out
    assert out.strip() and "(no blocks in library)" not in out


def test_list_blocks_json_category_filter(cli, amp_blocks):
    assert all(b["category"] == "amp" for b in amp_blocks)
    assert all({"display_name", "model_id", "category"} <= set(b) for b in amp_blocks)


def test_show_block_text_and_json(cli, amp_blocks):
    name = amp_blocks[0]["display_name"]
    code, out, err = cli("show-block", name)
    assert code == 0, err or out
    assert name in out and "params:" in out
    code, out, err = cli("show-block", name, "--json")
    assert code == 0, err or out
    schema = json.loads(out)
    assert schema["display_name"] == name
    assert isinstance(schema["params"], dict) and schema["params"]


def test_show_block_unknown_errors(cli):
    code, out, err = cli("show-block", "No Such Block Exists XYZ")
    assert code != 0


def test_controllers_text_and_json(cli):
    code, out, err = cli("controllers")
    assert code == 0, err or out
    assert "Footswitch" in out or "FS" in out
    code, out, err = cli("controllers", "--json")
    assert code == 0, err or out
    entries = json.loads(out)
    assert isinstance(entries, list) and entries
