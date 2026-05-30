"""Spec parser tests for the optional `ir` field on block entries."""
import json
from pathlib import Path

import pytest

from helixgen.spec import SpecError, parse_spec


def _write_spec(path: Path, blocks: list[dict]) -> None:
    path.write_text(json.dumps({"name": "t", "paths": [{"blocks": blocks}]}))


def test_ir_field_basename_carried_through(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "With Pan", "ir": "foo.wav"}])
    spec = parse_spec(json.loads(p.read_text()), source="s.json")
    block = spec.paths[0].blocks[0]
    assert block.ir == "foo.wav"


def test_ir_field_hash_carried_through(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "With Pan", "ir": "ad8182e1ebe9fd95dffde5dd54b6d89c"}])
    spec = parse_spec(json.loads(p.read_text()), source="s.json")
    assert spec.paths[0].blocks[0].ir == "ad8182e1ebe9fd95dffde5dd54b6d89c"


def test_block_without_ir_field_has_none(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "Brit Plexi Brt", "params": {"Drive": 0.7}}])
    spec = parse_spec(json.loads(p.read_text()), source="s.json")
    assert spec.paths[0].blocks[0].ir is None


def test_ir_field_rejects_non_string(tmp_path):
    p = tmp_path / "s.json"
    _write_spec(p, [{"block": "With Pan", "ir": 42}])
    with pytest.raises(SpecError, match="ir.*str"):
        parse_spec(json.loads(p.read_text()), source="s.json")
