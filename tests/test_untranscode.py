"""Fidelity gate for the REVERSE transcoder (``.sbe`` -> ``.hsp``).

The forward transcoder is hardware-validated byte-for-byte against HX Edit's
own import, which makes it the spec to invert — and makes
``.sbe -> .hsp -> .sbe`` a free oracle. Every round-trip test here builds its
own device content from a recipe (``transcode.recipe_to_sbepgsm``), so the
whole module runs on a clean clone with no fixtures and no block library.

``test_corpus_*`` additionally sweeps real device blobs when
``tests/fixtures/device_content/*.sbe`` is populated (gitignored, like every
other real-export fixture in this suite).
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

pytest.importorskip("msgpack")

from helixgen.device import content, defs, transcode, untranscode  # noqa: E402
from helixgen.device.transcode import _controller_locl_ctxt  # noqa: E402

FIXDIR = Path(__file__).parent / "fixtures" / "device_content"

AMP = "HD2_AmpBritPlexiNrm"
DRIVE = "HD2_DistMinotaurMono"
FUZZ = "HD2_DistVerminDistMono"
CAB = "HX2_ImpulseResponseWithPan"
IRHASH = "0123456789abcdef0123456789abcdef"


def _pid(model: str, param: str) -> int:
    return defs.param_id_for(defs.model_id_for(model), param)


def _roundtrip(recipe: dict) -> tuple[dict, dict, dict]:
    """``recipe -> sbe -> hsp -> sbe``. Returns ``(sbe1, hsp, sbe2)``."""
    sbe1 = content.encode_content_data(transcode.recipe_to_sbepgsm(recipe))
    body = untranscode.sbe_bytes_to_hsp(sbe1, name="RT")
    sbe2 = transcode.hsp_to_sbepgsm(body)
    return sbe1, body, sbe2


def _assert_roundtrip(recipe: dict) -> dict:
    """Assert ``.sbe -> .hsp -> .sbe`` is byte-exact; return the ``.hsp`` body."""
    sbe1, body, sbe2 = _roundtrip(recipe)
    if sbe1 != sbe2:  # decode both so the failure names the diverging leaf
        a, b = content.decode_any(sbe2), content.decode_any(sbe1)
        assert a == b, "round trip diverged"
        pytest.fail("round trip is content-identical but not byte-identical")
    return body


def _flow0(body: dict) -> dict:
    return body["preset"]["flow"][0]


# --- the oracle: .sbe -> .hsp -> .sbe ----------------------------------------

def test_serial_chain_roundtrips():
    _assert_roundtrip({"name": "serial", "paths": [{"blocks": [
        {"block": DRIVE, "params": {"Gain": 0.4}},
        {"block": AMP, "params": {"Bass": 0.45, "Drive": 0.7}},
    ]}]})


def test_dual_dsp_roundtrips():
    """Two populated DSP flows (dual-amp) survive the round trip."""
    body = _assert_roundtrip({"name": "dual", "paths": [
        {"input": "inst1", "blocks": [{"block": AMP, "params": {"Bass": 0.4}}]},
        {"input": "inst2", "blocks": [{"block": AMP, "params": {"Bass": 0.6}}]},
    ]})
    assert len(body["preset"]["flow"]) == 2
    assert _flow0(body)["b00"]["slot"][0]["model"] == "P35_InputInst1"
    assert body["preset"]["flow"][1]["b00"]["slot"][0]["model"] == "P35_InputInst2"


def test_parallel_split_roundtrips():
    """A split/join flow with a lane-1 block round-trips, and the ``.hsp``
    carries the endpoint/branch routing pointers ``view`` reads."""
    recipe = {"name": "split", "paths": [{
        "blocks": [
            {"block": AMP, "params": {}, "lane": 0, "pos": 4},
            {"block": CAB, "params": {}, "lane": 0, "pos": 6},
            {"block": CAB, "params": {}, "lane": 1, "pos": 1},
        ],
        "structural": [
            {**copy.deepcopy(transcode._SPLIT_SCAFFOLD), "_pos": 5, "_lane": 0},
            {**copy.deepcopy(transcode._JOIN_SCAFFOLD), "_pos": 7, "_lane": 0},
        ],
    }]}
    body = _assert_roundtrip(recipe)
    flow = _flow0(body)
    assert flow["b05"]["type"] == "split" and flow["b07"]["type"] == "join"
    assert flow["b05"]["endpoint"] == "b07" and flow["b07"]["endpoint"] == "b05"
    assert flow["b05"]["branch"] == "b15" and flow["b07"]["branch"] == "b15"
    assert flow["b15"]["path"] == 1 and flow["b15"]["position"] == 1


def test_ir_reference_roundtrips():
    body = _assert_roundtrip({"name": "ir", "paths": [{"blocks": [
        {"block": CAB, "params": {}, "irhash": IRHASH},
    ]}]})
    assert _flow0(body)["b01"]["slot"][0]["irhash"] == IRHASH


def test_base_bypass_roundtrips():
    body = _assert_roundtrip({"name": "byp", "paths": [{"blocks": [
        {"block": DRIVE, "params": {}, "enabled": False},
        {"block": AMP, "params": {}},
    ]}]})
    assert _flow0(body)["b01"]["@enabled"]["value"] is False
    assert _flow0(body)["b02"]["@enabled"]["value"] is True


def test_snapshot_deltas_roundtrip():
    """Per-snapshot bypass + param arrays come back on the right wrappers, in
    ``.hsp`` polarity (``@enabled``, i.e. the inverse of the device's bypass)."""
    body = _assert_roundtrip({
        "name": "snaps",
        "snapshots": [{"name": "Rhythm"}, {"name": "Lead"}],
        "paths": [{"blocks": [
            {"block": DRIVE, "params": {"Gain": 0.4},
             "snap_bypass": [True, False, False, False, False, False, False, False]},
            {"block": AMP, "params": {"Bass": 0.45},
             "snap_params": {"Bass": [0.45, 0.38, 0.38, 0.38, 0.38, 0.38, 0.38, 0.38]}},
        ]}],
    })
    flow = _flow0(body)
    # device True == bypassed -> .hsp @enabled False
    assert flow["b01"]["@enabled"]["snapshots"][:2] == [False, True]
    assert flow["b02"]["slot"][0]["params"]["Bass"]["snapshots"][:2] == [0.45, 0.38]
    names = [s["name"] for s in body["preset"]["snapshots"]]
    assert names[:2] == ["Rhythm", "Lead"]
    assert len(names) == 8


def test_constant_snapshot_arrays_roundtrip():
    """A snapshot target whose 8 values are all EQUAL is still a target (bead
    hgc-xh3).

    Real device content is full of them — 532 such arrays across 50 of Line 6's
    66 factory presets — because the user assigned the block/param to snapshots
    and then set the same value in every one. The forward path used to require
    VARIATION, so a re-install silently un-assigned them and the second read
    produced a different ``.hsp``."""
    body = _assert_roundtrip({
        "name": "flat",
        "snapshots": [{"name": "A"}, {"name": "B"}],
        "paths": [{"blocks": [
            {"block": DRIVE, "params": {"Gain": 0.4},
             "snap_bypass": [False] * 8},
            {"block": AMP, "params": {"Bass": 0.45},
             "snap_params": {"Bass": [0.45] * 8}},
        ]}],
    })
    flow = _flow0(body)
    assert flow["b01"]["@enabled"]["snapshots"] == [True] * 8
    assert flow["b02"]["slot"][0]["params"]["Bass"]["snapshots"] == [0.45] * 8


def test_controller_assignments_roundtrip():
    """A footswitch bypass and an EXP1 param sweep come back as ``.hsp``
    controller dicts on the right wrappers."""
    body = _assert_roundtrip({
        "name": "ctl",
        "sources": {0x01010100: {"fs_color": "red", "fs_label": "DRV",
                                 "fs_topidx": 0}},
        "paths": [{"blocks": [
            {"block": DRIVE, "params": {"Gain": 0.5},
             "fs_bypass": {"source": 0x01010100, "behavior": "latching"}},
            {"block": AMP, "params": {"Bass": 0.5},
             "ctl_params": {"Bass": {"source": 0x01020100, "min": 0.1,
                                     "max": 0.8}}},
        ]}],
    })
    flow = _flow0(body)
    fsb = flow["b01"]["@enabled"]["controller"]
    assert fsb == {"source": 0x01010100, "type": "targetbypass",
                   "behavior": "latching"}
    exp = flow["b02"]["slot"][0]["params"]["Bass"]["controller"]
    assert exp["type"] == "param" and exp["source"] == 0x01020100
    assert exp["behavior"] == "continuous"
    assert (exp["min"], exp["max"]) == (pytest.approx(0.1), pytest.approx(0.8))
    # the scribble strip survives via preset.sources
    assert body["preset"]["sources"]["16843008"]["fs_label"] == "DRV"
    assert body["preset"]["sources"]["16843008"]["fs_color"] == "red"


def test_momentary_behavior_roundtrips():
    body = _assert_roundtrip({"name": "mom", "paths": [{"blocks": [
        {"block": FUZZ, "params": {},
         "fs_bypass": {"source": 0x01010104, "behavior": "momentary"}},
    ]}]})
    ctl = _flow0(body)["b01"]["@enabled"]["controller"]
    assert ctl["behavior"] == "momentary"


def test_exp_source_bypass_flag_survives():
    """A non-floorboard source (EXP1 / EXP1Toe) has no scribble strip, so its
    ``srcs.byps`` flag has to ride a bare ``preset.sources`` entry — without it
    the forward transcoder's "bypass-driving sources are True" default flips
    the flag back (corpus-observed on three real presets)."""
    body = _assert_roundtrip({"name": "toe", "paths": [{"blocks": [
        {"block": FUZZ, "params": {},
         "fs_bypass": {"source": 0x01010500, "behavior": "latching"}},
    ]}], "sources": {0x01010500: {"bypass": False}}})
    assert body["preset"]["sources"][str(0x01010500)] == {"bypass": False}


def test_active_snapshot_survives():
    """``cg__.asnp`` <-> ``preset.params.activesnapshot``. The forward
    transcoder used to hardcode 0, silently resetting the on-load snapshot on
    every install; the round trip caught it."""
    recipe = {"name": "asnp", "active_snapshot": 3,
              "snapshots": [{"name": f"S{i}"} for i in range(8)],
              "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}
    doc = transcode.recipe_to_sbepgsm(recipe)
    assert doc["cg__"]["asnp"] == 3
    body = untranscode.sbepgsm_to_hsp(doc, name="x")
    assert body["preset"]["params"]["activesnapshot"] == 3
    assert transcode.hsp_to_sbepgsm(body) == content.encode_content_data(doc)


@pytest.mark.parametrize("value,want", [
    (True, 0), (False, 0),          # bool is not an index
    ("2", 0), (None, 0), ([], 0),   # non-numeric -> the old default
    (3.0, 3),                       # an integral float survives JSON round-trips
    (3.5, 0),                       # a fractional one is not an index
    (-1, 0), (8, 7), (99, 7),       # CLAMPED, matching mutate._clamped_active_snapshot
])
def test_active_snapshot_coercion(value, want):
    """Out of range is clamped, not zeroed: `mutate._clamped_active_snapshot`
    clamps to length-1, so zeroing here would give one .hsp two different
    "active snapshots" depending on whether you edited it or installed it."""
    doc = transcode.recipe_to_sbepgsm(
        {"name": "x", "active_snapshot": value,
         "paths": [{"blocks": [{"block": AMP, "params": {}}]}]})
    assert doc["cg__"]["asnp"] == want


# --- structure the ``.hsp`` must have ----------------------------------------

def test_block_keys_follow_the_grid():
    """``bNN`` is the device grid position: row 0 is 0..13, row 1 is 14..27,
    and ``position``/``path`` decode as ``bridge._lane_pos`` does."""
    body = _assert_roundtrip({"name": "grid", "paths": [{"blocks": [
        {"block": DRIVE, "params": {}}, {"block": AMP, "params": {}},
    ]}]})
    flow = _flow0(body)
    assert sorted(k for k in flow if k.startswith("b")) == ["b00", "b01", "b02", "b13"]
    assert flow["b01"]["position"] == 1 and flow["b01"]["path"] == 0
    assert flow["b00"]["type"] == "input" and flow["b13"]["type"] == "output"
    assert flow["b00"]["endpoint"] == "b13" and flow["b13"]["endpoint"] == "b00"


def test_row1_none_endpoints_are_dropped():
    """The forward transcoder synthesizes the row-1 InputNone/OutputNone pair
    for every flow, and a real ``.hsp`` export omits it — so emitting it back
    would be noise the forward path discards anyway."""
    body = _assert_roundtrip({"name": "x", "paths": [{"blocks": [
        {"block": AMP, "params": {}}]}]})
    assert "b14" not in _flow0(body) and "b27" not in _flow0(body)


def test_block_types_use_the_hsp_vocabulary():
    body = _assert_roundtrip({"name": "types", "paths": [{"blocks": [
        {"block": DRIVE, "params": {}},
        {"block": AMP, "params": {}},
        {"block": CAB, "params": {}},
    ]}]})
    flow = _flow0(body)
    assert [flow[f"b0{i}"]["type"] for i in (1, 2, 3)] == ["fx", "amp", "cab"]


def test_params_are_device_names_without_a_library():
    """With no block library the device's own param names are used — which
    normalize onto themselves, so the transcode round trip still holds."""
    body = _assert_roundtrip({"name": "np", "paths": [{"blocks": [
        {"block": AMP, "params": {"Bass": 0.4}}]}]})
    params = _flow0(body)["b02" if "b02" in _flow0(body) else "b01"]["slot"][0]["params"]
    assert "Bass" in params and params["Bass"]["value"] == pytest.approx(0.4)


def test_meta_carries_a_real_stadium_device_id():
    """``meta.device_id`` is load-bearing: ``bridge``/``controllers`` resolve
    the input-model and controller-source tables from it."""
    from helixgen import controllers

    body = untranscode.sbepgsm_to_hsp(
        transcode.recipe_to_sbepgsm(
            {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}),
        name="Tone", author="me")
    assert body["meta"]["name"] == "Tone"
    assert body["meta"]["author"] == "me"
    assert controllers.input_mode_for_model(
        body["meta"]["device_id"], "P35_InputInst1") == "inst1"


def test_author_is_omitted_when_not_supplied():
    body = untranscode.sbepgsm_to_hsp(
        transcode.recipe_to_sbepgsm(
            {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}))
    assert "author" not in body["meta"]


def test_does_not_mutate_its_input():
    doc = transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]})
    ref = copy.deepcopy(doc)
    untranscode.sbepgsm_to_hsp(doc, name="x")
    assert doc == ref


