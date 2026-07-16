"""Live authoring verbs: generate/view/set-param/enable/disable/add-block/
remove-block/swap-model/patch/ingest/register — the real CLI on the real
block library (no device needed).

NOTE: `ingest` MUST always be pointed at a scratch library via --library;
without it, ingest would WRITE into the user's real library (HELIXGEN_LIBRARY
in the suite env points at the real one, read-only by convention).
"""
from __future__ import annotations

import json

import pytest

from .conftest import HGTEST, generate_hsp, make_recipe

pytestmark = [pytest.mark.live, pytest.mark.authoring]


@pytest.fixture()
def hsp(cli, scratch, amp_blocks, tmp_path, request):
    """A per-test HGTEST .hsp with one amp block (unique tone name so the
    scratch-manifest auto-registration never collides across tests)."""
    name = f"{HGTEST} Auth {request.node.name[:24]}"
    return generate_hsp(cli, scratch, name, amp_blocks[0]["display_name"])


def _view(cli, hsp):
    code, out, err = cli("view", hsp)
    assert code == 0, err or out
    return json.loads(out)


def _float_param(amp_schema) -> str:
    for pname, schema in amp_schema["params"].items():
        if schema.get("type") == "float":
            return pname
    pytest.skip("first amp block has no float param to exercise")


def test_generate_writes_hsp_magic(cli, scratch, amp_blocks):
    hsp = generate_hsp(cli, scratch, f"{HGTEST} Gen Magic",
                       amp_blocks[0]["display_name"])
    assert hsp.read_bytes()[:8] == b"rpshnosj"


def test_generate_rejects_unknown_block(cli, scratch, tmp_path):
    recipe = make_recipe(scratch, f"{HGTEST} Gen Bad",
                         "No Such Block Exists XYZ")
    code, out, err = cli("generate", recipe, "-o", tmp_path / "bad.hsp")
    assert code != 0
    assert not (tmp_path / "bad.hsp").exists()


def test_view_projects_recipe_shape(cli, hsp, amp_blocks):
    proj = _view(cli, hsp)
    assert proj["name"].startswith(f"{HGTEST} Auth")
    blocks = proj["paths"][0]["blocks"]
    assert any(b.get("block") == amp_blocks[0]["display_name"] for b in blocks)


def test_set_param_roundtrips_through_view(cli, hsp, amp_schema):
    pname = _float_param(amp_schema)
    code, out, err = cli("set-param", hsp, amp_schema["display_name"],
                         pname, "0.42")
    assert code == 0, err or out
    proj = _view(cli, hsp)
    block = next(b for b in proj["paths"][0]["blocks"]
                 if b.get("block") == amp_schema["display_name"])
    assert abs(block["params"][pname] - 0.42) < 1e-6


def test_set_param_rejects_unknown_param(cli, hsp, amp_schema):
    code, out, err = cli("set-param", hsp, amp_schema["display_name"],
                         "NoSuchParamXYZ", "0.5")
    assert code != 0


def test_disable_then_enable(cli, hsp, amp_schema):
    name = amp_schema["display_name"]
    code, out, err = cli("disable", hsp, name)
    assert code == 0, err or out
    proj = _view(cli, hsp)
    block = next(b for b in proj["paths"][0]["blocks"] if b.get("block") == name)
    assert block.get("enabled") is False
    code, out, err = cli("enable", hsp, name)
    assert code == 0, err or out
    proj = _view(cli, hsp)
    block = next(b for b in proj["paths"][0]["blocks"] if b.get("block") == name)
    assert block.get("enabled") in (True, None)  # enabled may be implicit


def test_add_then_remove_block(cli, hsp, amp_blocks):
    # a second, DISTINCT block to insert, so name-only remove is unambiguous.
    extra = None
    for category in ("delay", "reverb", "drive", "eq"):
        code, out, err = cli("list-blocks", "--json", "--category", category)
        assert code == 0, err or out
        found = json.loads(out)
        if found:
            extra = found[0]["display_name"]
            break
    if extra is None:
        pytest.skip("library has no non-amp block to add")
    code, out, err = cli("add-block", hsp, extra)
    assert code == 0, err or out
    names = [b.get("block") for b in _view(cli, hsp)["paths"][0]["blocks"]]
    assert extra in names
    code, out, err = cli("remove-block", hsp, extra)
    assert code == 0, err or out
    names = [b.get("block") for b in _view(cli, hsp)["paths"][0]["blocks"]]
    assert extra not in names


def test_swap_model_same_category(cli, hsp, amp_blocks):
    if len(amp_blocks) < 2:
        pytest.skip("need two amp blocks in the library to swap")
    old, new = amp_blocks[0]["display_name"], amp_blocks[1]["display_name"]
    code, out, err = cli("swap-model", hsp, old, new)
    assert code == 0, err or out
    names = [b.get("block") for b in _view(cli, hsp)["paths"][0]["blocks"]]
    assert new in names and old not in names


def test_patch_atomic_ops_via_stdin(cli, hsp, amp_schema):
    pname = _float_param(amp_schema)
    ops = json.dumps([
        {"op": "set_param", "block": amp_schema["display_name"],
         "param": pname, "value": 0.31},
        {"op": "set_enabled", "block": amp_schema["display_name"],
         "enabled": False},
    ])
    code, out, err = cli("patch", hsp, "-", "--json", stdin=ops)
    assert code == 0, err or out
    assert "path" in json.loads(out)
    block = next(b for b in _view(cli, hsp)["paths"][0]["blocks"]
                 if b.get("block") == amp_schema["display_name"])
    assert abs(block["params"][pname] - 0.31) < 1e-6
    assert block.get("enabled") is False


def test_patch_invalid_op_leaves_file_untouched(cli, hsp):
    before = hsp.read_bytes()
    ops = json.dumps([{"op": "set_param", "block": "No Such Block",
                       "param": "X", "value": 1}])
    code, out, err = cli("patch", hsp, "-", stdin=ops)
    assert code != 0
    assert hsp.read_bytes() == before


def test_ingest_into_scratch_library_only(cli, hsp, tmp_path):
    # SAFETY: --library is mandatory here — never ingest into the real library.
    scratch_lib = tmp_path / "ingest-lib"
    scratch_lib.mkdir()
    code, out, err = cli("ingest", hsp, "--library", scratch_lib)
    assert code == 0, err or out
    assert (scratch_lib / "index.json").exists()


def test_register_existing_hsp(cli, hsp):
    # register_tone is idempotent for the same path (generate already
    # auto-registered this tone into the SCRATCH manifest).
    code, out, err = cli("register", hsp)
    assert code == 0, err or out
    assert "registered" in out
