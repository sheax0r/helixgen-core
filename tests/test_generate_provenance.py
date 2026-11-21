"""`helixgen generate` stamps per-variant provenance (Task 1).

The default library write (no `-o`) records, on the variant, the recipe that
produced it, human prompt/research summaries, and the engine + plugin versions
— so a later migration/regeneration does not re-derive intent. The legacy
`-o` path writes no metadata and so no provenance.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

import helixgen
from helixgen import home, tone_meta
from helixgen.cli import cli


def _write_recipe(tmp_path: Path) -> Path:
    spec = tmp_path / "recipe.json"
    spec.write_text(json.dumps(
        {"name": "Recipe Title", "paths": [{"blocks": [{"block": "Brit Amp"}]}]}))
    return spec


def test_generate_stamps_provenance_on_the_variant(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    res = CliRunner().invoke(cli, [
        "generate", str(spec),
        "--descriptor", "Warm Jazz Clean",
        "--prompt-summary", "warm cleanish jazz comp",
        "--research-summary", "picked Brit clean, low gain",
        "--plugin-version", "5.30.0",
        "--library", str(hsp_library.root),
    ])
    assert res.exit_code == 0, res.output

    prov = tone_meta.load_tone_meta("warm-jazz-clean").variants["generic"].provenance
    assert prov is not None
    assert prov["recipe"] == json.loads(spec.read_text())
    assert prov["engine_version"] == helixgen.__version__
    assert prov["prompt_summary"] == "warm cleanish jazz comp"
    assert prov["research_summary"] == "picked Brit clean, low gain"
    assert prov["plugin_version"] == "5.30.0"
    assert isinstance(prov["authored_at"], str) and prov["authored_at"]


def test_generate_without_summaries_still_stamps_recipe_and_versions(
    tmp_home, hsp_library, tmp_path
):
    spec = _write_recipe(tmp_path)
    res = CliRunner().invoke(cli, [
        "generate", str(spec), "--descriptor", "Warm Jazz Clean",
        "--library", str(hsp_library.root),
    ])
    assert res.exit_code == 0, res.output
    prov = tone_meta.load_tone_meta("warm-jazz-clean").variants["generic"].provenance
    assert prov["recipe"] == json.loads(spec.read_text())
    assert prov["engine_version"] == helixgen.__version__
    # absent optional fields are omitted, not written as null
    assert "prompt_summary" not in prov
    assert "plugin_version" not in prov


def test_legacy_output_path_writes_no_metadata(tmp_home, hsp_library, tmp_path):
    spec = _write_recipe(tmp_path)
    out = tmp_path / "explicit.hsp"
    res = CliRunner().invoke(cli, [
        "generate", str(spec), "-o", str(out), "--library", str(hsp_library.root),
    ])
    assert res.exit_code == 0, res.output
    assert out.exists()
    assert not (home.tones_dir() / "recipe-title.json").exists()