def test_stereo_input_params_are_nested_per_channel():
    """The device names the stereo input's params `Pad.1`/`Pad.2`; the `.hsp`
    nests them. `bridge._lift_endpoint_params` reads BOTH spellings, so the
    round-trip oracle is blind here — but `view._lift_input` and `mutate`'s
    `input` pseudo-block only address the nested shape, so a flat `.hsp` makes
    `view` drop the input section and `set-param input …` a silent no-op."""
    body = _assert_roundtrip({"name": "stereo", "paths": [{
        "input": "both",
        "input_params": {"Trim.1": -6.0, "Trim.2": -6.0,
                         "noiseGate.1": True, "noiseGate.2": True},
        "blocks": [{"block": AMP, "params": {}}],
    }]})
    params = _flow0(body)["b00"]["slot"][0]["params"]
    assert params["Trim"] == {"1": {"value": -6.0}, "2": {"value": -6.0}}
    assert "Trim.1" not in params
    assert params["StereoLink"] == {"value": False}  # unsuffixed, left alone


def test_stereo_nesting_needs_both_channels():
    """A lone `.1` is not the per-channel shape; folding it would invent a
    half-populated wrapper the device never wrote."""
    assert untranscode._nest_stereo_channels({"Trim.1": {"value": 1}}) == \
        {"Trim.1": {"value": 1}}


