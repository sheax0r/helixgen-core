import json
from pathlib import Path
from helixgen import preset_io
from helixgen.hsp import HSP_MAGIC
from helixgen.generate import generate_preset


def test_sidecar_path():
    assert preset_io.sidecar_path(Path("/a/foo.hsp")) == Path("/a/foo.spec.json")


def test_generate_writes_sidecar(tmp_path, hsp_library):
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "Side", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, hsp_library)
    sidecar = preset_io.sidecar_path(out)
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())["name"] == "Side"


def test_load_spec_uses_sidecar(tmp_path, hsp_library):
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "Side", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, hsp_library)
    spec, path = preset_io.load_spec_for_preset(out, hsp_library)
    assert spec["name"] == "Side"
    assert path == preset_io.sidecar_path(out)


def test_load_spec_decompiles_orphan(tmp_path, hsp_library):
    # Generate, then delete the sidecar to simulate an orphan.
    spec_path = tmp_path / "in.json"
    spec_path.write_text(json.dumps(
        {"name": "Orphan", "paths": [{"blocks": [{"block": "Tube Drive"}]}]}))
    out = tmp_path / "out.hsp"
    generate_preset(spec_path, out, hsp_library)
    preset_io.sidecar_path(out).unlink()
    spec, path = preset_io.load_spec_for_preset(out, hsp_library)
    assert spec["paths"][0]["blocks"][0]["block"] == "Tube Drive"
    assert path.exists()  # sidecar written on decompile
