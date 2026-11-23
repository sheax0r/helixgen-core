"""The read-only Command Center pane (81v.4): LiveSession.commands() projects
the preset's command assignments off the live edit buffer.

The underlying read model — untranscode(``_sbepgsm``) -> ``.hsp`` -> flatten
(``bridge.hsp_commands``) — is covered by the device round-trip suites. What's
NEW here is the thin projection: resolving the device MIDI subtype (Note/MMC are
swapped vs the editor vocab), the per-family field selection, English source
labels, and the grouping + cold-start guard.
"""
from __future__ import annotations

from helixgen.liveedit.session import (
    LiveSession, _command_display, _command_fields, _enum_label,
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