def test_mono_input_params_are_left_flat():
    body = _assert_roundtrip({"name": "mono", "paths": [{
        "input": "inst1", "blocks": [{"block": AMP, "params": {}}]}]})
    params = _flow0(body)["b00"]["slot"][0]["params"]
    assert "Trim" in params and isinstance(params["Trim"].get("value"), float)


def test_two_split_pairs_are_wired_to_their_own_partners():
    """`view.branch_span` reads a split's `endpoint` + both `branch` keys to
    recover the parallel lane. Pairing the first split with the LAST join
    reports one giant bogus parallel section. The forward path ignores these
    pointers, so the round trip cannot catch it."""
    entries = {
        "b01": {"type": "split"}, "b03": {"type": "join"},
        "b05": {"type": "split"}, "b07": {"type": "join"},
        "b16": {"type": "fx"}, "b20": {"type": "fx"},
    }
    untranscode._endpoint_pointers(entries)
    assert entries["b01"]["endpoint"] == "b03"
    assert entries["b03"]["endpoint"] == "b01"
    assert entries["b05"]["endpoint"] == "b07"
    assert entries["b07"]["endpoint"] == "b05"


def test_split_with_an_empty_branch_lane_still_pairs():
    """No lane-1 block is a valid (if pointless) split. Emitting NO pointers
    at all leaves `view` unable to see the parallel structure exists."""
    entries = {"b05": {"type": "split"}, "b07": {"type": "join"}}
    untranscode._endpoint_pointers(entries)
    assert entries["b05"]["endpoint"] == "b07"
    assert entries["b07"]["endpoint"] == "b05"
    assert "branch" not in entries["b05"]


