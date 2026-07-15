"""End-to-end Command Center authoring (backlog #16): recipe -> .hsp
(native ``preset.commands``) -> view round-trip. The native encoding is the one
real exports carry (Mandarin Fuzz / Epic Lots of EQ), so commands are authored
directly into ``preset.commands`` (no sidecar) + a ``preset.sources`` entry."""
from __future__ import annotations

import copy

import pytest

from tests.golden import harness


def _chassis():
    from helixgen.chassis import extract_chassis_from_hsp
    return extract_chassis_from_hsp(copy.deepcopy(harness._CHASSIS_PAYLOAD))


def _author(tmp_path, commands, **extra):
    from helixgen.recipe import apply_recipe
    library = harness.build_corpus_library(tmp_path)
    recipe = {
        "name": "cc tone",
        "paths": [{"blocks": [
            {"block": "Brit 2204 Custom", "params": {"Drive": 0.6}},
            {"block": "Digital", "params": {"Mix": 0.3}},
        ]}],
        "commands": commands,
        **extra,
    }
    body = apply_recipe(recipe, library, chassis=_chassis())
    return body, library


def test_snapshot_command_native(tmp_path):
    body, _ = _author(tmp_path, [
        {"switch": "FS1", "command": "snapshot", "snapshot": 2},
    ])
    cmds = body["preset"]["commands"]
    key = str(0x01010100)  # FS1
    assert key in cmds
    rec = cmds[key][0]
    assert rec["type"] == "PresetSnapshot"
    assert rec["params"]["Snapshot"]["value"] == 2
    assert rec["ordinal"] == 0
    # A sources entry is registered for the FS scribble strip.
    assert body["preset"]["sources"][key]["fs_color"] == "auto"


def test_midi_cc_command_native(tmp_path):
    body, _ = _author(tmp_path, [
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127,
         "channel": 2, "toggle": True},
    ])
    key = str(0x04040100)  # Instant1
    rec = body["preset"]["commands"][key][0]
    assert rec["type"] == "MIDI"
    p = rec["params"]
    assert p["Command"]["value"] == 1        # CC subtype
    assert p["CC#"]["value"] == 85
    assert p["Value"]["value"] == 127
    assert p["MIDI Ch"]["value"] == 2
    assert rec["toggle"] is True
    # Instant source: bypass-only entry (no scribble).
    assert body["preset"]["sources"][key] == {"bypass": False}


def test_midi_pc_command_native(tmp_path):
    body, _ = _author(tmp_path, [
        {"switch": "Instant2", "command": "midi_pc", "program": 44, "channel": 4},
    ])
    p = body["preset"]["commands"][str(0x04040101)][0]["params"]
    assert p["Command"]["value"] == 0        # PC subtype
    assert p["PC"]["value"] == 44
    assert p["MIDI Ch"]["value"] == 4
    assert p["MSB"]["value"] == -1 and p["LSB"]["value"] == -1


def test_merged_switch_ordinals(tmp_path):
    body, _ = _author(tmp_path, [
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127},
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 0},
    ])
    recs = body["preset"]["commands"][str(0x04040100)]
    assert [r["ordinal"] for r in recs] == [0, 1]


def test_reserved_switch_rejected(tmp_path):
    from helixgen.mutate import MutateError
    with pytest.raises(Exception) as ei:
        _author(tmp_path, [{"switch": "FS6", "command": "snapshot", "snapshot": 1}])
    assert "MODE" in str(ei.value)


