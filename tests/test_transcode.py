"""Offline fidelity gate for the ``.hsp`` <-> device ``_sbepgsm`` transcoder.

Phase 1: prove ``recipe_to_sbepgsm(sbepgsm_to_recipe(D)) == D`` for real device
preset blobs, without a device or network. Fixtures are gitignored (like
``tests/fixtures/presets/``) so every test skips cleanly when absent.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

pytest.importorskip("msgpack")

from helixgen.device import content, defs  # noqa: E402
from helixgen.device import transcode  # noqa: E402

FIXDIR = Path(__file__).parent / "fixtures" / "device_content"
FIXTURES = ["preset_151", "preset_152", "preset_157"]


def _load(name: str) -> dict:
    path = FIXDIR / f"{name}.sbepgsm"
    if not path.exists():
        pytest.skip(f"device-content fixture absent: {path}")
    return content.decode_any(path.read_bytes())


def _user_cats(doc: dict):
    """Category strings the recipe should NOT model (endpoints)."""
    return {"input", "output", "looper", "split", "join", None}


# --- the fidelity gate ------------------------------------------------------

@pytest.mark.parametrize("name", FIXTURES)
def test_sfg_and_pm_roundtrip(name):
    D = _load(name)
    D_ref = copy.deepcopy(D)
    R = transcode.sbepgsm_to_recipe(D)
    # decode must not mutate its input
    assert D == D_ref, "sbepgsm_to_recipe mutated its input doc"
    D2 = transcode.recipe_to_sbepgsm(R)
    assert D2["sfg_"] == D["sfg_"], f"{name}: sfg_ diverged"
    assert D2["pm__"] == D["pm__"], f"{name}: pm__ diverged"


@pytest.mark.parametrize("name", FIXTURES)
def test_cg_and_hist_roundtrip(name):
    # We carry cg__/hist verbatim in raw, so they should also round-trip exactly.
    D = _load(name)
    D2 = transcode.recipe_to_sbepgsm(transcode.sbepgsm_to_recipe(D))
    assert D2.get("cg__") == D.get("cg__"), f"{name}: cg__ diverged"
    assert D2.get("hist") == D.get("hist"), f"{name}: hist diverged"


@pytest.mark.parametrize("name", FIXTURES)
def test_full_doc_roundtrip(name):
    D = _load(name)
    D2 = transcode.recipe_to_sbepgsm(transcode.sbepgsm_to_recipe(D))
    assert D2 == D, f"{name}: full doc diverged"


# --- modeling is real (not raw-passthrough) ---------------------------------

def test_151_models_and_params_are_lifted():
    D = _load("preset_151")
    R = transcode.sbepgsm_to_recipe(D)

    # path 0 holds the serial chain's user blocks in signal order
    blocks = R["paths"][0]["blocks"]
    names = [b["block"] for b in blocks]

    # amp, cab, and drive were lifted out as device model-id STRINGS
    assert "Agoura_AmpSolid100" in names  # amp
    assert "HD2_CabMicIr_4x12SoloLeadEMWithPan" in names  # cab
    assert "HD2_DistScream808Mono" in names  # drive/distortion

    # endpoints (input/output/looper) are NOT modeled
    assert "P35_InputInst1" not in names
    assert "P35_OutputMatrix" not in names
    assert "P35_LooperHelixStereo" not in names

    # the amp block carries plausible float params lifted from the parm list
    amp = next(b for b in blocks if b["block"] == "Agoura_AmpSolid100")
    assert amp["params"], "amp params should not be empty"
    assert any(isinstance(v, float) for v in amp["params"].values())

    # PROOF the values were lifted OUT of raw, not duplicated inside it:
    # the modeled amp block in raw.sfg_ must lack mdls[0].id__ and its named
    # parm leaves must lack 'valu'.
    amp_mid = defs.model_id_for("Agoura_AmpSolid100")
    raw_amp = None
    for item in R["raw"]["sfg_"]["flow"][0]["blks"]:
        if isinstance(item, dict):
            m0 = (item.get("mdls") or [{}])[0]
            # modeled blocks have their id__ stripped
            if "id__" not in m0 and any(
                p.get("mid_") == amp_mid for p in (m0.get("parm") or [])
            ):
                raw_amp = item
                break
    assert raw_amp is not None, "modeled amp block not found in raw"
    m0 = raw_amp["mdls"][0]
    assert "id__" not in m0, "raw still carries the model id (not lifted)"
    # every lifted param name must be missing its valu in raw
    assert any("valu" not in leaf for leaf in m0["parm"]), (
        "raw still carries param values (not lifted)"
    )


def test_151_path_block_count_matches_user_blocks():
    D = _load("preset_151")
    R = transcode.sbepgsm_to_recipe(D)
    cats = defs.load_defs()["model_categories"]
    skip = _user_cats(D)
    for fi, flow in enumerate(D["sfg_"]["flow"]):
        expected = 0
        for j in range(1, len(flow["blks"]), 2):
            b = flow["blks"][j]
            mid = (b.get("mdls") or [{}])[0].get("id__")
            cat = cats.get(defs.model_name_for(mid)) if mid is not None else None
            if cat not in skip:
                expected += 1
        assert len(R["paths"][fi]["blocks"]) == expected, f"flow {fi} block count"


# --- Phase 2: serial synthesis (authored recipe, no device-origin raw) -------

def _modeled_paths(recipe: dict):
    """Just the modeled block+params content of each path, order-preserving."""
    return [[(b["block"], b["params"]) for b in p["blocks"]]
            for p in recipe["paths"]]


def _assert_structurally_valid(doc: dict):
    """A synthesized doc must decode/re-encode cleanly and be self-consistent."""
    # re-serialize + decode: proves it is msgpack-encodable device content
    blob = content.encode_content_data(doc)
    doc2 = content.decode_any(blob)
    assert doc2["sfg_"]["flow"], "no flows"
    for fi, flow in enumerate(doc2["sfg_"]["flow"]):
        blks = flow["blks"]
        dicts = [b for b in blks if isinstance(b, dict)]
        # bcnt counts the block dicts (flat grid is [idx, block, ...])
        assert flow["bcnt"] == len(blks) // 2 == len(dicts), f"flow {fi} bcnt"
        assert len(flow["bmap"]) == flow["bcnt"], f"flow {fi} bmap length"
        for b in dicts:
            assert "hrns" in b and "id__" in b["hrns"], "block missing hrns"
            assert "type" in b, "block missing type"
            assert "id__" in b, "block missing id__"
            m0 = (b.get("mdls") or [{}])[0]
            mid = m0.get("id__")
            assert mid is not None, "block missing model id"
            assert defs.model_name_for(mid) is not None, f"unresolvable model {mid}"
    # endpoints present in flow 0
    f0_models = {(b["mdls"][0]["id__"])
                 for b in doc2["sfg_"]["flow"][0]["blks"] if isinstance(b, dict)}
    assert defs.model_id_for("P35_InputInst1") in f0_models, "no input endpoint"
    assert defs.model_id_for("P35_OutputMatrix") in f0_models, "no output endpoint"
    # cg__ present with an 8-slot snapshot array
    assert "cg__" in doc2, "cg__ missing"
    assert len(doc2["cg__"]["entt"]["snps"]) == 8, "cg__ needs 8 snapshot slots"


@pytest.mark.parametrize("name", ["preset_151", "preset_157"])
def test_synthesis_recipe_roundtrips(name):
    """Drop the device-origin ``raw`` from a modeled recipe, run the synthesis
    path, and assert the MODELED content (block model strings + params, in
    order) survives a decode round-trip. tid_/bmap/hrns will differ from the
    real preset — that is expected; only the modeled content must be faithful."""
    D = _load(name)
    R = transcode.sbepgsm_to_recipe(D)
    original = _modeled_paths(R)

    authored = {"name": R["name"], "paths": copy.deepcopy(R["paths"])}
    assert "raw" not in authored  # this is the synthesis (Phase 2) path

    synth = transcode.recipe_to_sbepgsm(authored)
    _assert_structurally_valid(synth)

    # decode the synthesized doc back into a recipe; its path 0 must carry the
    # same modeled blocks (serial synthesis collapses to a single modeled path)
    back = transcode.sbepgsm_to_recipe(synth)
    assert _modeled_paths(back)[0] == original[0], f"{name}: modeled path 0 diverged"


def test_synthesis_is_only_triggered_without_raw():
    """A recipe WITH device-origin raw must still rebuild exactly (no regression
    of the Phase-1 round-trip)."""
    D = _load("preset_151")
    R = transcode.sbepgsm_to_recipe(D)  # carries raw
    assert R.get("raw")
    D2 = transcode.recipe_to_sbepgsm(R)  # must take the rebuild path
    assert D2["sfg_"] == D["sfg_"], "raw-bearing recipe no longer rebuilds exactly"


def test_hsp_to_sbepgsm_smoke():
    """Feed a real authored serial ``.hsp`` and assert the transcoded blob
    decodes back with the expected block categories, enabled, in order. Offline."""
    hsp = pytest.importorskip("helixgen.hsp")
    path = Path("/Users/michael.shea/git/guitar-training/tones/blues-lead-lp-jr.hsp")
    if not path.exists():
        pytest.skip(f"authored .hsp fixture absent: {path}")
    body = hsp.read_hsp(path)

    blob = transcode.hsp_to_sbepgsm(body)
    doc = content.decode_any(blob)
    _assert_structurally_valid(doc)

    cats = defs.load_defs()["model_categories"]
    endpoints = {"input", "output", "looper", "split", "join", None}
    got = []
    for b in doc["sfg_"]["flow"][0]["blks"]:
        if not isinstance(b, dict):
            continue
        mid = (b.get("mdls") or [{}])[0].get("id__")
        cat = cats.get(defs.model_name_for(mid)) if mid is not None else None
        if cat in endpoints:
            continue
        assert b.get("enbl") == 1, "synthesized user block should be enabled"
        got.append(cat)

    # blues-lead-lp-jr = drive -> amp -> cab(IR) -> delay -> reverb, in order
    assert got == ["distortion", "amp", "ir", "delay", "reverb"], got


# --- IR-hash injection (irmd) ------------------------------------------------

BLUES_HSP = Path("/Users/michael.shea/git/guitar-training/tones/blues-lead-lp-jr.hsp")
BLUES_IRHASH = "3047970ab472b55a3b87314ba0a114b1"


def _ir_block(doc: dict):
    """The single enabled ir-category block dict in flow 0 of a synthesized doc."""
    cats = defs.load_defs()["model_categories"]
    for b in doc["sfg_"]["flow"][0]["blks"]:
        if not isinstance(b, dict):
            continue
        mid = (b.get("mdls") or [{}])[0].get("id__")
        cat = cats.get(defs.model_name_for(mid)) if mid is not None else None
        if cat == "ir":
            return b
    return None


def test_hsp_to_sbepgsm_injects_irmd_on_ir_cab():
    """The authored path must carry the .hsp cab slot's irhash onto the synth
    cab's ``mdls[0].irmd`` as the 16-byte hash, so the cab resolves on-device."""
    hsp = pytest.importorskip("helixgen.hsp")
    if not BLUES_HSP.exists():
        pytest.skip(f"authored .hsp fixture absent: {BLUES_HSP}")
    body = hsp.read_hsp(BLUES_HSP)

    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    ir = _ir_block(doc)
    assert ir is not None, "no ir-category block in synthesized doc"
    irmd = ir["mdls"][0].get("irmd")
    assert irmd == bytes.fromhex(BLUES_IRHASH), irmd
    assert len(irmd) == 16


