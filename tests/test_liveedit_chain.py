"""liveedit.chain — the live edit buffer parsed into the signal-chain structure
the `device live` page renders (hgc-58a).

The edit buffers here are SYNTHETIC, shaped like a real decoded Stadium edit
buffer (``sfg_.flow[dsp].blks`` = flat ``[grid_slot, block, …]``; each block's
``mdls[0]`` carries ``id__`` + ``parm``; splits/joins carry ``bblk``), using
real model ids from the vendored defs.
"""
from helixgen.device import defs
from helixgen.liveedit import chain

# real model ids (vendored defs)
IN_INST1, IN_NONE, IN_MIC = 770, 771, 766
OUT_MATRIX, OUT_NONE, OUT_USB34 = 783, 789, 785
SPLIT_Y, JOIN = 475, 478
SCREAM, AMP, IR = 310, 758, 468


def _blk(mid, parm=None, *, enbl=1, bblk=None, bflw=None, snap_pids=()):
    b = {"enbl": enbl, "mdls": [{
        "id__": mid, "enbl": 1,
        "parm": [{"pid_": pid, "valu": v, "snap": pid in snap_pids}
                 for pid, v in (parm or {}).items()]}]}
    if bblk is not None:
        # ``bblk`` is a block INSTANCE id, resolved to a grid slot through the
        # flow's ``bmap`` (identity in these fixtures unless a test says so).
        b.update(bblk=bblk, hasb=True, bflw=0 if bflw is None else bflw)
    return b


def _eb(*paths, asnp=0, hist=0, bmap=None):
    flow = []
    for slots in paths:
        blks = []
        for slot in sorted(slots):
            blks += [slot, slots[slot]]
        flow.append({"blks": blks,
                     "bmap": list(range(28)) if bmap is None else bmap})
    return {"sfg_": {"flow": flow}, "cg__": {"asnp": asnp}, "hist": hist}


def _serial():
    return _eb({
        0: _blk(IN_INST1, {2: 1, 3: 0.0, 4: True, 5: -52.0}),
        1: _blk(SCREAM, {1: 0.3, 2: 0.8, 3: 0.84}, enbl=0),
        2: _blk(AMP, {defs.model_params_for(AMP)["Drive"]["id"]: 0.74},
                snap_pids={defs.model_params_for(AMP)["Drive"]["id"]}),
        13: _blk(OUT_MATRIX, {1: 0.5, 2: -3.0}),
        14: _blk(IN_NONE), 27: _blk(OUT_NONE),
    }, asnp=2, hist=1)


def _kinds(segments):
    return [s["kind"] for s in segments]


def _slots(segments):
    return [s["block"]["block"] for s in segments if s["kind"] == "block"]


# --- serial chain ----------------------------------------------------------

def test_serial_chain_is_blocks_in_grid_order_without_none_endpoints():
    st = chain.build_chain(_serial())
    assert len(st["paths"]) == 1
    p = st["paths"][0]
    assert p["path"] == 0 and p["extra"] == []
    assert _kinds(p["segments"]) == ["block"] * 4
    assert _slots(p["segments"]) == [0, 1, 2, 13]      # 14/27 None endpoints hidden


def test_snapshot_comes_from_the_buffer_but_dirty_does_not():
    st = chain.build_chain(_serial())
    assert st["snapshot"] == 2
    # `hist` is NOT the unsaved flag (HW 2026-11-13: still 1 right after a
    # save) — that is the `volatile.preset.edited` property, read by the
    # session. The chain says nothing about it.
    assert "dirty" not in st


def test_block_fields():
    st = chain.build_chain(_serial())
    blocks = {b["block"]: b for b in chain.iter_blocks(st)}
    scream = blocks[1]
    assert scream["path"] == 0 and scream["row"] == 0
    assert scream["model_id"] == SCREAM
    assert scream["model"] == "HD2_DistScream808Mono"
    assert scream["name"] == "Scream 808 Mono"          # the editor's display name
    assert scream["category"] == "distortion"
    assert scream["enabled"] is False
    assert blocks[0]["enabled"] is True
    assert blocks[0]["category"] == "input"
    # structural blocks have no editor display name -> readable fallback
    assert blocks[0]["name"] == "Input Inst 1"
    assert blocks[13]["name"] == "Output Matrix"


