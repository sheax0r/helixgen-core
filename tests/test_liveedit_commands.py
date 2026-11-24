"""The read-only Command Center pane (81v.4): LiveSession.commands() projects
the preset's command assignments off the live edit buffer.

The underlying read model — untranscode(``_sbepgsm``) -> ``.hsp`` -> flatten
(``bridge.hsp_commands``) — is covered by the device round-trip suites. What's
NEW here is the thin projection: resolving the device MIDI subtype (Note/MMC are
swapped vs the editor vocab), the per-family field selection, English source
labels, and the grouping + cold-start guard.
"""
from __future__ import annotations

import pytest

from helixgen.liveedit.session import (
    LiveError, LiveSession, _command_display, _command_fields, _enum_label,
    _format_commands, _instant_label)

# The slice of _param_ui.json controls the formatter reads.
CONTROLS = {
    "cc_action": {"labels": ["Press", "Release", "Hold", "Toggle"]},
    "cc_presetsnapshot_command": {"labels": [
        "Next Bank", "Prev Bank", "Next Preset", "Prev Preset", "Preset",
        "Next Snap", "Prev Snap", "Snapshot"]},
    "cc_midi_message": {"labels": ["Stop", "Play", "Fast Forward"]},
    "midi_note": {"labels": ["C-1"] + [f"n{i}" for i in range(1, 60)] + ["B3"]},
}


# --- _enum_label --------------------------------------------------------------

def test_enum_label_in_range_and_out():
    assert _enum_label(CONTROLS, "cc_action", 0) == "Press"
    assert _enum_label(CONTROLS, "cc_action", 3) == "Toggle"
    assert _enum_label(CONTROLS, "cc_action", 99) is None   # out of range
    assert _enum_label(CONTROLS, "nope", 0) is None         # unknown enum
    assert _enum_label(CONTROLS, "cc_action", None) is None


def test_instant_label():
    assert _instant_label("Instant7") == "Instant 7"
    assert _instant_label("FS3") is None
    assert _instant_label(None) is None


# --- _command_fields: per-family slot selection -------------------------------

def test_midi_cc_fields_show_value_zero():
    # a legitimate CC Value of 0 must NOT be hidden (the honest-zero rule).
    f = _command_fields("MIDI", {"Command": 1, "MIDI Ch": 2, "CC#": 11,
                                 "Value": 0}, CONTROLS)
    assert f == [["Channel", "2"], ["CC#", "11"], ["Value", "0"]]


def test_midi_bank_program_fields():
    f = _command_fields("MIDI", {"Command": 0, "MIDI Ch": 1, "PC": 5,
                                 "MSB": 0, "LSB": 2}, CONTROLS)
    assert f == [["Channel", "1"], ["PC", "5"], ["MSB(CC00)", "0"],
                 ["LSB(CC32)", "2"]]


def test_midi_bank_program_unset_slots_show_off_not_minus_one():
    # The forward path writes -1 into an unset MSB/LSB/PC; show "Off", not -1.
    f = _command_fields("MIDI", {"Command": 0, "MIDI Ch": 1, "PC": 5,
                                 "MSB": -1, "LSB": -1}, CONTROLS)
    assert f == [["Channel", "1"], ["PC", "5"], ["MSB(CC00)", "Off"],
                 ["LSB(CC32)", "Off"]]


def test_midi_note_resolves_note_name():
    f = _command_fields("MIDI", {"Command": 3, "MIDI Ch": 1, "Note": 0,
                                 "Velocity": 100}, CONTROLS)
    assert f == [["Channel", "1"], ["Note", "C-1"], ["Velocity", "100"]]


def test_midi_mmc_resolves_message_label():
    f = _command_fields("MIDI", {"Command": 2, "MIDI Ch": 1, "Message": 1},
                        CONTROLS)
    assert f == [["Channel", "1"], ["Message", "Play"]]


def test_presetsnap_preset_vs_snapshot_fields():
    assert _command_fields("PresetSnapshot", {"Command": 4, "Setlist": 1,
                                              "Preset": 12}, CONTROLS) == \
        [["Setlist", "1"], ["Preset", "12"]]
    assert _command_fields("PresetSnapshot", {"Command": 7, "Snapshot": 3},
                           CONTROLS) == [["Snapshot", "3"]]
    # a bank/preset-step command carries no extra fields
    assert _command_fields("PresetSnapshot", {"Command": 0}, CONTROLS) == []