def test_view_round_trips_commands(tmp_path):
    from helixgen.view import view
    body, library = _author(tmp_path, [
        {"switch": "FS1", "command": "snapshot", "snapshot": 2, "label": "SNAP",
         "color": "red"},
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127,
         "channel": 2, "toggle": True},
        {"switch": "Instant2", "command": "midi_pc", "program": 44, "channel": 4},
        {"switch": "FS3", "command": "midi_note", "note": 60, "velocity": 100,
         "note_off": True, "channel": 1},
    ])
    projected = view(body, library)
    assert "commands" in projected
    by_switch = {}
    for c in projected["commands"]:
        by_switch.setdefault(c["switch"], []).append(c)
    assert by_switch["FS1"][0]["command"] == "snapshot"
    assert by_switch["FS1"][0]["snapshot"] == 2
    assert by_switch["FS1"][0]["label"] == "SNAP"
    assert by_switch["FS1"][0]["color"] == "red"
    assert by_switch["Instant1"][0]["command"] == "midi_cc"
    assert by_switch["Instant1"][0]["cc"] == 85
    assert by_switch["Instant1"][0]["toggle"] is True
    assert by_switch["Instant2"][0]["command"] == "midi_pc"
    assert by_switch["Instant2"][0]["program"] == 44
    assert by_switch["FS3"][0]["command"] == "midi_note"
    assert by_switch["FS3"][0]["note_off"] is True
    # The projection re-parses cleanly.
    from helixgen.spec import parse_spec
    parse_spec({"name": "x", "paths": [{"blocks": [
        {"block": "Brit 2204 Custom"}, {"block": "Digital"}]}],
        "commands": projected["commands"]})


def _real_library(tmp_path):
    from helixgen.library import Block, Library
    lib = Library(root=tmp_path)
    src = {"preset": "cc-test", "firmware": "test", "date": "2026-07-14"}
    lib.save_block(Block(
        model_id="HD2_AmpBritPlexiNrm", category="amp", display_name="Brit Plexi Nrm",
        params={k: {"type": "float"} for k in ("Bass", "Drive", "Mid", "Treble")},
        exemplar={"@model": "HD2_AmpBritPlexiNrm", "@type": "fx", "@enabled": True,
                  "Bass": 0.5, "Drive": 0.5, "Mid": 0.5, "Treble": 0.5},
        first_seen=src))
    lib.rebuild_index()
    return lib


def _transcode_entt(tmp_path, commands):
    from helixgen.recipe import apply_recipe
    from helixgen.device import content, transcode
    library = _real_library(tmp_path)
    recipe = {"name": "cc dev", "paths": [{"blocks": [
        {"block": "Brit Plexi Nrm", "params": {"Drive": 0.6}}]}],
        "commands": commands}
    body = apply_recipe(recipe, library, chassis=_chassis())
    doc = content.decode_any(transcode.hsp_to_sbepgsm(body))
    return doc["cg__"]


