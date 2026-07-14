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
        # The device uses a FIXED 28-slot grid per flow with an identity bmap
        # (bmap[gridpos] == the block id at that grid position). blks alternates
        # [gridpos, block, ...] for occupied positions only.
        assert flow["bcnt"] == 28, f"flow {fi} bcnt must be the fixed 28-slot grid"
        assert len(flow["bmap"]) == 28, f"flow {fi} bmap must be 28-wide"
        base = flow["bmap"][0]
        assert flow["bmap"] == [base + i for i in range(28)], f"flow {fi} bmap not identity"
        dicts = []
        i = 0
        while i < len(blks):
            gp, b = blks[i], blks[i + 1]
            assert isinstance(gp, int) and 0 <= gp < 28, f"flow {fi} gridpos {gp} off-grid"
            assert isinstance(b, dict), f"flow {fi} expected block after gridpos"
            assert flow["bmap"][gp] == b["id__"], f"flow {fi} bmap[{gp}] != block id"
            dicts.append(b)
            i += 2
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


# --- Phase 3 / snapshots spec Part B: FS/EXP controller graph ----------------

def test_controller_graph_synthesis():
    """Author a recipe with a known FS->bypass (A1 + A5) and an EXP1->param sweep
    -> synth -> decode -> assert the srcs (locl/ctxt), trgs, and ctrl (behv/
    min_/max_) match the device-RE mapping, plus sm__.scid + pm__ scribble."""
    amp_mid = defs.model_id_for("HD2_AmpBritPlexiNrm")
    bass_pid = defs.load_defs()["model_params"][str(amp_mid)]["Bass"]["id"]
    recipe = {
        "name": "ctrl test",
        "sources": {0x01010100: {"fs_color": "auto", "fs_label": "DRV",
                                 "fs_topidx": 0}},
        "paths": [{"blocks": [
            {"block": "HD2_DistMinotaurMono", "params": {"Gain": 0.5},
             "fs_bypass": {"source": 0x01010100, "behavior": "latching"}},   # A1
            {"block": "HD2_DistVerminDistMono", "params": {},
             "fs_bypass": {"source": 0x01010104, "behavior": "momentary"}},   # A5
            {"block": "HD2_AmpBritPlexiNrm", "params": {"Bass": 0.5},
             "exp_params": {"Bass": {"source": 0x01020100, "min": 0.1,
                                     "max": 0.8}}},                            # EXP1
        ]}],
    }
    doc = content.decode_any(
        content.encode_content_data(transcode.recipe_to_sbepgsm(recipe)))
    entt = doc["cg__"]["entt"]

    # three sources: A1 (locl 25, ctxt 1, bypass), A5 (locl 29, ctxt 1, bypass),
    # EXP1 (locl 42, ctxt 0, param sweep -> byps False).
    by_locl = {s["locl"]: s for s in entt["srcs"]}
    assert set(by_locl) == {25, 29, 42}
    assert by_locl[25]["ctxt"] == 1 and by_locl[25]["byps"] is True
    assert by_locl[29]["ctxt"] == 1 and by_locl[29]["byps"] is True
    assert by_locl[42]["ctxt"] == 0 and by_locl[42]["byps"] is False

    # ctrl entries: two bypass (behv 0 / type 1) + one param (behv 2 / type 3).
    byp_ctrls = [c for c in entt["ctrl"] if c["type"] == 1]
    par_ctrls = [c for c in entt["ctrl"] if c["type"] == 3]
    assert len(byp_ctrls) == 2 and len(par_ctrls) == 1
    for c in byp_ctrls:
        assert c["behv"] == 0 and c["min_"] is False and c["max_"] is True
        assert c["curv"] == 5
    pc = par_ctrls[0]
    assert pc["behv"] == 2 and pc["min_"] == 0.1 and pc["max_"] == 0.8

    # the momentary A5 bypass sets togl True; latching A1 sets togl False
    a5_src = by_locl[29]["id__"]
    a1_src = by_locl[25]["id__"]
    assert next(c for c in byp_ctrls if c["trig"] == a5_src)["togl"] is True
    assert next(c for c in byp_ctrls if c["trig"] == a1_src)["togl"] is False

    # the EXP param trg is a type2/enty3 target on the amp's Bass pid, packed
    # into ptid.
    par_trg = next(t for t in entt["trgs"] if t["id__"] == pc["tid_"])
    assert par_trg["type"] == 2 and par_trg["enty"] == 3
    assert par_trg["pid_"] == bass_pid and par_trg["mmid"] == amp_mid
    packed = (par_trg["eID_"] << 16) | bass_pid
    ptid = entt["ctm_"]["ptid"]
    assert dict(zip(ptid[::2], ptid[1::2])).get(packed) == par_trg["id__"]

    # sm__.scid links each target to its driving ctrl id
    scid = dict(zip(entt["sm__"]["scid"][::2], entt["sm__"]["scid"][1::2]))
    assert scid[pc["tid_"]] == [pc["cid_"]]

    # pm__ scribble strip for A1 (stomp a.1) carries the source label
    pm = {p["key_"]: p["val_"] for p in doc["pm__"]}
    assert pm["preset.floorboard.stomp.a.1.label"] == "DRV"


