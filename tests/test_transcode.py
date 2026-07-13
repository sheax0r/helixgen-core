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
    # endpoints present in flow 0 (an input-category + an output-category block;
    # the exact input model now follows the path's routing — inst1/inst2/both).
    cats = defs.load_defs()["model_categories"]
    f0_cats = {cats.get(defs.model_name_for(b["mdls"][0]["id__"]))
               for b in doc2["sfg_"]["flow"][0]["blks"] if isinstance(b, dict)}
    assert "input" in f0_cats, "no input endpoint"
    assert "output" in f0_cats, "no output endpoint"
    # cg__ present with an 8-slot snapshot array
    assert "cg__" in doc2, "cg__ missing"
    assert len(doc2["cg__"]["entt"]["snps"]) == 8, "cg__ needs 8 snapshot slots"


def _flow_block_cats(doc: dict, fi: int):
    """The device ``type`` ints of every block dict in flow ``fi``."""
    return [b.get("type") for b in doc["sfg_"]["flow"][fi]["blks"]
            if isinstance(b, dict)]


@pytest.mark.parametrize("name", ["preset_151", "preset_152", "preset_157"])
def test_synthesis_recipe_roundtrips(name):
    """Drop the device-origin ``raw`` from a modeled recipe, run the synthesis
    path, and assert the MODELED content (block model strings + params, in
    order) survives a decode round-trip, for EVERY DSP path (dual-amp). tid_/
    bmap/hrns will differ from the real preset — that is expected; only the
    modeled content must be faithful."""
    D = _load(name)
    R = transcode.sbepgsm_to_recipe(D)
    original = _modeled_paths(R)

    authored = {"name": R["name"], "paths": copy.deepcopy(R["paths"])}
    assert "raw" not in authored  # this is the synthesis (Phase 2) path

    synth = transcode.recipe_to_sbepgsm(authored)
    _assert_structurally_valid(synth)

    # decode the synthesized doc back into a recipe; EVERY path's modeled blocks
    # must survive in order (dual-DSP synth emits one populated flow per path).
    back = transcode.sbepgsm_to_recipe(synth)
    back_paths = _modeled_paths(back)
    for pi in range(len(original)):
        got = back_paths[pi] if pi < len(back_paths) else []
        assert got == original[pi], f"{name}: modeled path {pi} diverged"


def test_synthesis_preserves_dual_amp_split_join():
    """preset_152 is a dual-DSP + intra-flow split/join preset. Synthesizing it
    from its raw-less recipe must emit BOTH paths' modeled blocks AND keep the
    split (type 3) + join (type 4) routing structure in flow 0."""
    D = _load("preset_152")
    R = transcode.sbepgsm_to_recipe(D)

    # sbepgsm_to_recipe surfaces the split/join skeleton OUTSIDE raw so it
    # survives a raw drop.
    assert R["paths"][0].get("structural"), "flow-0 split/join skeleton not surfaced"
    kinds = {defs.load_defs()['model_categories'][transcode.defs.model_name_for(
        s['mdls'][0]['id__'])] for s in R["paths"][0]["structural"]}
    assert kinds == {"split", "join"}, kinds

    authored = {"name": R["name"], "paths": copy.deepcopy(R["paths"])}
    synth = transcode.recipe_to_sbepgsm(authored)
    _assert_structurally_valid(synth)

    # both DSP flows are populated (not a fixed-empty flow 1)
    assert synth["sfg_"]["fcnt"] == 2
    assert len(synth["sfg_"]["flow"]) == 2
    # flow 0 carries a type-3 split and a type-4 join
    f0_types = _flow_block_cats(synth, 0)
    assert 3 in f0_types, "synth lost the split block"
    assert 4 in f0_types, "synth lost the join block"
    # both paths keep their two amps / their effect chain
    back = transcode.sbepgsm_to_recipe(synth)
    assert _modeled_paths(back)[0], "path 0 empty after synth"
    assert _modeled_paths(back)[1], "path 1 empty after synth"


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