# --- preset scalars the forward path used to hardcode -------------------------

@pytest.mark.parametrize("hsp_path,pm_key,value", [
    (("preset", "params", "tempo"), "preset.tempo.bpm", 145.0),
    (("meta", "info"), "preset.meta.info", "live version"),
    (("preset", "params", "activeexpsw"), "preset.expsw.active", 2),
    (("preset", "xyctrl", "x"), "preset.xyctrl.x", 3),
    (("preset", "clip", "filename"), "preset.clip.filename", "MY CLIP"),
])
def test_preset_scalars_survive_the_forward_transcode(hsp_path, pm_key, value):
    """`_synth_pm` hardcoded these, so every install/sync silently reset a
    preset's tempo to 120 and wiped the Preset Info text `device set-info`
    writes. Same class of bug as the hardcoded `cg__.asnp`."""
    body = untranscode.sbepgsm_to_hsp(transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}))
    node = body
    for k in hsp_path[:-1]:
        node = node.setdefault(k, {})
    node[hsp_path[-1]] = value
    pm = {e["key_"]: e["val_"]
          for e in content.decode_any(transcode.hsp_to_sbepgsm(body))["pm__"]}
    assert pm[pm_key] == value


def test_preset_scalars_reject_a_wrongly_typed_value():
    """A hand-edited .hsp must not write a string where the device reads a
    float — fall back to the default rather than corrupt the slot."""
    body = untranscode.sbepgsm_to_hsp(transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}))
    body["preset"]["params"]["tempo"] = "fast"
    pm = {e["key_"]: e["val_"]
          for e in content.decode_any(transcode.hsp_to_sbepgsm(body))["pm__"]}
    assert pm["preset.tempo.bpm"] == 120.0


def test_snapshot_colour_survives_the_forward_transcode():
    body = untranscode.sbepgsm_to_hsp(transcode.recipe_to_sbepgsm({
        "name": "x", "snapshots": [{"name": "A"}, {"name": "B"}],
        "paths": [{"blocks": [{"block": AMP, "params": {}}]}]}))
    body["preset"]["snapshots"][0]["color"] = "purple"
    body["preset"]["snapshots"][1]["valid"] = False
    snps = content.decode_any(
        transcode.hsp_to_sbepgsm(body))["cg__"]["entt"]["snps"]
    by_si = {s["si__"]: s for s in snps}
    assert by_si[0]["colr"] == 9 and by_si[0]["vald"] is True
    assert by_si[1]["vald"] is False


# --- vocabulary inverses ------------------------------------------------------

@pytest.mark.parametrize("source", [
    0x01010100, 0x01010104, 0x0101010b,     # stomp bank A
    0x01010200, 0x01010205,                 # stomp bank B
    0x01010400, 0x01010409,                 # looper-function bank
    0x01010500,                             # EXP1 toe switch
    0x01020100, 0x01020101,                 # EXP1 / EXP2
])
def test_source_id_inverts_the_forward_mapping(source):
    locl, ctxt = _controller_locl_ctxt(source)
    assert untranscode._source_id(locl, ctxt) == source


def test_source_id_rejects_unmapped_pairs():
    assert untranscode._source_id(None, 1) is None
    assert untranscode._source_id(99, 7) is None


def test_reverse_modelmap_is_injective_on_the_shipped_asset():
    from helixgen.device import modelmap

    forward = modelmap.load_modelmap().get("map", {})
    rev = untranscode._reverse_modelmap()
    assert len(rev) == len(set(forward.values()))
    for hg, dev in forward.items():
        assert untranscode._hsp_model(dev, rev) is not None


def test_param_plan_follows_library_order_not_pid_order(tmp_path):
    """Order is load-bearing: the forward transcoder allocates ``cg__`` target
    ids while walking a block's params in ``.hsp`` dict order, so emitting them
    in device-pid order shuffles every downstream id."""
    from helixgen.library import Block, Library

    mid = defs.model_id_for(AMP)
    library = Library(root=tmp_path / "lib")
    names = ["Master", "Bass", "Drive"]
    library.save_block(Block(model_id=AMP, category="amp", display_name="Amp",
                             params={n: {"type": "float", "default": 0.5}
                                     for n in names},
                             exemplar={}, first_seen={}))
    plan = untranscode._param_plan(mid, AMP, library)
    assert [n for _, n in plan[:3]] == names
    assert [p for p, _ in plan[:3]] != sorted(p for p, _ in plan[:3])
    # every device pid still reaches the .hsp, even the ones the block omits
    assert {p for p, _ in plan} == {
        m["id"] for m in defs.model_params_for(mid).values() if m.get("id") is not None}