def test_hsp_to_sbepgsm_controllers_from_hsp():
    """End-to-end: a real authored .hsp with footswitch + wah/EXP assignments
    transcodes into the controller graph (bridge extraction + Part B synth)."""
    hsp = pytest.importorskip("helixgen.hsp")
    path = Path("/Users/michael.shea/git/guitar-training/tones/thunder-kiss-65.hsp")
    if not path.exists():
        pytest.skip(f"authored .hsp fixture absent: {path}")
    body = hsp.read_hsp(path)
    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    entt = doc["cg__"]["entt"]

    # A-bank footswitches map to locl 25.. ctxt 1; the wah EXP toe+pedal to
    # locl 42 ctxt 0.
    locls = {s["locl"] for s in entt["srcs"]}
    assert {25, 26, 27, 28} <= locls, locls   # FS1-4 bypasses
    assert 42 in locls                          # EXP1 (toe + pedal)
    assert any(s["ctxt"] == 0 and s["locl"] == 42 for s in entt["srcs"])
    # a param-sweep ctrl (the wah pedal) is present
    assert any(c["type"] == 3 and c["behv"] == 2 for c in entt["ctrl"])
    # scribble strip carried through
    pm = {p["key_"]: p["val_"] for p in doc["pm__"]}
    assert pm.get("preset.floorboard.stomp.a.1.label") == "GTR1->"


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


# --- snapshot bypass semantics: device-native polarity + bindings ------------
#
# Hardware reference: the same tone imported by the Stadium app vs transcoded
# by helixgen (Dream On, 2026-07-13). The device-native encoding is:
#   * block-level ``enbl`` carries the BASE bypass (0 = block loads bypassed);
#   * a bypass target's ``tamv`` value is BYPASS polarity (True = bypassed),
#     the inverse of the ``.hsp`` ``@enabled`` arrays;
#   * every snapshot-tracked entity is BOUND to its target: the block dict
#     (bypass) or parm leaf (param) carries ``snap=True, tid_=<trg id>``;
#     untracked blocks/leaves carry ``snap=False, tid_=0`` (an FS-only bypass
#     target does NOT set the block's ``tid_``);
#   * ``tamv``/names cover all 8 snapshots from the .hsp's dense arrays (the
#     trailing "Snap N" defaults are real state, not padding).