def test_transcode_snapshot_cmnd_matches_mandarin_anchor(tmp_path):
    """A PresetSnapshot command on FS1 reproduces the live Mandarin Fuzz cmnd
    shape (5 int + 5 bool slots, type 1); srcs FS1 = locl 25/ctxt 1/type 1."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "FS1", "command": "snapshot", "snapshot": 0}])
    entt = cg["entt"]
    assert len(entt["cmnd"]) == 1
    cm = entt["cmnd"][0]
    assert cm["type"] == 1 and cm["func"] == 0
    assert [cm[f"pvl{c}"] for c in "abcde"] == [0, 0, 0, 0, 0]
    assert all(cm[f"psp{c}"] is False for c in "abcde")
    assert "pvlf" not in cm  # 5-slot layout, not 12
    src = next(s for s in entt["srcs"] if s["id__"] == cm["trig"])
    assert (src["locl"], src["ctxt"], src["type"]) == (25, 1, 1)
    assert cm["cid_"] in src["cmds"]
    trg = next(t for t in entt["trgs"] if t["id__"] == cm["tid_"])
    assert trg == {"eID_": cm["cid_"], "enty": 6, "id__": cm["tid_"],
                   "pid_": 0, "slot": 0, "type": 4}
    # Snapshot value lands in pvle.
    cg2 = _transcode_entt(tmp_path, [
        {"switch": "FS1", "command": "snapshot", "snapshot": 3}])
    assert cg2["entt"]["cmnd"][0]["pvle"] == 3


def test_transcode_midi_pc_cmnd_matches_zzcap_anchor(tmp_path):
    """A MIDI PC command on Instant2 reproduces the ZZCAP-CC Instant PC cmnd
    (12 int + 12 bool, type 6, func 0): [0, ch, msb, lsb, -1, 0,0,0, 100, 1, 0,0].
    Instant srcs = locl N/ctxt 0/type 4."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "Instant2", "command": "midi_pc", "program": 0, "channel": 4}])
    entt = cg["entt"]
    cm = entt["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 0
    assert [cm[f"pvl{c}"] for c in "abcdefghijkl"] == \
        [0, 4, -1, -1, -1, 0, 0, 0, 100, 1, 0, 0]
    assert all(cm[f"psp{c}"] is False for c in "abcdefghijkl")
    src = next(s for s in entt["srcs"] if s["id__"] == cm["trig"])
    assert (src["locl"], src["ctxt"], src["type"]) == (1, 0, 4)  # Instant2 = locl 1


def test_transcode_footswitch_cc_matches_capture(tmp_path):
    """FS CC command reproduces the HW-captured footswitch 12-slot layout
    (2026-07-15 findings §TARGET D): device func=1, and the footswitch layout
    reserves pvl1=subtype and shifts data +1 vs Instant:
    pvl=[0, 1, ch, -1, -1, -1, CC#, val, 0, 100, 1, 0]."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "FS1", "command": "midi_cc", "cc": 45, "value": 0,
         "channel": 5}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 1
    assert [cm[f"pvl{c}"] for c in "abcdefghijkl"] == \
        [0, 1, 5, -1, -1, -1, 45, 0, 0, 100, 1, 0]
    assert all(cm[f"psp{c}"] is False for c in "abcdefghijkl")
    src = next(s for s in cg["entt"]["srcs"] if s["id__"] == cm["trig"])
    assert (src["locl"], src["ctxt"], src["type"]) == (25, 1, 1)  # FS1


def test_transcode_footswitch_note_matches_capture(tmp_path):
    """FS Note: device func=2 (.hsp ``Command`` 3 -> device 2 — Note/MMC
    swapped), note@pvl8, vel@pvl9. HW capture: [0,2,7,-1,-1,-1,0,0,40,77,1,0]."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "FS1", "command": "midi_note", "note": 40, "velocity": 77,
         "channel": 7}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 2
    assert [cm[f"pvl{c}"] for c in "abcdefghijkl"] == \
        [0, 2, 7, -1, -1, -1, 0, 0, 40, 77, 1, 0]


def test_transcode_footswitch_mmc_matches_capture(tmp_path):
    """FS MMC: device func=3 (.hsp ``Command`` 2 -> device 3), message@pvl11.
    HW capture: [0,3,1,-1,-1,-1,0,0,0,100,1,5]."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "FS1", "command": "midi_mmc", "message": 5, "channel": 1}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 3
    assert [cm[f"pvl{c}"] for c in "abcdefghijkl"] == \
        [0, 3, 1, -1, -1, -1, 0, 0, 0, 100, 1, 5]


def test_transcode_footswitch_pc_layout(tmp_path):
    """FS PC/Bank: the footswitch 12-slot layout puts program@pvl0,
    subtype@pvl1=0, ch@pvl2, MSB@pvl3, LSB@pvl4 (extends the captured
    footswitch layout; PC not isolated on hardware but the +1 shift is fixed)."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "FS1", "command": "midi_pc", "program": 12, "channel": 3,
         "bank_msb": 1, "bank_lsb": 2}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 0
    assert [cm[f"pvl{c}"] for c in "abcdefghijkl"] == \
        [12, 0, 3, 1, 2, -1, 0, 0, 0, 100, 1, 0]


def test_transcode_instant_layout_unchanged_by_fs_fix(tmp_path):
    """Regression: the Instant layout (ch@pvl1, NO subtype slot) must NOT pick
    up the footswitch +1 shift. Instant CC keeps CC#@pvl5, val@pvl6, func=1."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "Instant1", "command": "midi_cc", "cc": 45, "value": 7,
         "channel": 5}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 1
    assert [cm[f"pvl{c}"] for c in "abcdefghijkl"] == \
        [0, 5, 0, 0, -1, 45, 7, 0, 100, 1, 0, 0]


def test_transcode_instant_note_func_swapped(tmp_path):
    """Instant Note emits DEVICE func=2 (.hsp Command 3 -> device 2). The swap
    direction on Instant is ASSUMED (global func enum; only Instant PC was
    captured, and 0 is swap-invariant) — this pins the assumption so an
    accidental reversion is caught. Slot layout stays Instant (note@pvl10,
    vel@pvl8 — the pre-capture placement, also uncaptured)."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "Instant1", "command": "midi_note", "note": 40,
         "velocity": 77, "channel": 7}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 2
    assert cm["pvlb"] == 7  # ch@pvl1: Instant layout, no subtype slot


