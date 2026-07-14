"""End-to-end MIDI CC controller authoring (backlog #33): recipe -> .hsp
(helixgen-namespaced ``preset._helixgen_midi``) -> view round-trip -> device
transcode (``cg__.entt`` ctrl/ctm_ records per the parity-capture findings §6).
"""
from __future__ import annotations

import copy

import pytest

from tests.golden import harness


def _chassis():
    from helixgen.chassis import extract_chassis_from_hsp
    return extract_chassis_from_hsp(copy.deepcopy(harness._CHASSIS_PAYLOAD))


def _author(tmp_path, midi, **extra):
    from helixgen.recipe import apply_recipe
    library = harness.build_corpus_library(tmp_path)
    recipe = {
        "name": "midi tone",
        "paths": [{"blocks": [
            {"block": "Brit 2204 Custom", "params": {"Drive": 0.6}},
            {"block": "Digital", "params": {"Mix": 0.3}},
        ]}],
        "midi": midi,
        **extra,
    }
    body = apply_recipe(recipe, library, chassis=_chassis())
    return body, library


def test_hsp_carries_namespaced_midi_param(tmp_path):
    body, _ = _author(tmp_path, [
        {"cc": 61, "targets": [{"block": "Brit 2204 Custom", "param": "Drive",
                                "min": 0.2, "max": 0.9}]},
    ])
    recs = body["preset"]["_helixgen_midi"]
    assert len(recs) == 1
    r = recs[0]
    assert r["cc"] == 61 and r["param"] == "Drive"
    assert r["path"] == 0 and r["min"] == 0.2 and r["max"] == 0.9
    # NOT written as a device-native controller: the block's @enabled/params
    # carry no `controller` with a MIDI source.
    b01 = body["preset"]["flow"][0]["b01"]
    assert "controller" not in b01["slot"][0]["params"].get("Drive", {})


def test_hsp_carries_namespaced_midi_bypass(tmp_path):
    body, _ = _author(tmp_path, [
        {"cc": 79, "targets": [{"block": "Digital", "bypass": True}]},
    ])
    r = body["preset"]["_helixgen_midi"][0]
    assert r["cc"] == 79 and r["param"] is None


def test_view_round_trips_midi(tmp_path):
    from helixgen.view import view
    body, library = _author(tmp_path, [
        {"cc": 61, "targets": [{"block": "Brit 2204 Custom", "param": "Drive",
                                "min": 0.2, "max": 0.9}]},
        {"cc": 79, "targets": [{"block": "Digital", "bypass": True}]},
    ])
    projected = view(body, library)
    assert "midi" in projected
    by_cc = {m["cc"]: m for m in projected["midi"]}
    assert by_cc[61]["targets"][0]["param"] == "Drive"
    assert by_cc[61]["targets"][0]["min"] == 0.2
    assert by_cc[79]["targets"][0]["bypass"] is True
    # re-parse the projection: it must be a valid recipe again
    from helixgen.spec import parse_spec
    reparsed = parse_spec(projected)
    assert len(reparsed.midi) == 2


# --- edit-verb reconciliation: _helixgen_midi coordinates must track ---------
# renumbering (adversarial-review round 2, Important). Pinned end-to-end
# through bridge.hsp_to_paths AND the transcoded ctrl[] output, using a
# mini-library of REAL device model ids (the corpus-harness models are
# synthetic and don't resolve in the device modelmap).