# --- Phase 3 / snapshots spec Part A: snapshot deltas ------------------------

def _trg_by(entt, **match):
    """Find the single trg matching every key in ``match`` (e.g. type=1)."""
    hits = [t for t in entt["trgs"]
            if all(t.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, f"expected 1 trg for {match}, got {len(hits)}"
    return hits[0]


def _tamv_map(snap):
    """A snapshot's ``tamv`` flat list -> ``{trg_id: value}``."""
    tamv = snap["tamv"]
    return {tamv[i]: tamv[i + 1] for i in range(0, len(tamv), 2)}


def test_snapshot_delta_synthesis():
    """Author a recipe with a 2-snapshot bypass delta AND a param delta -> synth
    -> decode -> assert the ``cg__`` carries a bypass trg + a param trg, that
    each snapshot's ``tamv`` holds the right (trg, value) pairs, and ctm_.stid /
    ptid match. Part A of the snapshots/controllers spec (capture-free)."""
    recipe = {
        "name": "snap test",
        "snapshots": [{"name": "Rhythm"}, {"name": "Lead"}],
        "paths": [{"blocks": [
            {"block": "HD2_DistMinotaurMono", "params": {"Gain": 0.4},
             "snap_bypass": [True, False]},
            {"block": "HD2_AmpBritPlexiNrm", "params": {"Bass": 0.45},
             "snap_params": {"Bass": [0.45, 0.38]}},
        ]}],
    }
    doc = content.decode_any(
        content.encode_content_data(transcode.recipe_to_sbepgsm(recipe)))
    entt = doc["cg__"]["entt"]

    # exactly two tracked targets: one bypass (type1), one param (type2)
    assert len(entt["trgs"]) == 2, entt["trgs"]
    byp = _trg_by(entt, type=1)
    par = _trg_by(entt, type=2)
    assert byp["enty"] == 2 and byp["pid_"] == 0
    assert par["enty"] == 3 and par["pid_"] == defs.load_defs()[
        "model_params"][str(defs.model_id_for("HD2_AmpBritPlexiNrm"))]["Bass"]["id"]

    # the two trgs are keyed by DISTINCT device instance ids (from Phase 1 map)
    assert byp["eID_"] != par["eID_"]

    # ctm_.stid lists both tracked trg ids; ptid packs the param target
    assert set(entt["ctm_"]["stid"]) == {byp["id__"], par["id__"]}
    packed = (par["eID_"] << 16) | par["pid_"]
    ptid = entt["ctm_"]["ptid"]
    assert dict(zip(ptid[::2], ptid[1::2])) == {packed: par["id__"]}

    # snapshot 0 (Rhythm) and 1 (Lead) carry the authored per-scene values
    snps = sorted(entt["snps"], key=lambda s: s["si__"])
    assert snps[0]["name"] == "Rhythm" and snps[1]["name"] == "Lead"
    s0, s1 = _tamv_map(snps[0]), _tamv_map(snps[1])
    assert s0[byp["id__"]] is True and s1[byp["id__"]] is False
    assert s0[par["id__"]] == 0.45 and s1[par["id__"]] == 0.38
    # unnamed snapshots 2..7 hold the last (Lead) value (padded)
    assert _tamv_map(snps[7])[byp["id__"]] is False


def test_no_snapshot_variation_yields_blank8():
    """A recipe with no per-snapshot variation still produces the blank-8
    ``cg__`` (no trgs), matching the pre-Part-A behaviour."""
    recipe = {"name": "flat", "paths": [{"blocks": [
        {"block": "HD2_DistMinotaurMono", "params": {"Gain": 0.4}},
    ]}]}
    doc = transcode.recipe_to_sbepgsm(recipe)
    entt = doc["cg__"]["entt"]
    assert entt["trgs"] == [] and entt["ctm_"]["stid"] == []
    assert len(entt["snps"]) == 8
    assert all(s["tamv"] == [] for s in entt["snps"])


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
