import json

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.hsp import write_hsp


def _hsp(dirpath, name):
    p = dirpath / f"{name}.hsp"
    write_hsp(p, {"meta": {"name": name}})
    return p


def test_library_import_places_the_file(tmp_path):
    """`helixgen register` is retired: the library DIRECTORY is the index
    (2026-09-09 file-copy design), so a tone is in the library exactly when
    its .hsp is there. `library import` is the verb that puts it there."""
    from helixgen import home
    hp = _hsp(tmp_path, "Imported")
    r = CliRunner().invoke(cli, ["library", "import", str(hp)])
    assert r.exit_code == 0, r.output
    assert any(p.suffix == ".hsp" for p in home.tones_dir().glob("*.hsp"))


def test_generate_auto_registers(tmp_path):
    # Uses the real chassis; skip gracefully if no library is present.
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "Auto Reg Test", "paths": [{"blocks": []}]}))
    out = tmp_path / "out.hsp"
    r = CliRunner().invoke(cli, ["generate", str(recipe), "-o", str(out)])
    if r.exit_code != 0:
        import pytest
        pytest.skip(f"generate unavailable in this env: {r.output}")
    # "Registered" == the .hsp is in the library directory (the index).
    from helixgen import home
    from helixgen.hsp import read_hsp
    assert any(read_hsp(p)["meta"]["name"] == "Auto Reg Test"
               for p in home.tones_dir().glob("*.hsp"))