def _real_library(tmp_path):
    """Library of 3 device-resolvable blocks: Minotaur (drive), Brit Plexi Nrm
    (amp), DL4 Digital (delay). Param names match the device defs so the
    bridge's name map is identity."""
    import copy as _copy
    from helixgen.chassis import extract_chassis_from_hsp
    from helixgen.library import Block, Library
    lib = Library(root=tmp_path / "reallib")
    lib.save_chassis(extract_chassis_from_hsp(
        _copy.deepcopy(harness._CHASSIS_PAYLOAD)))
    src = {"preset": "midi-test", "firmware": "test", "date": "2026-07-14"}
    for mid, cat, name, params in [
        ("HD2_DistMinotaurMono", "drive", "Minotaur",
         {"Gain": 0.5, "Level": 0.5, "Tone": 0.5}),
        ("HD2_AmpBritPlexiNrm", "amp", "Brit Plexi Nrm",
         {"Bass": 0.5, "Drive": 0.5, "Mid": 0.5, "Treble": 0.5}),
        ("HD2_DL4DigDelay", "delay", "DL4 Digital",
         {"Feedback": 0.4, "Level": 0.5, "Mix": 0.3}),
    ]:
        lib.save_block(Block(
            model_id=mid, category=cat, display_name=name,
            params={k: {"type": "float"} for k in params},
            exemplar={"@model": mid, "@type": "fx", "@enabled": True, **params},
            first_seen=src,
        ))
    lib.rebuild_index()
    return lib


def _author3(tmp_path, midi):
    """Three-block chain of real device models: Minotaur (pos1) /
    Brit Plexi Nrm (pos2) / DL4 Digital (pos3)."""
    from helixgen.recipe import apply_recipe
    library = _real_library(tmp_path)
    recipe = {
        "name": "midi edit",
        "paths": [{"blocks": [
            {"block": "Minotaur", "params": {"Gain": 0.3}},
            {"block": "Brit Plexi Nrm", "params": {"Drive": 0.6}},
            {"block": "DL4 Digital", "params": {"Mix": 0.3}},
        ]}],
        "midi": midi,
    }
    return apply_recipe(recipe, library, chassis=_chassis()), library


def _transcoded_midi_ctrls(body):
    """{cc: (ctrl_entry, its target trg)} from the fully transcoded body."""
    from helixgen.device import content, transcode
    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    entt = doc["cg__"]["entt"]
    out = {}
    for c in entt["ctrl"]:
        cc = c.get("cnt2")
        if cc:
            trg = next(t for t in entt["trgs"] if t["id__"] == c["tid_"])
            out[cc] = (c, trg)
    return out


def test_remove_block_before_midi_target_shifts_pos(tmp_path):
    from helixgen import mutate
    from helixgen.device import defs
    body, library = _author3(tmp_path, [
        {"cc": 20, "targets": [{"block": "DL4 Digital", "param": "Mix"}]},
    ])
    mutate.remove_block(body, "Minotaur", library)
    rec = body["preset"]["_helixgen_midi"][0]
    assert rec["pos"] == 2  # delay moved b03 -> b02
    # the transcoded ctrl still targets the DELAY's Mix, not whatever now
    # sits at the old coordinate
    ctrls = _transcoded_midi_ctrls(body)
    c, trg = ctrls[20]
    dl4 = defs.model_id_for("HD2_DL4DigDelay")
    assert trg["mmid"] == dl4
    assert trg["pid_"] == defs.load_defs()["model_params"][str(dl4)]["Mix"]["id"]
    assert c["type"] == 3 and c["midi"] == 0xB000 | 20


def test_add_block_before_midi_target_shifts_pos(tmp_path):
    from helixgen import mutate
    from helixgen.device import defs
    body, library = _author3(tmp_path, [
        {"cc": 21, "targets": [{"block": "Brit Plexi Nrm", "param": "Drive"}]},
        {"cc": 22, "targets": [{"block": "DL4 Digital", "bypass": True}]},
    ])
    mutate.add_block(body, "Minotaur", library, after="Minotaur")
    recs = {r["cc"]: r for r in body["preset"]["_helixgen_midi"]}
    assert recs[21]["pos"] == 3  # amp b02 -> b03
    assert recs[22]["pos"] == 4  # delay b03 -> b04
    ctrls = _transcoded_midi_ctrls(body)
    amp = defs.model_id_for("HD2_AmpBritPlexiNrm")
    dl4 = defs.model_id_for("HD2_DL4DigDelay")
    c21, t21 = ctrls[21]
    assert t21["mmid"] == amp and c21["type"] == 3
    assert t21["pid_"] == defs.load_defs()["model_params"][str(amp)]["Drive"]["id"]
    c22, t22 = ctrls[22]
    assert t22["mmid"] == dl4 and c22["type"] == 1 and t22["pid_"] == 0