# --- _command_display: headline / action / subtype swap -----------------------

def test_display_midi_cc_headline_no_action():
    # The decoder populates "Action" only for PresetSnapshot, never for MIDI, so
    # a MIDI command reaching this pane has no Action — the chip stays empty.
    rec = {"type": "MIDI", "behavior": "momentary", "toggle": False,
           "params": {"Command": 1, "MIDI Ch": 1, "CC#": 7, "Value": 127}}
    d = _command_display(rec, CONTROLS)
    assert d["family"] == "MIDI" and d["headline"] == "MIDI CC"
    assert d["action"] == "" and d["behavior"] == "momentary"
    assert ["CC#", "7"] in d["fields"]


def test_display_midi_note_subtype_not_mmc():
    # .hsp Command 3 is Note On, NOT MMC — the swap against cc_midi_command.
    d = _command_display({"type": "MIDI", "params": {"Command": 3}}, CONTROLS)
    assert d["headline"] == "MIDI Note On"
    d2 = _command_display({"type": "MIDI", "params": {"Command": 2}}, CONTROLS)
    assert d2["headline"] == "MIDI MMC"


def test_display_presetsnapshot_headline_and_action():
    # PresetSnapshot DOES carry Action (pvl0), so the chip renders here.
    d = _command_display({"type": "PresetSnapshot",
                          "params": {"Action": 0, "Command": 7, "Snapshot": 2}},
                         CONTROLS)
    assert d["family"] == "Preset/Snapshot" and d["headline"] == "Snapshot"
    assert d["action"] == "Press"


def test_display_unknown_family_passes_through():
    d = _command_display({"type": "Looper", "params": {}}, CONTROLS)
    assert d["family"] == "Looper" and d["headline"] == "Looper"


# --- _format_commands: grouping + source labels -------------------------------

def test_format_groups_by_source_with_english_labels():
    cmds = [
        {"source": 16843010, "type": "MIDI",
         "params": {"Action": 0, "Command": 1, "MIDI Ch": 1, "CC#": 11,
                    "Value": 64}},
        {"source": 16843010, "type": "PresetSnapshot",
         "params": {"Action": 0, "Command": 7, "Snapshot": 1}},
    ]
    labels = {"FS3": "Footswitch 3 (top row, 3rd from left)"}
    out = _format_commands(cmds, "stadium_xl", CONTROLS, labels)
    assert len(out["sources"]) == 1                 # both on one source
    s = out["sources"][0]
    assert s["id"] == "FS3" and s["label"] == labels["FS3"] and s["named"]
    assert [c["headline"] for c in s["commands"]] == ["MIDI CC", "Snapshot"]


def test_format_unknown_source_is_shown_not_dropped():
    # a source helixgen can't name must still appear (never silently lost).
    out = _format_commands([{"source": 0x09990000, "type": "MIDI",
                             "params": {"Command": 1}}], "stadium_xl",
                           CONTROLS, {})
    assert len(out["sources"]) == 1
    s = out["sources"][0]
    assert s["named"] is False and s["id"].startswith("0x")


def test_format_instant_source_gets_friendly_label():
    from helixgen import controllers as _ctl
    sid = _ctl.resolve_command_source("stadium_xl", "Instant1")
    out = _format_commands([{"source": sid, "type": "MIDI",
                             "params": {"Command": 1}}], "stadium_xl",
                           CONTROLS, {})
    s = out["sources"][0]
    assert s["id"] == "Instant1" and s["label"] == "Instant 1" and s["named"]


# --- LiveSession.commands(): the wiring ---------------------------------------

def test_commands_empty_when_no_buffer():
    """Cold start (no buffer read yet) -> no sources, never a crash, no I/O."""
    assert LiveSession().commands() == {"sources": []}