def test_params_are_in_pid_order_with_stored_values_and_defaults():
    st = chain.build_chain(_serial())
    scream = chain.block_index(st)[(0, 1)]
    assert [p["pid"] for p in scream["params"]] == sorted(p["pid"] for p in scream["params"])
    by = {p["name"]: p for p in scream["params"]}
    assert by["Gain"]["value"] == 0.3 and by["Gain"]["ctl"] == "knob"
    assert by["Gain"]["min"] == 0 and by["Gain"]["max"] == 1
    # a param the buffer stores no entry for shows the DEVICE default
    amp = chain.block_index(st)[(0, 2)]
    aby = {p["name"]: p for p in amp["params"]}
    assert aby["Drive"]["value"] == 0.74
    assert aby["Bass"]["value"] == defs.model_params_for(AMP)["Bass"]["def"]


def test_param_display_metadata_comes_from_paraminfo():
    st = chain.build_chain(_serial())
    amp = {p["name"]: p for p in chain.block_index(st)[(0, 2)]["params"]}
    assert amp["Drive"]["scale"] == 10                   # stored 0.74 shows 7.4
    assert amp["AmpCabPeakFc"]["internal"] is True
    assert amp["Drive"]["internal"] is False
    inp = {p["name"]: p for p in chain.block_index(st)[(0, 0)]["params"]}
    assert inp["threshold"]["label"] == "Threshold"
    assert inp["threshold"]["unit"] == "dB"
    out = {p["name"]: p for p in chain.block_index(st)[(0, 13)]["params"]}
    assert out["pan"]["display_min"] == -100 and out["pan"]["display_max"] == 100
    assert out["gain"]["label"] == "Level"


def test_control_types():
    st = chain.build_chain(_serial())
    inp = {p["name"]: p for p in chain.block_index(st)[(0, 0)]["params"]}
    # bool -> toggle, value encoded 0/1
    assert inp["noiseGate"]["ctl"] == "toggle"
    assert inp["noiseGate"]["value"] == 1.0
    assert (inp["noiseGate"]["min"], inp["noiseGate"]["max"]) == (0, 1)
    # int with enum labels -> select; the label index is value - min
    assert inp["Pad"]["ctl"] == "select"
    assert inp["Pad"]["labels"] == ["Off/Inst", "On/Line"]
    assert inp["Pad"]["min"] == 1 and inp["Pad"]["value"] == 1.0
    # float -> knob
    assert inp["Trim"]["ctl"] == "knob"


def test_snapshot_controlled_flag_is_carried():
    st = chain.build_chain(_serial())
    amp = {p["name"]: p for p in chain.block_index(st)[(0, 2)]["params"]}
    assert amp["Drive"]["snap"] is True
    assert amp["Bass"]["snap"] is False


def test_unknown_pid_is_dropped_and_unknown_model_still_renders():
    eb = _eb({0: _blk(IN_INST1, {999: 1.0}), 1: _blk(999999, {1: 0.5}),
              13: _blk(OUT_MATRIX)})
    st = chain.build_chain(eb)
    assert all(p["pid"] != 999 for p in chain.block_index(st)[(0, 0)]["params"])
    odd = chain.block_index(st)[(0, 1)]
    assert odd["params"] == [] and odd["name"] == "Model 999999"


# --- parallel lanes --------------------------------------------------------

def _split_join():
    return _eb({
        0: _blk(IN_INST1), 3: _blk(AMP),
        4: _blk(SPLIT_Y, {1: 0.5, 2: 0.5}, bblk=15),
        5: _blk(IR, {11: 80.0}),
        6: _blk(JOIN, {1: 0.0, 3: -2.5}, bblk=20),
        7: _blk(SCREAM),
        13: _blk(OUT_MATRIX),
        14: _blk(IN_NONE), 15: _blk(IR, {11: 120.0}, bblk=4), 27: _blk(OUT_NONE),
    })


def test_split_join_becomes_a_parallel_segment():
    p = chain.build_chain(_split_join())["paths"][0]
    assert _kinds(p["segments"]) == ["block", "block", "parallel", "block", "block"]
    par = p["segments"][2]
    assert par["split"]["block"] == 4 and par["join"]["block"] == 6
    assert _slots(par["a"]) == [5]
    assert _slots(par["b"]) == [15]
    assert par["b"][0]["block"]["row"] == 1
    assert p["extra"] == []