def test_remove_midi_bound_block_drops_record_with_warning(tmp_path, capsys):
    from helixgen import mutate
    from helixgen.device import defs
    body, library = _author3(tmp_path, [
        {"cc": 30, "targets": [{"block": "Brit Plexi Nrm", "param": "Drive"}]},
        {"cc": 31, "targets": [{"block": "DL4 Digital", "bypass": True}]},
    ])
    mutate.remove_block(body, "Brit Plexi Nrm", library)
    err = capsys.readouterr().err
    assert "MIDI CC 30" in err and "dropped" in err
    recs = body["preset"]["_helixgen_midi"]
    assert [r["cc"] for r in recs] == [31]
    assert recs[0]["pos"] == 2  # delay compacted b03 -> b02
    ctrls = _transcoded_midi_ctrls(body)
    assert set(ctrls) == {31}
    _c, trg = ctrls[31]
    assert trg["mmid"] == defs.model_id_for("HD2_DL4DigDelay")
    assert trg["type"] == 1


def test_swap_away_midi_bound_param_drops_with_warning(tmp_path):
    """Swap DL4 Digital (has Bass) for Adriatic (no Bass): the Bass binding is
    dropped with a warning; the Mix binding survives with its stored block
    name refreshed AND its transcoded ctrl targeting the NEW model."""
    import copy as _copy
    from helixgen import mutate
    from helixgen.chassis import extract_chassis_from_hsp
    from helixgen.device import defs
    from helixgen.library import Block
    from helixgen.recipe import apply_recipe
    library = _real_library(tmp_path)
    library.save_block(Block(
        model_id="HD2_DelayAdriaticDelayMono", category="delay",
        display_name="Adriatic",
        params={k: {"type": "float"} for k in ("Depth", "Feedback", "Level", "Mix")},
        exemplar={"@model": "HD2_DelayAdriaticDelayMono", "@type": "fx",
                  "@enabled": True, "Depth": 0.5, "Feedback": 0.4,
                  "Level": 0.5, "Mix": 0.3},
        first_seen={"preset": "midi-test", "firmware": "test",
                    "date": "2026-07-14"},
    ))
    library.rebuild_index()
    # give the authored DL4 a Bass param so CC40 can bind it
    body = apply_recipe({
        "name": "swap midi",
        "paths": [{"blocks": [
            {"block": "Brit Plexi Nrm"},
            {"block": "DL4 Digital", "params": {"Mix": 0.3}},
        ]}],
        "midi": [
            {"cc": 40, "targets": [{"block": "DL4 Digital", "param": "Feedback"}]},
            {"cc": 41, "targets": [{"block": "DL4 Digital", "param": "Mix"}]},
        ],
    }, library, chassis=extract_chassis_from_hsp(
        _copy.deepcopy(harness._CHASSIS_PAYLOAD)))
    # narrow Adriatic so it LACKS Feedback for this test
    adriatic = library.load_block("HD2_DelayAdriaticDelayMono")
    adriatic.params.pop("Feedback")
    library.save_block(adriatic)
    library.rebuild_index()
    warnings = mutate.swap_model(body, "DL4 Digital", "Adriatic", library)
    assert any("MIDI CC 40" in w and "Feedback" in w for w in warnings)
    recs = body["preset"]["_helixgen_midi"]
    assert [r["cc"] for r in recs] == [41]
    assert recs[0]["block"] == "Adriatic"  # stored name refreshed
    ctrls = _transcoded_midi_ctrls(body)
    adr = defs.model_id_for("HD2_DelayAdriaticDelayMono")
    _c, trg = ctrls[41]
    assert trg["mmid"] == adr
    assert trg["pid_"] == defs.load_defs()["model_params"][str(adr)]["Mix"]["id"]


def test_view_drops_unresolvable_midi_coordinate(tmp_path, capsys):
    """A structurally-valid record whose coordinate resolves to no placed
    block is dropped with a warning — never silently projected via its stored
    block name (install would not honor it either)."""
    from helixgen.view import view
    body, library = _author(tmp_path, [
        {"cc": 61, "targets": [{"block": "Brit 2204 Custom", "param": "Drive"}]},
    ])
    body["preset"]["_helixgen_midi"].append(
        {"cc": 50, "path": 0, "lane": 0, "pos": 9,
         "block": "Brit 2204 Custom", "param": "Drive"})
    projected = view(body, library)
    assert [m["cc"] for m in projected["midi"]] == [61]
    assert "MIDI CC 50" in capsys.readouterr().err