def test_transcode_instant_mmc_func_swapped(tmp_path):
    """Instant MMC emits DEVICE func=3 (.hsp Command 2 -> device 3) — same
    pinned assumption as Instant Note."""
    cg = _transcode_entt(tmp_path, [
        {"switch": "Instant1", "command": "midi_mmc", "message": 5,
         "channel": 1}])
    cm = cg["entt"]["cmnd"][0]
    assert cm["type"] == 6 and cm["func"] == 3
    assert cm["pvlb"] == 1  # ch@pvl1: Instant layout, no subtype slot


def test_transcode_footswitch_unknown_func_drops_with_warning(tmp_path, capsys):
    """A hand-edited .hsp with an out-of-range MIDI ``Command`` on a footswitch
    is dropped with a stderr warning (not silently)."""
    from helixgen.device import transcode
    payload = transcode._command_payload("MIDI", 9, {"MIDI Ch": 1}, ctxt=1)
    assert payload is None
    assert "unknown MIDI Command subtype" in capsys.readouterr().err


def test_transcode_midi_cc_and_merged_instant(tmp_path):
    cg = _transcode_entt(tmp_path, [
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127,
         "channel": 2, "toggle": True},
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 0},
    ])
    entt = cg["entt"]
    assert len(entt["cmnd"]) == 2
    # Both commands share ONE Instant1 srcs entry (merged switch).
    trigs = {cm["trig"] for cm in entt["cmnd"]}
    assert len(trigs) == 1
    src = next(s for s in entt["srcs"] if s["id__"] in trigs)
    assert src["locl"] == 0  # Instant1
    assert sorted(c for c in src["cmds"] if c != -1) == \
        sorted(cm["cid_"] for cm in entt["cmnd"])
    cc = entt["cmnd"][0]
    assert cc["func"] == 1 and cc["pvlf"] == 85 and cc["pvlg"] == 127
    assert cc["togl"] is True


def test_transcode_command_does_not_corrupt_controllers(tmp_path):
    """A footswitch controller (block bypass) AND commands coexist: command
    srcs are appended, sm__.scid stays controller-only, ids do not collide."""
    from helixgen.recipe import apply_recipe
    from helixgen.device import content, transcode
    library = _real_library(tmp_path)
    body = apply_recipe({"name": "mix", "paths": [{"blocks": [
        {"block": "Brit Plexi Nrm", "params": {"Drive": 0.6}}]}],
        "footswitches": [{"switch": "FS2", "block": "Brit Plexi Nrm"}],
        "commands": [{"switch": "Instant1", "command": "midi_pc", "program": 1}]},
        library, chassis=_chassis())
    entt = content.decode_any(transcode.hsp_to_sbepgsm(body))["cg__"]["entt"]
    # controller srcs (FS2) + command srcs (Instant1) both present, distinct ids
    ids = [s["id__"] for s in entt["srcs"]]
    assert len(ids) == len(set(ids))
    # sm__.scid references only the controller source, not the command source
    cmd_src = next(s for s in entt["srcs"] if s["type"] == 4)
    scid_srcs = entt["sm__"]["scid"][0::2]
    assert cmd_src["id__"] not in scid_srcs
    # the FS2 bypass ctrl survived
    assert any(c["type"] == 1 for c in entt["ctrl"])


