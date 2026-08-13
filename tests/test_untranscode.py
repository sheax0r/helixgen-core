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


def test_active_snapshot_out_of_range_falls_back_to_zero():
    for bad in (-1, 8, 99, True, "2", None):
        doc = transcode.recipe_to_sbepgsm(
            {"name": "x", "active_snapshot": bad,
             "paths": [{"blocks": [{"block": AMP, "params": {}}]}]})
        assert doc["cg__"]["asnp"] == 0, bad


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
            m0 = (blk.get("mdls") or [{}])[0]
            blocks.append((gp, m0.get("id__"), blk.get("enbl"),
                           tuple(sorted((p.get("pid_"), p.get("valu"))
                                        for p in m0.get("parm") or []))))
        flows.append(tuple(blocks))

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
        # a key/value list: order is the device's own business, content is not
        "pm": {e.get("key_"): e.get("val_") for e in doc.get("pm__") or []},
    }


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