def test_view_warns_on_stale_midi_block_name(tmp_path, capsys):
    """Coordinate resolves but the stored name mismatches: project the placed
    block (coordinate is authoritative) and warn."""
    from helixgen.view import view
    body, library = _author(tmp_path, [
        {"cc": 61, "targets": [{"block": "Brit 2204 Custom", "param": "Drive"}]},
    ])
    body["preset"]["_helixgen_midi"][0]["block"] = "Some Old Name"
    projected = view(body, library)
    assert projected["midi"][0]["targets"][0]["block"] == "Brit 2204 Custom"
    assert "coordinate is authoritative" in capsys.readouterr().err


def test_view_drops_corrupt_midi_records(tmp_path):
    """A hand-corrupted ``_helixgen_midi`` record (cc out of range, missing
    block) is dropped by ``view`` — mirroring the bridge's guards — so the
    projection never emits a ``midi`` entry parse_spec would reject."""
    from helixgen.spec import parse_spec
    from helixgen.view import view
    body, library = _author(tmp_path, [
        {"cc": 61, "targets": [{"block": "Brit 2204 Custom", "param": "Drive"}]},
    ])
    body["preset"]["_helixgen_midi"].extend([
        {"cc": 9999, "path": 0, "lane": 0, "pos": 1,
         "block": "Brit 2204 Custom", "param": "Drive"},   # cc out of range
        {"cc": 20, "path": 0, "lane": 0, "pos": 1,
         "block": None, "param": None},                     # missing block
        "not-a-dict",
    ])
    projected = view(body, library)
    assert [m["cc"] for m in projected["midi"]] == [61]
    parse_spec(projected)  # must remain a valid recipe


# --- device transcode: cg__.entt ctrl/ctm_ synthesis (findings §6) -----------
#
# The transcoder is exercised at the recipe layer (real device model ids, as
# in test_transcode.test_controller_graph_synthesis) because the corpus-harness
# models above are synthetic and do not resolve in the device defs. A separate
# test (test_bridge_lifts_namespaced_midi) covers the .hsp -> recipe hop.

pytest.importorskip("msgpack")


def _entt(recipe):
    from helixgen.device import content, transcode
    doc = content.decode_any(
        content.encode_content_data(transcode.recipe_to_sbepgsm(recipe)))
    return doc["cg__"]["entt"]


def test_transcode_midi_param_ctrl():
    from helixgen.device import defs
    amp_mid = defs.model_id_for("HD2_AmpBritPlexiNrm")
    bass_pid = defs.load_defs()["model_params"][str(amp_mid)]["Bass"]["id"]
    entt = _entt({
        "name": "m",
        "paths": [{"blocks": [
            {"block": "HD2_AmpBritPlexiNrm", "params": {"Bass": 0.5},
             "midi_params": {"Bass": {"cc": 61, "min": 0.1, "max": 0.8}}},
        ]}],
    })
    # one MIDI ctrl (type 3 = param), no physical srcs entry (source is inline)
    midi_ctrls = [c for c in entt["ctrl"] if c.get("cnt2") == 61]
    assert len(midi_ctrls) == 1
    c = midi_ctrls[0]
    assert c["type"] == 3
    assert c["cnt2"] == 61
    assert c["midi"] == 0xB000 | 61  # CC on device global base channel
    assert c["min_"] == 0.1 and c["max_"] == 0.8
    assert c["trig"] == 0  # no physical source slot
    # the param target: type2/enty3 on the amp Bass pid, packed into ptid
    trg = next(t for t in entt["trgs"] if t["id__"] == c["tid_"])
    assert trg["type"] == 2 and trg["enty"] == 3 and trg["pid_"] == bass_pid
    packed = (trg["eID_"] << 16) | bass_pid
    ptid = dict(zip(entt["ctm_"]["ptid"][::2], entt["ctm_"]["ptid"][1::2]))
    assert ptid.get(packed) == trg["id__"]
    # no physical source slot / scid entry for a MIDI source
    assert entt["srcs"] == []
    assert entt["sm__"]["scid"] == []


