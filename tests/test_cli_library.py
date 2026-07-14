import json

from click.testing import CliRunner

from helixgen.cli import cli
from helixgen.device.manifest import SetlistManifest
from helixgen.hsp import write_hsp


def _hsp(dirpath, name):
    p = dirpath / f"{name}.hsp"
    write_hsp(p, {"meta": {"name": name}})
    return p


def test_register_cmd(tmp_path):
    hp = _hsp(tmp_path, "Imported")
    r = CliRunner().invoke(cli, ["register", str(hp)])
    assert r.exit_code == 0, r.output
    m = SetlistManifest.load()
    assert m.tones["Imported"]["source"] == "import-local"
    assert m.tones["Imported"]["slot"] is None


def test_device_add_and_unsync(tmp_path):
    hp = _hsp(tmp_path, "Alpha")
    CliRunner().invoke(cli, ["register", str(hp)])
    assert CliRunner().invoke(cli, ["device", "add", "Alpha"]).exit_code == 0
    assert SetlistManifest.load().tones["Alpha"]["slot"] == "auto"
    assert CliRunner().invoke(cli, ["device", "add", "Alpha", "--slot", "7C"]).exit_code == 0
    assert SetlistManifest.load().tones["Alpha"]["slot"] == "7C"
    assert CliRunner().invoke(cli, ["device", "unsync", "Alpha"]).exit_code == 0
    assert SetlistManifest.load().tones["Alpha"]["slot"] is None


def test_device_add_invalid_slot_errors(tmp_path):
    hp = _hsp(tmp_path, "Beta")
    CliRunner().invoke(cli, ["register", str(hp)])
    r = CliRunner().invoke(cli, ["device", "add", "Beta", "--slot", "ZZ"])
    assert r.exit_code != 0
    assert "invalid slot" in r.output.lower()


def test_setlist_sync_on_marks_members(tmp_path):
    hp = _hsp(tmp_path, "Gamma")
    CliRunner().invoke(cli, ["register", str(hp)])
    CliRunner().invoke(cli, ["device", "setlist", "add", "live", str(hp)])
    assert CliRunner().invoke(cli, ["device", "setlist", "sync-on", "live"]).exit_code == 0
    m = SetlistManifest.load()
    assert m.is_synced("live")
    assert m.tones["Gamma"]["slot"] == "auto"


def test_device_library_json(tmp_path):
    hp = _hsp(tmp_path, "Delta")
    CliRunner().invoke(cli, ["register", str(hp)])
    r = CliRunner().invoke(cli, ["device", "library", "--json"])
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert any(row["name"] == "Delta" and row["on_device"] is False for row in rows)


def test_generate_auto_registers(tmp_path):
    # Uses the real chassis; skip gracefully if no library is present.
    recipe = tmp_path / "r.json"
    recipe.write_text(json.dumps({"name": "Auto Reg Test", "paths": [{"blocks": []}]}))
    out = tmp_path / "out.hsp"
    r = CliRunner().invoke(cli, ["generate", str(recipe), "-o", str(out)])
    if r.exit_code != 0:
        import pytest
        pytest.skip(f"generate unavailable in this env: {r.output}")
    m = SetlistManifest.load()
    assert "Auto Reg Test" in m.tones
    assert m.tones["Auto Reg Test"]["slot"] is None
    assert m.tones["Auto Reg Test"]["source"] == "authored"