def test_param_plan_falls_back_without_a_library():
    mid = defs.model_id_for(AMP)
    plan = untranscode._param_plan(mid, AMP, None)
    assert {n for _, n in plan} == set(defs.model_params_for(mid))


# --- warnings for what is NOT yet reversed ------------------------------------

def test_midi_controllers_are_dropped_with_a_warning(capsys):
    doc = transcode.recipe_to_sbepgsm({"name": "midi", "paths": [{"blocks": [
        {"block": DRIVE, "params": {}, "midi_bypass": {"cc": 20}},
    ]}]})
    assert any(c.get("trig") == 0 for c in doc["cg__"]["entt"]["ctrl"])
    body = untranscode.sbepgsm_to_hsp(doc, name="x")
    assert "MIDI" in capsys.readouterr().err
    assert "controller" not in body["preset"]["flow"][0]["b01"]["@enabled"]


def test_commands_are_dropped_with_a_warning(capsys):
    doc = transcode.recipe_to_sbepgsm({
        "name": "cmd",
        "commands": [{"source": 0x01010100, "type": "PresetSnapshot",
                      "func": 0, "params": {"Snapshot": 1}}],
        "paths": [{"blocks": [{"block": AMP, "params": {}}]}],
    })
    assert doc["cg__"]["entt"]["cmnd"]
    untranscode.sbepgsm_to_hsp(doc, name="x")
    assert "Command Center" in capsys.readouterr().err


def _one_amp_doc() -> dict:
    return transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]})


def _first_user_block(doc: dict) -> dict:
    return next(b for gp, b in untranscode._iter_blocks(doc["sfg_"]["flow"][0])
                if gp == 1)


@pytest.mark.parametrize("phrase,mutate", [
    # A dual-cab B model with no helixgen counterpart: the A cab still stands,
    # but the pairing is gone and the user has to be told.
    ("model slot 1 has no helixgen model",
     lambda d: _add_second_model_slot(d, 999999, {})),
    # A disabled DSP path: _canonical_flow always writes enbl=1.
    ("DISABLED", lambda d: d["sfg_"]["flow"][0].__setitem__("enbl", 0)),
    ("signal-flow graph is DISABLED", lambda d: d["sfg_"].__setitem__("enbl", 0)),
    # Row-1 blocks with no split: they KEEP their slots (bead hgc-8o6) but the
    # forward path still writes InputNone/OutputNone into row 1, so nothing
    # feeds them. A second rig going missing must never be silent.
    ("have no split feeding them", lambda d: _shift_block(d, 1, 16)),
])
def test_unreproducible_device_state_warns(phrase, mutate, capsys):
    """Everything this converter cannot carry has to reach stderr. Silence must
    mean "nothing was dropped", or the user has no way to know."""
    doc = _one_amp_doc()
    mutate(doc)
    untranscode.sbepgsm_to_hsp(doc, name="x")
    assert phrase in capsys.readouterr().err


def test_a_gapped_serial_row_round_trips_with_its_positions(capsys):
    """Real device content leaves GAPS in a row — 61 of Line 6's 66 factory
    presets trip the gap check, and the hardware ships and plays them. The
    forward path used to
    re-pack the row onto 1..n, which MOVED the user's layout and cost the round
    trip its fixed point (bead hgc-8o6)."""
    doc = _one_amp_doc()
    _shift_block(doc, 1, 5)                     # the amp sits at grid slot 5
    sbe1 = content.encode_content_data(doc)
    body = untranscode.sbe_bytes_to_hsp(sbe1, name="RT")
    assert [k for k in _flow0(body) if k.startswith("b")] == ["b00", "b05", "b13"]
    assert _flow0(body)["b05"]["position"] == 5
    assert transcode.hsp_to_sbepgsm(body) == sbe1   # a fixed point, byte-exact
    assert "not contiguous" not in capsys.readouterr().err


def _shift_block(doc: dict, frm: int, to: int) -> None:
    """Move a block to another grid slot the way device content carries it —
    the ``blks`` coordinate AND the identity id (``bmap[gridpos]``)."""
    flow = doc["sfg_"]["flow"][0]
    blks = flow["blks"]
    i = blks.index(frm)
    blks[i] = to
    blks[i + 1]["id__"] = flow["bmap"][to]


# --- dual cab: a block with two model slots (bead hgc-q38) --------------------

def _add_second_model_slot(doc: dict, dev_id: int,
                           params: dict, *, block=None) -> dict:
    """Append a second ``mdls`` entry to a block, the way the device stores a
    dual cab: one block instance, two model instances. Returns the new entry.

    ``params`` is keyed by DEVICE param name. Built through the forward
    transcoder's own synthesis so the entry is shaped exactly like one the
    device wrote — otherwise the round-trip oracle would be testing the
    fixture, not the code.
    """
    blk = _first_user_block(doc) if block is None else block
    name = defs.model_name_for(dev_id)
    _, m = transcode._make_model_instance(name or CAB_A, params, None)
    m["id__"] = dev_id      # an unresolvable id, when that is the point
    blk["mdls"].append(m)
    return m


# Two real Stadium cab models: the A and B mics of one dual-cab block.
CAB_A = "HD2_CabMicIr_4x12SoloLeadEMWithPan"
CAB_B = "HD2_CabMicIr_1x12OpenCreamWithPan"