def test_commands_projects_off_the_buffer(monkeypatch):
    """End-to-end wiring with the heavy read model stubbed: untranscode -> flatten
    -> group, returning English-labelled sources."""
    from helixgen import controllers as _controllers
    from helixgen import view as _view
    from helixgen.device import bridge as _bridge

    monkeypatch.setattr("helixgen.device.untranscode.sbepgsm_to_hsp",
                        lambda eb, **k: {"_marker": "body"})
    monkeypatch.setattr(_bridge, "hsp_commands", lambda body: [
        {"source": 16843010, "type": "MIDI",
         "params": {"Action": 0, "Command": 1, "MIDI Ch": 1, "CC#": 11,
                    "Value": 64}}])
    monkeypatch.setattr(_view, "_device_id", lambda body: "stadium_xl")
    monkeypatch.setattr(_controllers, "controller_mapping",
                        lambda dev: [{"id": "FS3",
                                      "english": "Footswitch 3 (x)"}])

    sess = LiveSession()
    sess._lib = object()                       # skip real Library.load()
    sess._eb = {"sfg_": {"flow": []}, "cg__": {}}
    out = sess.commands()

    assert len(out["sources"]) == 1
    assert out["sources"][0]["label"] == "Footswitch 3 (x)"
    assert out["sources"][0]["commands"][0]["headline"] == "MIDI CC"


def test_commands_empty_when_buffer_has_none(monkeypatch):
    """A buffer with no commands -> empty sources (not a crash)."""
    from helixgen.device import bridge as _bridge
    monkeypatch.setattr("helixgen.device.untranscode.sbepgsm_to_hsp",
                        lambda eb, **k: {})
    monkeypatch.setattr(_bridge, "hsp_commands", lambda body: [])
    sess = LiveSession()
    sess._lib = object()
    sess._eb = {"cg__": {}}
    assert sess.commands() == {"sources": []}


# --- command_add / command_remove (81v.4 WRITE): the authoring verbs ----------
# The wire-captured write surface is MIDI (family 6) on a FOOTSWITCH source; the
# capture (2026-11-23) confirmed /attachCommandWithType + /setCommandParamVal
# (pvlIndex == transcode._command_payload's footswitch layout) + /removeCommand.
# These pin that the session resolves the switch -> (type, locl, ctxt), attaches
# at subtype 0, and writes the family's pvl slots.

def _cmd_session():
    from tests.test_liveedit_session import FakeClient, FakeLease
    c = FakeClient()
    s = LiveSession(c, lease=FakeLease())
    s.load()
    return c, s


def test_command_add_footswitch_cc_writes_the_captured_pvl_slots():
    c, s = _cmd_session()
    s.command_add("FS1", 1, {"MIDI Ch": 2, "CC#": 11, "Value": 64})
    # FS1 -> (type 1, locl 25, ctxt 1); family 6 (MIDI). The command's func is
    # set by the ATTACH subtype (HW: a later index-1 write does NOT change it),
    # so CC attaches at device func 1 — slot 1 allocated.
    assert c.cmd_attaches == [(1, 25, 1, 6, 1, 1)]
    # Footswitch CC layout (HW-captured): subtype@1=CC(1), channel@2, CC#@6, Value@7.
    sets = {i: v for (_slot, i, v) in c.cmd_param_sets}
    assert sets[1] == 1 and sets[2] == 2 and sets[6] == 11 and sets[7] == 64
    # everything written against the device-allocated slot 1
    assert {slot for (slot, _i, _v) in c.cmd_param_sets} == {1}


def test_command_add_bank_program_writes_pc_and_bank_slots():
    c, s = _cmd_session()
    s.command_add("FS2", 0, {"MIDI Ch": 1, "PC": 5, "MSB": 0, "LSB": 2})
    sets = {i: v for (_slot, i, v) in c.cmd_param_sets}
    # Footswitch Bank/Program: PC@0, subtype@1=0, channel@2, MSB@3, LSB@4.
    assert sets[0] == 5 and sets[1] == 0 and sets[2] == 1
    assert sets[3] == 0 and sets[4] == 2


def test_command_add_attaches_at_the_device_func_with_note_mmc_swapped():
    # func is set by the attach subtype; Note/MMC are swapped device<->.hsp
    # (.hsp Note On=3 -> device 2; .hsp MMC=2 -> device 3).
    c, s = _cmd_session()
    s.command_add("FS1", 3, {"MIDI Ch": 1, "Note": 60, "Velocity": 100})  # Note On
    assert c.cmd_attaches[0][4] == 2
    c2, s2 = _cmd_session()
    s2.command_add("FS1", 2, {"MIDI Ch": 1, "Message": 1})                # MMC
    assert c2.cmd_attaches[0][4] == 3