def test_transcode_midi_bypass_ctrl():
    entt = _entt({
        "name": "m",
        "paths": [{"blocks": [
            {"block": "HD2_AmpBritPlexiNrm", "params": {},
             "midi_bypass": {"cc": 79}},
        ]}],
    })
    midi_ctrls = [c for c in entt["ctrl"] if c.get("cnt2") == 79]
    assert len(midi_ctrls) == 1
    c = midi_ctrls[0]
    assert c["type"] == 1  # bypass
    assert c["midi"] == 0xB000 | 79
    assert c["min_"] is False and c["max_"] is True
    trg = next(t for t in entt["trgs"] if t["id__"] == c["tid_"])
    assert trg["type"] == 1 and trg["enty"] == 2 and trg["pid_"] == 0


def test_transcode_midi_shares_bypass_trg_with_footswitch():
    """An FS and a MIDI CC both driving one block's bypass reuse the SAME
    bypass target (the device supports multi-source bypass)."""
    entt = _entt({
        "name": "m",
        "paths": [{"blocks": [
            {"block": "HD2_AmpBritPlexiNrm", "params": {},
             "midi_bypass": {"cc": 79},
             "fs_bypass": {"source": 0x01010102, "behavior": "latching"}},
        ]}],
    })
    byp_trgs = [t for t in entt["trgs"] if t["type"] == 1]
    assert len(byp_trgs) == 1
    tid = byp_trgs[0]["id__"]
    # both the FS ctrl (trig->srcs) and the MIDI ctrl (cnt2) point at it
    midi_c = next(c for c in entt["ctrl"] if c.get("cnt2") == 79)
    fs_c = next(c for c in entt["ctrl"]
                if not c.get("cnt2") and c["type"] == 1)
    assert midi_c["tid_"] == tid and fs_c["tid_"] == tid


def test_transcode_midi_only_still_emits_ctrl():
    """A tone whose ONLY controller is MIDI (no snapshots, no FS/EXP) still
    builds a populated cg__ (not the blank-8 fallback)."""
    entt = _entt({
        "name": "m",
        "paths": [{"blocks": [
            {"block": "HD2_AmpBritPlexiNrm", "params": {"Bass": 0.5},
             "midi_params": {"Bass": {"cc": 20}}},
        ]}],
    })
    assert any(c.get("cnt2") == 20 for c in entt["ctrl"])


def test_bridge_lifts_namespaced_midi():
    """The .hsp -> recipe hop: bridge reads preset._helixgen_midi and attaches
    device-name-mapped midi_params / midi_bypass to the right block spec."""
    from helixgen.device import bridge, defs
    # minimal .hsp body with a real device model id at (path 0, b01)
    body = {
        "meta": {"device_id": 2490368},
        "preset": {
            "flow": [{
                "b01": {"@enabled": {"value": True}, "type": "amp",
                        "slot": [{"model": "HD2_AmpBritPlexiNrm",
                                  "params": {"Bass": {"value": 0.5}}}]},
            }],
            "_helixgen_midi": [
                {"cc": 61, "path": 0, "lane": 0, "pos": 1,
                 "block": "HD2_AmpBritPlexiNrm", "param": "Bass",
                 "min": 0.1, "max": 0.8},
                {"cc": 79, "path": 0, "lane": 0, "pos": 1,
                 "block": "HD2_AmpBritPlexiNrm", "param": None},
            ],
        },
    }
    paths = bridge.hsp_to_paths(body, strict=False)
    blk = paths[0]["blocks"][0]
    # Bass maps to its device param name
    dev_name = defs.model_name_for  # sanity import
    assert "midi_params" in blk
    (only_param, cfg), = blk["midi_params"].items()
    assert cfg["cc"] == 61 and cfg["min"] == 0.1 and cfg["max"] == 0.8
    assert blk["midi_bypass"] == {"cc": 79}