def _dual_cab_doc() -> dict:
    """Device content whose single cab block carries two model slots with
    DIFFERENT models and different mic/level/EQ — the factory shape (corpus:
    78 such blocks across the 66 Line 6 factory presets)."""
    doc = transcode.recipe_to_sbepgsm({"name": "dualcab", "paths": [{"blocks": [
        {"block": AMP, "params": {}},
        {"block": CAB_A, "params": {"Mic": 3, "LowCut": 50.0, "Level": 1.0}},
    ]}]})
    cab = next(b for gp, b in untranscode._iter_blocks(doc["sfg_"]["flow"][0])
               if gp == 2)
    _add_second_model_slot(doc, defs.model_id_for(CAB_B),
                           {"Mic": 9, "LowCut": 90.0, "Level": -3.0},
                           block=cab)
    return doc


def test_dual_cab_second_slot_survives_the_round_trip():
    """The B cab is a different mic at a different level — dropping it halved
    every converted factory preset's cab. It has to reach the ``.hsp`` AND come
    back out of the forward transcoder unchanged."""
    doc = _dual_cab_doc()
    sbe1 = content.encode_content_data(doc)
    body = untranscode.sbe_bytes_to_hsp(sbe1, name="dualcab")

    slots = _flow0(body)["b02"]["slot"]
    assert [s["model"] for s in slots] == [CAB_A, CAB_B]
    assert slots[1]["params"]["Mic"]["value"] == 9
    assert slots[1]["params"]["Level"]["value"] == pytest.approx(-3.0)
    assert slots[0]["params"]["Level"]["value"] == pytest.approx(1.0)

    assert transcode.hsp_to_sbepgsm(body) == sbe1


def test_dual_cab_conversion_is_silent(capsys):
    """A carried B cab is not a loss, so it must not warn — the contract is
    that stderr silence means nothing was dropped."""
    untranscode.sbepgsm_to_hsp(_dual_cab_doc(), name="x")
    assert capsys.readouterr().err == ""


def test_dual_cab_second_slot_bypass_survives():
    """A B slot the user switched off (``mdls[1].enbl == 0``) is how a dual-cab
    block runs single — losing the flag would turn the second mic back on."""
    doc = _dual_cab_doc()
    cab = next(b for gp, b in untranscode._iter_blocks(doc["sfg_"]["flow"][0])
               if gp == 2)
    cab["mdls"][1]["enbl"] = 0
    sbe1 = content.encode_content_data(doc)
    body = untranscode.sbe_bytes_to_hsp(sbe1, name="x")
    assert _flow0(body)["b02"]["slot"][1]["@enabled"]["value"] is False
    assert transcode.hsp_to_sbepgsm(body) == sbe1


def test_snapshot_target_on_the_b_slot_is_not_read_onto_the_a_slot(capsys):
    """Both model slots live under ONE ``eID_`` and share the cab model's pids,
    so the ``cg__`` target's ``slot`` field is the only thing separating them.
    Keying on ``(eid, pid)`` alone handed the B cab's per-snapshot array to the
    A cab's param — silent, wrong, and audible."""
    doc = transcode.recipe_to_sbepgsm({
        "name": "x", "snapshots": [{"name": "A"}, {"name": "B"}],
        "paths": [{"blocks": [
            {"block": CAB_A, "params": {"Level": 1.0},
             "snap_params": {"Level": [1.0, -2.0] + [-2.0] * 6}},
        ]}]})
    cab = _first_user_block(doc)
    _add_second_model_slot(doc, defs.model_id_for(CAB_A), {"Level": -3.0},
                           block=cab)
    # Re-point the existing param target at model slot 1 (the B cab).
    trg = next(t for t in doc["cg__"]["entt"]["trgs"] if t.get("type") == 2)
    trg["slot"] = 1

    body = untranscode.sbepgsm_to_hsp(doc, name="x")
    slots = _flow0(body)["b01"]["slot"]
    assert "snapshots" not in slots[0]["params"]["Level"]
    assert slots[1]["params"]["Level"]["snapshots"][:2] == [1.0, -2.0]
    # ... and the loss it implies (no forward spelling) is reported.
    assert "model slot 1 carries snapshot or controller assignments" \
        in capsys.readouterr().err


def test_unmappable_controller_source_warns(capsys):
    """`_controller_locl_ctxt` can PRODUCE (locl, ctxt) pairs `_source_id`
    cannot invert. Dropping the assignment silently leaves the user hunting a
    footswitch that stopped working."""
    doc = transcode.recipe_to_sbepgsm({"name": "x", "paths": [{"blocks": [
        {"block": DRIVE, "params": {},
         "fs_bypass": {"source": 0x01010100, "behavior": "latching"}}]}]})
    doc["cg__"]["entt"]["srcs"][0]["locl"] = 40  # bank A, out of the anchored range
    body = untranscode.sbepgsm_to_hsp(doc, name="x")
    assert "no .hsp source id" in capsys.readouterr().err
    assert "controller" not in body["preset"]["flow"][0]["b01"]["@enabled"]


