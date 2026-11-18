"""Legacy models with a Stadium-native successor: the map, and its surfaces."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from helixgen import home
from helixgen.cli import cli
from helixgen.library import Block
from helixgen.superseded import SUPERSEDED_BY

PLATE, DYN_PLATE = "HD2_ReverbPlateStereo", "VIC_DynPlateStereo"
SPRING, CAB = "HD2_Reverb63SpringStereo", "HD2_CabMicIr_4x12Greenback25"


def _block(model_id: str, category: str, name: str) -> Block:
    return Block(
        model_id=model_id, category=category, display_name=name,
        params={"Mix": {"type": "float"}},
        exemplar={"@model": model_id, "@type": "fx", "@enabled": True, "Mix": 0.3},
        first_seen={"preset": "_", "firmware": "_", "date": "2026-11-17"})


@pytest.fixture
def lib(hsp_library):
    for mid, name in ((PLATE, "Plate"), (DYN_PLATE, "Dynamic Plate Stereo"),
                      (SPRING, "'63 Spring")):
        hsp_library.save_block(_block(mid, "reverb", name))
    hsp_library.save_block(_block(CAB, "cab", "4x12 Greenback 25"))
    return hsp_library


def _generate(lib, tmp_path, block: str):
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps(
        {"name": "T", "paths": [{"blocks": [{"block": block}]}]}))
    out = tmp_path / f"{abs(hash(block))}.hsp"
    res = CliRunner().invoke(cli, ["generate", str(recipe), "-o", str(out),
                                   "--library", str(lib.root)])
    assert res.exit_code == 0, res.output
    return res, out


def test_map_is_explicit_not_prefix_derived():
    # Native successors only, no chains, and no-successor legacy stays out.
    assert SUPERSEDED_BY[PLATE] == DYN_PLATE
    assert not any(v.startswith("HD2_") for v in SUPERSEDED_BY.values())
    assert not set(SUPERSEDED_BY) & set(SUPERSEDED_BY.values())
    for absent in (SPRING, "HD2_ReverbTileStereo", "HD2_ReverbSpringStereo",
                   "HD2_AmpCaliIVLead"):
        assert absent not in SUPERSEDED_BY
    assert not any("Cab" in k or "Preamp" in k for k in SUPERSEDED_BY)


def test_list_blocks_marks_superseded(lib):
    res = CliRunner().invoke(cli, ["list-blocks", "--library", str(lib.root)])
    lines = res.output.splitlines()
    plate = next(l for l in lines if f"[{PLATE}]" in l and "->" in l)
    assert "superseded by Dynamic Plate Stereo" in plate
    assert sum("superseded by" in l for l in lines) == 1

    data = json.loads(CliRunner().invoke(
        cli, ["list-blocks", "--json", "--library", str(lib.root)]).stdout)
    by_id = {b["model_id"]: b["superseded_by"] for b in data}
    assert by_id[PLATE] == {"display_name": "Dynamic Plate Stereo",
                            "model_id": DYN_PLATE}
    assert by_id[SPRING] is None and by_id[CAB] is None


def test_show_block_marks_superseded(lib):
    r = CliRunner()
    assert "superseded by: Dynamic Plate Stereo" in r.invoke(
        cli, ["show-block", "Plate", "--library", str(lib.root)]).output
    assert "superseded" not in r.invoke(
        cli, ["show-block", "'63 Spring", "--library", str(lib.root)]).output
    data = json.loads(r.invoke(
        cli, ["show-block", PLATE, "--json", "--library", str(lib.root)]).stdout)
    assert data["superseded_by"]["model_id"] == DYN_PLATE


def test_generate_warns_on_superseded_only(lib, tmp_path):
    res, _ = _generate(lib, tmp_path, "Plate")
    assert "warning" in res.stderr and "Dynamic Plate Stereo" in res.stderr
    for clean in ("'63 Spring", "4x12 Greenback 25", "Dynamic Plate Stereo"):
        res, _ = _generate(lib, tmp_path, clean)
        assert "superseded" not in res.stderr


def test_no_marker_when_successor_missing_from_library(hsp_library):
    hsp_library.save_block(_block(PLATE, "reverb", "Plate"))
    res = CliRunner().invoke(
        cli, ["list-blocks", "--library", str(hsp_library.root)])
    assert "superseded" not in res.output


def test_library_audit_is_read_only_report(lib, tmp_path, tmp_home):
    _, legacy = _generate(lib, tmp_path, "Plate")
    _, native = _generate(lib, tmp_path, "'63 Spring")
    tones = home.tones_dir()
    tones.mkdir(parents=True, exist_ok=True)
    (tones / "old.hsp").write_bytes(legacy.read_bytes())
    (tones / "fine.hsp").write_bytes(native.read_bytes())
    (tones / "junk.hsp").write_bytes(b"rpshnosj[1,2]")  # wrong shape: skipped
    before = (tones / "old.hsp").read_bytes()

    res = CliRunner().invoke(
        cli, ["library", "audit", "--json", "--library", str(lib.root)])
    assert res.exit_code == 0, res.output
    # (`generate -o` under tmp_home also lands its own copies in the library)
    rows = {r["hsp"]: r for r in json.loads(res.stdout)["tones"]}
    assert "old.hsp" in rows and "fine.hsp" not in rows
    assert rows["old.hsp"]["superseded"][0]["superseded_by"]["model_id"] == DYN_PLATE
    assert (tones / "old.hsp").read_bytes() == before