def test_command_add_rejects_instant_source_uncaptured():
    # The live capture drove a footswitch only; Instant command authoring is not
    # wire-captured, so the session refuses it rather than guessing the layout.
    c, s = _cmd_session()
    with pytest.raises(LiveError):
        s.command_add("Instant1", 1, {"CC#": 1, "Value": 1})
    assert c.cmd_attaches == []


def test_command_add_rejects_unknown_func():
    c, s = _cmd_session()
    with pytest.raises(LiveError):
        s.command_add("FS1", 9, {})
    assert c.cmd_attaches == []


def test_command_add_rejects_out_of_range_value():
    c, s = _cmd_session()
    with pytest.raises(LiveError):
        s.command_add("FS1", 1, {"CC#": 200})
    assert c.cmd_attaches == []        # refused before touching the device


def test_command_add_rejects_reserved_footswitch():
    c, s = _cmd_session()
    with pytest.raises(LiveError):
        s.command_add("FS6", 1, {"CC#": 1})   # FS6/FS12 are reserved
    assert c.cmd_attaches == []


def _stub_two_commands_on(monkeypatch, switch):
    """Make commands() project two MIDI commands onto ``switch``, carrying the
    device slots (cmnd cid_) the live read model exposes — the list is newest
    first (slot 7 then slot 3), as the hardware orders them."""
    from helixgen import controllers as _controllers
    from helixgen import view as _view
    from helixgen.device import bridge as _bridge
    sid = _controllers.resolve_command_source("stadium_xl", switch)
    monkeypatch.setattr("helixgen.device.untranscode.sbepgsm_to_hsp",
                        lambda eb, **k: {"_marker": "body"})
    monkeypatch.setattr(_bridge, "hsp_commands", lambda body: [
        {"source": sid, "type": "MIDI", "slot": 7, "params": {"Command": 1, "CC#": 1}},
        {"source": sid, "type": "MIDI", "slot": 3, "params": {"Command": 1, "CC#": 2}}])
    monkeypatch.setattr(_view, "_device_id", lambda body: "stadium_xl")
    monkeypatch.setattr(_controllers, "controller_mapping", lambda dev: [])


def test_command_remove_addresses_the_device_slot(monkeypatch):
    c, s = _cmd_session()
    _stub_two_commands_on(monkeypatch, "FS3")
    s.command_remove("FS3", 3)        # the stable cid_ handle, not a position
    s.command_remove("FS3", 7)
    assert c.cmd_removes == [3, 7]


def test_command_remove_rejects_a_slot_not_on_the_switch(monkeypatch):
    c, s = _cmd_session()
    _stub_two_commands_on(monkeypatch, "FS3")
    with pytest.raises(LiveError):
        s.command_remove("FS3", 99)   # no command with that slot -> never deletes
    assert c.cmd_removes == []


def test_command_remove_surfaces_a_device_refusal(monkeypatch):
    c, s = _cmd_session()
    _stub_two_commands_on(monkeypatch, "FS3")
    c.cmd_remove_fail = True
    with pytest.raises(LiveError):
        s.command_remove("FS3", 3)


def test_command_read_model_marks_midi_footswitch_with_slot_editable():
    from helixgen.liveedit.session import _format_commands
    sid = __import__("helixgen.controllers", fromlist=["x"]).resolve_command_source(
        "stadium_xl", "FS3")
    out = _format_commands([
        {"source": sid, "type": "MIDI", "slot": 5, "params": {"Command": 1, "CC#": 7}},
        {"source": sid, "type": "MIDI", "slot": None, "params": {"Command": 1}},
    ], "stadium_xl", CONTROLS, {})
    cmds = out["sources"][0]["commands"]
    assert cmds[0]["editable"] and cmds[0]["slot"] == 5 and cmds[0]["source"] == "FS3"
    assert not cmds[1]["editable"]            # no device slot -> not addressable