def test_partial_snapshot_target_warns(capsys):
    """A target the device did not write into every snapshot's tamv has no
    readable per-snapshot array; losing its scenes silently would look like the
    snapshots simply had no deltas."""
    doc = transcode.recipe_to_sbepgsm({
        "name": "x", "snapshots": [{"name": "A"}, {"name": "B"}],
        "paths": [{"blocks": [{"block": AMP, "params": {"Bass": 0.4},
                               "snap_params": {"Bass": [0.4, 0.7] + [0.7] * 6}}]}]})
    doc["cg__"]["entt"]["snps"][1]["tamv"] = []  # drop one snapshot's values
    body = untranscode.sbepgsm_to_hsp(doc, name="x")
    assert "missing from some snapshots' tamv" in capsys.readouterr().err
    params = body["preset"]["flow"][0]["b01"]["slot"][0]["params"]
    assert "snapshots" not in params["Bass"]


def test_clean_conversion_is_silent(capsys):
    """The contract is "silence means nothing was dropped" — so an ordinary
    preset must not emit warnings, or the real ones get tuned out."""
    untranscode.sbepgsm_to_hsp(_one_amp_doc(), name="x")
    assert capsys.readouterr().err == ""


def test_unresolvable_model_is_dropped_with_a_warning(capsys):
    doc = transcode.recipe_to_sbepgsm(
        {"name": "x", "paths": [{"blocks": [{"block": AMP, "params": {}}]}]})
    for item in doc["sfg_"]["flow"][0]["blks"]:
        if isinstance(item, dict) and item["mdls"][0]["id__"] == defs.model_id_for(AMP):
            item["mdls"][0]["id__"] = 999999
    body = untranscode.sbepgsm_to_hsp(doc, name="x")
    assert "999999" in capsys.readouterr().err
    assert "b01" not in body["preset"]["flow"][0]


# --- real-blob corpus (gitignored fixtures) -----------------------------------

def _corpus() -> list[Path]:
    return sorted(FIXDIR.glob("*.sbe")) + sorted(FIXDIR.glob("*.sbepgsm"))


@pytest.mark.parametrize("path", _corpus() or [None])
def test_corpus_roundtrip_is_a_fixed_point(path):
    """On real device blobs the conversion is a CANONICALIZATION: the second
    pass reproduces the first exactly, in both directions. That is what makes
    the residual first-pass diff (device-side serialization conventions and
    ``cg__`` id numbering on content the DEVICE re-saved) sonically null rather
    than lossy.
    """
    if path is None:
        pytest.skip(f"no device-content corpus in {FIXDIR}")
    blob = path.read_bytes()
    body1 = untranscode.sbe_bytes_to_hsp(blob, name=path.stem)
    sbe1 = transcode.hsp_to_sbepgsm(body1)
    body2 = untranscode.sbe_bytes_to_hsp(sbe1, name=path.stem)
    assert body2 == body1, f"{path.name}: .hsp not stable across the loop"
    assert transcode.hsp_to_sbepgsm(body2) == sbe1, \
        f"{path.name}: .sbe not a fixed point"


def _semantics(doc: dict) -> dict:
    """An IDENTITY-FREE projection of device content: everything that decides
    how the preset SOUNDS and behaves, with every instance/target/source/
    controller id resolved away.

    The residual round-trip diff on content the DEVICE re-saved is exactly the
    identity layer — ``cg__`` target ids, ``srcs``/``ctrl`` ids, block ``id__``
    and the ``bmap`` permutation they induce, plus ``pm__`` list order and the
    ``snps.si__`` order. Comparing this projection instead of the raw document
    is what turns "sonically null" into an assertion: a preset is addressed
    here by GRID POSITION and PARAM ID, so renumbering cancels out while any
    real change to a model, a value, a snapshot scene or a controller
    assignment still shows up.
    """
    entt = (doc.get("cg__") or {}).get("entt") or {}
    trgs = {t["id__"]: t for t in entt.get("trgs") or []}
    srcs = {s["id__"]: s for s in entt.get("srcs") or []}
    # eID_ -> (flow index, grid position): the identity-free address.
    where: dict = {}
    flows = []
    for fi, flow in enumerate(doc.get("sfg_", {}).get("flow") or []):
        blocks = []
        for gp, blk in untranscode._iter_blocks(flow):
            where[blk.get("id__")] = (fi, gp)
            # EVERY model slot, not just mdls[0]: a dropped dual-cab second
            # slot is exactly the kind of loss this projection exists to catch.
            models = tuple(
                (m.get("id__"), m.get("enbl"), m.get("vers"), m.get("irmd"),
                 tuple(sorted((p.get("pid_"), p.get("valu"), p.get("snap"),
                               # tid_ != 0 is the BINDING that makes the device
                               # apply a snapshot/controller value at all; a
                               # preset with intact tamv and zeroed tid_ sounds
                               # static, so identity-free "is it bound" has to
                               # be compared, not the id itself.
                               bool(p.get("tid_")))
                              for p in m.get("parm") or [])))
                for m in blk.get("mdls") or [])
            hrns = blk.get("hrns") or {}
            blocks.append((
                gp, blk.get("enbl"), blk.get("type"), blk.get("favo"),
                blk.get("hasb"), bool(blk.get("tid_")), blk.get("snap"),
                # split/join partner pointers ARE routing; bflw is a flow
                # index, bblk an instance id resolved to its grid address.
                blk.get("bflw"), where.get(blk.get("bblk"), blk.get("bblk")),
                (hrns.get("id__"), hrns.get("enbl"),
                 tuple(sorted((p.get("pid_"), p.get("valu"))
                              for p in hrns.get("parm") or []))),
                models))
        flows.append((flow.get("enbl"), tuple(blocks)))

    def target(tid):
        t = trgs.get(tid)
        if t is None:
            return None
        return (where.get(t.get("eID_")),
                "bypass" if t.get("type") == 1 else t.get("pid_"))

    snaps = sorted(entt.get("snps") or [], key=lambda s: s.get("si__", 0))
    scenes: dict = {}
    for i, s in enumerate(snaps):
        for tid, value in untranscode._tamv_map(s).items():
            key = target(tid)
            if key is not None:
                scenes.setdefault(key, {})[i] = value
    controllers = set()
    for c in entt.get("ctrl") or []:
        src = srcs.get(c.get("trig")) or {}
        controllers.add((
            untranscode._source_id(src.get("locl"), src.get("ctxt")),
            target(c.get("tid_")), c.get("type"), c.get("behv"), c.get("curv"),
            c.get("min_"), c.get("max_"), c.get("thrs"),
        ))
    return {
        "flows": flows,
        "scenes": {k: tuple(sorted(v.items())) for k, v in scenes.items()},
        "controllers": controllers,
        "snapshot_meta": [(s.get("name"), s.get("exsw"), s.get("bpm_"),
                           s.get("vald")) for s in snaps],
        "active_snapshot": (doc.get("cg__") or {}).get("asnp"),
        "sfg_enabled": (doc.get("sfg_") or {}).get("enbl"),
        # a key/value list: order is the device's own business, content is not
        "pm": {e.get("key_"): e.get("val_") for e in doc.get("pm__") or []},
    }