def test_view_skips_recall_preset_command(tmp_path, capsys):
    """A device export carrying a recall-PRESET PresetSnapshot command (Preset/
    Setlist set) is out of scope — view drops it with a warning instead of
    misprojecting it as a snapshot (H1)."""
    from helixgen.view import view
    body, library = _author(tmp_path, [
        {"switch": "FS1", "command": "snapshot", "snapshot": 1}])
    # Hand-inject a recall-preset record onto FS2.
    body["preset"]["commands"][str(0x01010101)] = [{
        "type": "PresetSnapshot", "behavior": "latching", "ordinal": 0,
        "toggle": False, "params": {"Action": {"value": 0}, "Command": {"value": 0},
        "Preset": {"value": 3}, "Setlist": {"value": 1}, "Snapshot": {"value": 0}}}]
    proj = view(body, library)
    assert "recall-preset" in capsys.readouterr().err
    switches = {c["switch"] for c in proj["commands"]}
    assert switches == {"FS1"}  # the preset command dropped


def test_view_buckets_fs_collision_command_as_unknown(tmp_path, capsys):
    """A device export may carry BOTH a block-bypass footswitch AND a command
    on one switch (Mandarin Fuzz's FS1 does) — a combination parse_spec rejects
    at authoring time. `view` must keep the projection parseable: the command
    goes to `unknown_controllers` (ignored by parse_spec), not `commands`
    (adversarial-review round 2, Critical)."""
    from helixgen.view import view
    from helixgen.spec import parse_spec
    # Author a footswitch on FS1, then hand-inject a command on the same FS1
    # (wire_command can't author this — parse_spec rejects it — but a real
    # device export carries exactly this shape).
    from helixgen.recipe import apply_recipe
    library = harness.build_corpus_library(tmp_path)
    body = apply_recipe({
        "name": "collide",
        "paths": [{"blocks": [{"block": "Brit 2204 Custom"}]}],
        "footswitches": [{"switch": "FS1", "block": "Brit 2204 Custom"}],
    }, library, chassis=_chassis())
    body["preset"]["commands"] = {str(0x01010100): [{
        "type": "PresetSnapshot", "behavior": "latching", "ordinal": 0,
        "toggle": False, "params": {
            "Action": {"value": 0}, "Command": {"value": 0},
            "Preset": {"value": 0}, "Setlist": {"value": 0},
            "Snapshot": {"value": 0}}}]}
    projected = view(body, library)
    assert "unknown_controllers" in projected
    unk = [u for u in projected["unknown_controllers"] if u["kind"] == "command"]
    assert len(unk) == 1
    assert unk[0]["switch"] == "FS1"
    assert unk[0]["command"]["command"] == "snapshot"
    assert "commands" not in projected  # no first-class entry
    assert "not yet authorable" in capsys.readouterr().err
    # THE regression: the projection must re-parse (the 211-export net).
    parse_spec(projected)


def test_nxtm_unchanged_for_command_free_preset(tmp_path):
    """M1: adding command synthesis must not change nxtm for a preset with no
    commands (only snapshots/controllers) — it stays the historical 1."""
    from helixgen.recipe import apply_recipe
    from helixgen.device import content, transcode
    library = _real_library(tmp_path)
    body = apply_recipe({"name": "snaps", "paths": [{"blocks": [
        {"block": "Brit Plexi Nrm", "params": {"Drive": 0.6}}]}],
        "snapshots": [{"name": "A"}, {"name": "B", "params": {
            "Brit Plexi Nrm": {"Drive": 0.9}}}]}, library, chassis=_chassis())
    cg = content.decode_any(transcode.hsp_to_sbepgsm(body))["cg__"]
    assert cg["nxtm"] == 1


def test_commands_survive_block_edits(tmp_path):
    """Commands are keyed by switch source, not block coordinate, so add/remove
    block renumbering must not touch them (backlog #16 §4)."""
    from helixgen import mutate
    body, library = _author(tmp_path, [
        {"switch": "FS1", "command": "snapshot", "snapshot": 2},
    ])
    before = copy.deepcopy(body["preset"]["commands"])
    # Removing the first block renumbers the second's position; commands
    # (switch-keyed, not block-keyed) are untouched by construction.
    mutate.remove_block(body, "Brit 2204 Custom", library, path=0)
    assert body["preset"]["commands"] == before