def test_unjoined_split_runs_lane_b_to_the_row1_output():
    # how a USB recording tap looks: split right before the output, lane B is
    # just row 1's own (real) output.
    eb = _eb({
        0: _blk(IN_INST1), 3: _blk(AMP),
        11: _blk(SPLIT_Y, bblk=25), 13: _blk(OUT_MATRIX),
        14: _blk(IN_NONE), 27: _blk(OUT_USB34),
    })
    p = chain.build_chain(eb)["paths"][0]
    assert _kinds(p["segments"]) == ["block", "block", "parallel"]
    par = p["segments"][2]
    assert par["join"] is None
    assert _slots(par["a"]) == [13]
    assert _slots(par["b"]) == [27]


def test_two_pairs_then_tap_in_one_path():
    eb = _eb({
        0: _blk(IN_INST1),
        2: _blk(SPLIT_Y, bblk=16), 3: _blk(SCREAM), 4: _blk(JOIN, bblk=18),
        6: _blk(SPLIT_Y, bblk=20), 7: _blk(AMP), 8: _blk(JOIN, bblk=22),
        11: _blk(SPLIT_Y, bblk=25), 13: _blk(OUT_MATRIX),
        14: _blk(IN_NONE), 16: _blk(IR), 21: _blk(IR), 27: _blk(OUT_USB34),
    })
    p = chain.build_chain(eb)["paths"][0]
    assert _kinds(p["segments"]) == ["block", "parallel", "parallel", "parallel"]
    one, two, tap = p["segments"][1:]
    assert _slots(one["b"]) == [16] and _slots(two["b"]) == [21]
    assert _slots(tap["a"]) == [13] and _slots(tap["b"]) == [27]


def test_bblk_is_an_instance_id_resolved_through_bmap():
    # the split names instance 99; bmap says instance 99 sits at grid slot 17.
    # Row 1 ALSO holds a block at 15 that this split does not feed: only the
    # bmap resolution can tell them apart (the no-pointer fallback would start
    # the lane at 15 and claim both).
    bmap = list(range(28))
    bmap[17] = 99
    eb = _eb({
        0: _blk(IN_INST1),
        4: _blk(SPLIT_Y, bblk=99), 5: _blk(AMP), 6: _blk(JOIN, bblk=20),
        13: _blk(OUT_MATRIX),
        14: _blk(IN_NONE), 15: _blk(SCREAM), 17: _blk(IR), 27: _blk(OUT_NONE),
    }, bmap=bmap)
    p = chain.build_chain(eb)["paths"][0]
    par = p["segments"][1]
    assert par["kind"] == "parallel" and _slots(par["b"]) == [17]
    assert _slots(p["extra"]) == [15]


def test_split_without_a_usable_pointer_still_claims_its_lane():
    eb = _split_join()
    blks = eb["sfg_"]["flow"][0]["blks"]
    split = blks[blks.index(4) + 1]
    del split["bblk"], split["hasb"], split["bflw"]   # the split loses its pointer
    blks[blks.index(15)] = 19                         # branch block elsewhere in range
    p = chain.build_chain(eb)["paths"][0]
    par = p["segments"][2]
    assert _slots(par["b"]) == [19]


def test_cross_dsp_pointer_is_a_link_tag_not_a_lane():
    # a block whose branch pointer names the OTHER flow (USB tap / mutual tap):
    # it must not open a lane in its own path; the page shows a link tag.
    eb = _eb(
        {0: _blk(IN_INST1), 3: _blk(SPLIT_Y, bblk=0, bflw=1), 4: _blk(AMP),
         13: _blk(OUT_MATRIX)},
        {0: _blk(IN_MIC, bblk=11, bflw=0), 13: _blk(OUT_MATRIX)})
    st = chain.build_chain(eb)
    p0, p1 = st["paths"]
    assert _kinds(p0["segments"]) == ["block"] * 4
    assert chain.block_index(st)[(0, 3)]["xlink"] == {"path": 1}
    assert chain.block_index(st)[(1, 0)]["xlink"] == {"path": 0}
    assert chain.block_index(st)[(0, 4)]["xlink"] is None


def test_independent_row1_chain_lands_in_extra():
    # row 1 with its OWN real input and no split feeding it
    eb = _eb({
        0: _blk(IN_INST1), 1: _blk(AMP), 13: _blk(OUT_MATRIX),
        14: _blk(IN_MIC), 16: _blk(SCREAM), 27: _blk(OUT_USB34),
    })
    p = chain.build_chain(eb)["paths"][0]
    assert _slots(p["segments"]) == [0, 1, 13]
    assert _slots(p["extra"]) == [14, 16, 27]