def test_hsp_to_sbepgsm_non_ir_blocks_have_no_irmd():
    """Only the IR cab gets an ``irmd``; every other user block must lack it."""
    hsp = pytest.importorskip("helixgen.hsp")
    if not BLUES_HSP.exists():
        pytest.skip(f"authored .hsp fixture absent: {BLUES_HSP}")
    body = hsp.read_hsp(BLUES_HSP)

    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    cats = defs.load_defs()["model_categories"]
    for b in doc["sfg_"]["flow"][0]["blks"]:
        if not isinstance(b, dict):
            continue
        m0 = (b.get("mdls") or [{}])[0]
        mid = m0.get("id__")
        cat = cats.get(defs.model_name_for(mid)) if mid is not None else None
        if cat != "ir":
            assert "irmd" not in m0, f"non-ir block {cat} carries irmd"


def test_synthesized_ir_doc_lifts_irhash_back_to_recipe():
    """``sbepgsm_to_recipe`` exposes an existing ``mdls[0].irmd`` as recipe
    ``irhash`` hex, and the rebuild re-emits it byte-for-byte (round-trip)."""
    hsp = pytest.importorskip("helixgen.hsp")
    if not BLUES_HSP.exists():
        pytest.skip(f"authored .hsp fixture absent: {BLUES_HSP}")
    body = hsp.read_hsp(BLUES_HSP)

    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))

    # the device-origin projection exposes irhash as hex on the ir block
    recipe = transcode.sbepgsm_to_recipe(doc)
    ir_specs = [b for b in recipe["paths"][0]["blocks"] if b.get("irhash")]
    assert len(ir_specs) == 1, "expected exactly one irhash-bearing block"
    assert ir_specs[0]["irhash"] == BLUES_IRHASH

    # and it round-trips exactly through the rebuild path (irmd re-emitted)
    doc2 = transcode.recipe_to_sbepgsm(recipe)
    assert doc2 == doc, "irhash-bearing doc did not round-trip exactly"