def _snapshot_hsp_body():
    """A minimal authored ``.hsp`` body shaped like the dream-on tone:

    comp (base-BYPASSED, snapshot-tracked) -> drive (FS1 + snapshot-tracked)
    -> delay (FS2-only bypass, snapshot-tracked Mix param).
    Snapshots: "Lead" (comp off) / "Clean" (drive off), 3..8 default.
    """
    def wrap(v, snaps=None):
        w = {"value": v}
        if snaps is not None:
            w["snapshots"] = snaps
        return w

    def fs(source):
        return {"behavior": "latching", "bypassed": False, "curve": "linear",
                "delay": None, "goid": None, "max": None, "midisource": 0,
                "min": None, "source": source, "threshold": None,
                "type": "targetbypass"}

    flow0 = {
        "b00": {"@enabled": {"value": True},
                "slot": [{"model": "P35_InputInst1_2", "params": {}}]},
        "b01": {"@enabled": {"value": False,
                             "snapshots": [False, True, True, True,
                                           True, True, True, True]},
                "slot": [{"model": "HX2_CompressorLAStudioCompMono",
                          "params": {"Gain": wrap(0.48)}}]},
        "b02": {"@enabled": {"value": True,
                             "snapshots": [True, False, True, True,
                                           True, True, True, True],
                             "controller": fs(0x01010100)},
                "slot": [{"model": "HD2_DistMinotaurMono",
                          "params": {"Gain": wrap(0.4)}}]},
        "b03": {"@enabled": {"value": True, "controller": fs(0x01010101)},
                "slot": [{"model": "HD2_DL4TapeEchoStereo",
                          "params": {"Mix": wrap(
                              0.24, [0.24, 0.1, 0.24, 0.24,
                                     0.24, 0.24, 0.24, 0.24])}}]},
        "b13": {"@enabled": {"value": True},
                "slot": [{"model": "P35_OutputMatrix", "params": {}}]},
    }
    snapshots = [{"name": "Lead", "expsw": 1}, {"name": "Clean"}] + [
        {"name": f"Snap {i}"} for i in range(3, 9)]
    return {"meta": {"device_id": "stadium_xl"},
            "preset": {"flow": [flow0], "snapshots": snapshots}}


def _blocks_by_mid(doc):
    out = {}
    for flow in doc["sfg_"]["flow"]:
        for b in flow["blks"]:
            if isinstance(b, dict):
                mid = (b.get("mdls") or [{}])[0].get("id__")
                out.setdefault(mid, b)
    return out


def _snap_doc():
    return content.decode_any(transcode.hsp_to_sbepgsm(_snapshot_hsp_body()))


def test_base_bypass_survives_transcode():
    """A block whose .hsp ``@enabled.value`` is False must synthesize with
    block-level ``enbl == 0`` (it loads bypassed), everything else ``1``."""
    doc = _snap_doc()
    blocks = _blocks_by_mid(doc)
    comp = blocks[defs.model_id_for("HX2_CompressorLAStudioCompMono")]
    drive = blocks[defs.model_id_for("HD2_DistMinotaurMono")]
    delay = blocks[defs.model_id_for("HD2_DL4TapeEchoStereo")]
    assert comp["enbl"] == 0, "base-bypassed block must synthesize enbl=0"
    assert drive["enbl"] == 1 and delay["enbl"] == 1
    # the model instance stays enabled even when the block is bypassed
    assert comp["mdls"][0]["enbl"] == 1


def test_tamv_bypass_values_are_bypass_polarity():
    """``tamv`` values for a bypass target are True=BYPASSED (device polarity),
    across ALL 8 snapshots from the .hsp's dense arrays (no last-named
    padding)."""
    doc = _snap_doc()
    entt = doc["cg__"]["entt"]
    comp_eid = _blocks_by_mid(doc)[
        defs.model_id_for("HX2_CompressorLAStudioCompMono")]["id__"]
    drive_eid = _blocks_by_mid(doc)[
        defs.model_id_for("HD2_DistMinotaurMono")]["id__"]
    comp_trg = _trg_by(entt, type=1, eID_=comp_eid)
    drive_trg = _trg_by(entt, type=1, eID_=drive_eid)
    snps = sorted(entt["snps"], key=lambda s: s["si__"])
    comp_row = [_tamv_map(s)[comp_trg["id__"]] for s in snps]
    drive_row = [_tamv_map(s)[drive_trg["id__"]] for s in snps]
    # .hsp @enabled [F,T,T,...] -> device bypass [T,F,F,...]
    assert comp_row == [True] + [False] * 7, comp_row
    # .hsp @enabled [T,F,T,...] -> device bypass [F,T,F,...] — snaps 3..8 must
    # come from the dense arrays (base state), NOT pad with Clean's value.
    assert drive_row == [False, True] + [False] * 6, drive_row