def _corrupt(doc, mutate):
    """Deep-copy ``doc``, apply ``mutate`` to the first user block, return it."""
    d = copy.deepcopy(doc)
    flow = d["sfg_"]["flow"][0]
    blk = next(b for gp, b in untranscode._iter_blocks(flow) if gp == 1)
    mutate(d, flow, blk)
    return d


@pytest.mark.parametrize("name,mutate", [
    ("dual-cab second model slot deleted",
     lambda d, f, b: b["mdls"].__delitem__(1)),
    ("flow re-enabled", lambda d, f, b: f.__setitem__("enbl", 1)),
    ("sfg re-enabled", lambda d, f, b: d["sfg_"].__setitem__("enbl", 1)),
    ("harness re-enabled", lambda d, f, b: b["hrns"].__setitem__("enbl", 1)),
    ("harness parm reset",
     lambda d, f, b: b["hrns"].__setitem__("parm", [])),
    ("favorite reset", lambda d, f, b: b.__setitem__("favo", 0)),
    ("hasb reset", lambda d, f, b: b.__setitem__("hasb", False)),
    ("model version reset", lambda d, f, b: b["mdls"][0].__setitem__("vers", 0)),
    ("model instance re-enabled",
     lambda d, f, b: b["mdls"][0].__setitem__("enbl", 1)),
    ("split partner pointer moved", lambda d, f, b: b.__setitem__("bblk", 999)),
    ("snapshot binding unbound",
     lambda d, f, b: [p.__setitem__("tid_", 0) for p in b["mdls"][0]["parm"]]),
])
def test_semantics_catches_the_corruption(name, mutate):
    """`_semantics` is the acceptance bar, so it has to be shown to FAIL on the
    things it claims to cover. Each of these once slipped through it."""
    doc = transcode.recipe_to_sbepgsm({"name": "x", "paths": [{"blocks": [
        {"block": AMP, "params": {"Bass": 0.4},
         "snap_params": {"Bass": [0.4, 0.7] + [0.7] * 6}},
    ]}], "snapshots": [{"name": "A"}, {"name": "B"}]})
    # give the block the state each corruption then removes
    flow = doc["sfg_"]["flow"][0]
    blk = next(b for gp, b in untranscode._iter_blocks(flow) if gp == 1)
    blk["mdls"].append(copy.deepcopy(blk["mdls"][0]))
    blk["favo"], blk["hasb"], blk["bblk"] = 1, True, 0
    blk["mdls"][0]["vers"], blk["mdls"][0]["enbl"] = 3, 0
    flow["enbl"], doc["sfg_"]["enbl"] = 0, 0
    blk["hrns"]["enbl"] = 0

    assert _semantics(_corrupt(doc, mutate)) != _semantics(doc), (
        f"_semantics did not notice: {name}")


@pytest.mark.parametrize("path", _corpus() or [None])
def test_corpus_roundtrip_is_semantically_identical(path):
    """The whole acceptance bar in one assertion: whatever the id-numbering and
    serialization residue, the round trip changes NOTHING about how the preset
    sounds or behaves — same blocks in the same grid slots with the same param
    values, the same per-snapshot scenes, the same controller assignments, the
    same preset params."""
    if path is None:
        pytest.skip(f"no device-content corpus in {FIXDIR}")
    blob = path.read_bytes()
    sbe2 = transcode.hsp_to_sbepgsm(
        untranscode.sbe_bytes_to_hsp(blob, name=path.stem))
    assert _semantics(content.decode_any(sbe2)) == \
        _semantics(content.decode_any(blob))


def test_corpus_is_mostly_byte_exact():
    """Content helixgen itself installed must round-trip BYTE-exactly; only
    content the DEVICE re-saved is allowed to differ. Pinned as a ratio so a
    regression that starts perturbing ordinary presets fails loudly even
    though the corpus is gitignored and its exact makeup varies."""
    corpus = _corpus()
    if len(corpus) < 10:
        pytest.skip(f"device-content corpus too small in {FIXDIR}")
    exact = sum(
        transcode.hsp_to_sbepgsm(
            untranscode.sbe_bytes_to_hsp(p.read_bytes(), name=p.stem))
        == p.read_bytes() for p in corpus)
    assert exact >= 0.85 * len(corpus), (
        f"only {exact}/{len(corpus)} device blobs round-tripped byte-exactly")