def test_second_dsp_path_is_its_own_section():
    eb = _eb({0: _blk(IN_INST1), 13: _blk(OUT_MATRIX)},
             {0: _blk(IN_MIC), 13: _blk(OUT_MATRIX), 14: _blk(IN_NONE), 27: _blk(OUT_NONE)})
    st = chain.build_chain(eb)
    assert [p["path"] for p in st["paths"]] == [0, 1]
    assert _slots(st["paths"][1]["segments"]) == [0, 13]


def test_a_path_with_only_none_endpoints_is_omitted():
    eb = _eb({0: _blk(IN_INST1), 13: _blk(OUT_MATRIX)},
             {0: _blk(IN_NONE), 13: _blk(OUT_NONE)})
    assert [p["path"] for p in chain.build_chain(eb)["paths"]] == [0]


def test_every_real_block_appears_exactly_once():
    for eb in (_serial(), _split_join()):
        st = chain.build_chain(eb)
        seen = [(b["path"], b["block"]) for b in chain.iter_blocks(st)]
        assert len(seen) == len(set(seen))
        want = set()
        for pi, dsp in enumerate(eb["sfg_"]["flow"]):
            blks = dsp["blks"]
            for i in range(0, len(blks), 2):
                mid = blks[i + 1]["mdls"][0]["id__"]
                if mid not in (IN_NONE, OUT_NONE):
                    want.add((pi, blks[i]))
        assert set(seen) == want


def test_garbage_inside_the_buffer_does_not_raise_or_explode():
    eb = _serial()
    eb["cg__"]["entt"] = {"snps": 5}                       # a scalar where a list belongs
    assert chain.build_chain(eb)["snapshots"] == []
    eb["cg__"]["entt"] = {"snps": [7, {"si__": 10 ** 9, "name": "x"}, {"si__": 1, "name": "ok"}]}
    assert chain.build_chain(eb)["snapshots"] == ["Snapshot 1", "ok"]   # a wild index sizes nothing
    blks = eb["sfg_"]["flow"][0]["blks"]
    blks[1]["mdls"][0]["parm"] = 9                         # scalar parm
    blks[3]["mdls"][0]["parm"] = [{"pid_": 1, "valu": float("nan")}, "junk"]
    st = chain.build_chain(eb)
    scream = chain.block_index(st)[(0, 1)]
    assert all(p["pid"] != 1 for p in scream["params"])    # a NaN is not a knob position
    import json
    json.dumps(st, allow_nan=False)                        # the state is always valid JSON


def test_malformed_buffers_do_not_raise():
    assert chain.build_chain({})["paths"] == []
    assert chain.build_chain({"sfg_": {"flow": [None, {"blks": [0, {"mdls": []}]}]}})["paths"] == []


# --- indexes + clamping ----------------------------------------------------

def test_param_index_addresses_by_path_slot_pid():
    st = chain.build_chain(_serial())
    idx = chain.param_index(st)
    assert idx[(0, 1, 1)]["name"] == "Gain"
    assert (0, 14, 1) not in idx                      # hidden endpoint


def test_clamp_respects_range_and_type():
    st = chain.build_chain(_serial())
    idx = chain.param_index(st)
    gain = idx[(0, 1, 1)]
    assert chain.clamp(gain, 5.0) == 1.0
    assert chain.clamp(gain, -1.0) == 0.0
    assert chain.clamp(gain, 0.42) == 0.42
    pad = next(p for p in chain.block_index(st)[(0, 0)]["params"] if p["name"] == "Pad")
    assert chain.clamp(pad, 1.6) == 2.0               # ints snap
    assert chain.clamp(pad, 9) == 2.0
    gate = next(p for p in chain.block_index(st)[(0, 0)]["params"] if p["name"] == "noiseGate")
    assert chain.clamp(gate, 0.7) == 1.0 and chain.clamp(gate, 0.2) == 0.0
    out_gain = idx[(0, 13, 2)]
    assert chain.clamp(out_gain, 99.0) == 20.0        # the +20 dB ceiling holds


def test_snapshot_names_are_ordered_by_index():
    eb = _serial()
    # the device lists them in arbitrary order, keyed by si__
    eb["cg__"]["entt"] = {"snps": [
        {"si__": 2, "name": "Lead"}, {"si__": 0, "name": "Rhythm"},
        {"si__": 1, "name": "Chorus"}]}
    st = chain.build_chain(eb)
    assert st["snapshots"] == ["Rhythm", "Chorus", "Lead"]
    assert chain.build_chain(_split_join())["snapshots"] == []