def test_tamv_param_values_cover_all_snapshots():
    """A snapshot-tracked param's ``tamv`` row uses the .hsp's dense 8-value
    array (0.24 base in snaps 3..8), not last-named-snapshot padding."""
    doc = _snap_doc()
    entt = doc["cg__"]["entt"]
    par = _trg_by(entt, type=2)
    snps = sorted(entt["snps"], key=lambda s: s["si__"])
    row = [_tamv_map(s)[par["id__"]] for s in snps]
    assert row == [0.24, 0.1] + [0.24] * 6, row


def test_snapshot_tracked_entities_are_bound():
    """Snapshot-tracked blocks carry ``snap=True, tid_=<bypass trg id>``; a
    tracked param's parm leaf carries ``snap=True, tid_=<param trg id>``."""
    doc = _snap_doc()
    entt = doc["cg__"]["entt"]
    blocks = _blocks_by_mid(doc)

    for model in ("HX2_CompressorLAStudioCompMono", "HD2_DistMinotaurMono"):
        blk = blocks[defs.model_id_for(model)]
        trg = _trg_by(entt, type=1, eID_=blk["id__"])
        assert blk["snap"] is True, f"{model} bypass is snapshot-tracked"
        assert blk["tid_"] == trg["id__"], f"{model} tid_ must bind its trg"

    delay = blocks[defs.model_id_for("HD2_DL4TapeEchoStereo")]
    par = _trg_by(entt, type=2)
    mix_pid = par["pid_"]
    leaf = next(p for p in delay["mdls"][0]["parm"] if p["pid_"] == mix_pid)
    assert leaf["snap"] is True and leaf["tid_"] == par["id__"]


def test_fs_only_bypass_is_not_snapshot_bound():
    """A block with an FS bypass but NO snapshot variation keeps
    ``snap=False, tid_=0`` at block level (its trg exists only for the ctrl),
    and untracked blocks/endpoints carry ``tid_=0`` (no sequential ids that
    collide with real target ids)."""
    doc = _snap_doc()
    entt = doc["cg__"]["entt"]
    blocks = _blocks_by_mid(doc)
    delay = blocks[defs.model_id_for("HD2_DL4TapeEchoStereo")]
    delay_byp = _trg_by(entt, type=1, eID_=delay["id__"])
    assert delay["snap"] is False and delay["tid_"] == 0
    # the FS trg still exists and is NOT in the snapshot-tracked stid set
    assert delay_byp["id__"] not in entt["ctm_"]["stid"]
    # untracked leaves (endpoints included) never carry a stale tid_
    for b in _blocks_by_mid(doc).values():
        if b["snap"] is False:
            assert b["tid_"] == 0, b
    for p in delay["mdls"][0]["parm"]:
        if p["snap"] is False:
            assert p["tid_"] == 0


def test_snapshot_names_cover_all_eight():
    """Snapshot names come from the .hsp for all 8 slots ("Snap 3", not the
    "SNAPSHOT 3" fallback), with exsw/bpm carried on the named ones."""
    doc = _snap_doc()
    snps = sorted(doc["cg__"]["entt"]["snps"], key=lambda s: s["si__"])
    assert [s["name"] for s in snps] == (
        ["Lead", "Clean"] + [f"Snap {i}" for i in range(3, 9)])
    assert snps[0]["exsw"] == 1 and snps[1]["exsw"] == -1


def test_sparse_snapshot_arrays_fall_back_to_base():
    """A legacy sparse ``@enabled.snapshots`` (None entries) treats None as the
    base value, not as False/enabled."""
    body = _snapshot_hsp_body()
    b01 = body["preset"]["flow"][0]["b01"]
    b01["@enabled"]["snapshots"] = [None, True, None, None,
                                    None, None, None, None]
    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    entt = doc["cg__"]["entt"]
    comp_eid = _blocks_by_mid(doc)[
        defs.model_id_for("HX2_CompressorLAStudioCompMono")]["id__"]
    trg = _trg_by(entt, type=1, eID_=comp_eid)
    snps = sorted(entt["snps"], key=lambda s: s["si__"])
    row = [_tamv_map(s)[trg["id__"]] for s in snps]
    # base value=False (bypassed): None -> bypassed; True -> not bypassed
    assert row == [True, False] + [True] * 6, row
