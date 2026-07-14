"""Recipe parsing/validation for the top-level ``commands`` list (Command
Center, backlog #16). A command binds a footswitch/Instant slot to a MIDI
message (PC/CC/Note/MMC) or a Preset/Snapshot action."""
from __future__ import annotations

import pytest

from helixgen import spec as specmod


def _base(**extra):
    d = {
        "name": "cc test",
        "paths": [{"blocks": [
            {"block": "Brit Plexi Brt", "params": {"Drive": 0.6}},
        ]}],
    }
    d.update(extra)
    return d


def test_no_commands_yields_empty():
    assert specmod.parse_spec(_base()).commands == []


def test_midi_cc_command():
    s = specmod.parse_spec(_base(commands=[
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127,
         "channel": 2, "toggle": True},
    ]))
    assert len(s.commands) == 1
    c = s.commands[0]
    assert c.switch == "Instant1" and c.command == "midi_cc"
    assert c.fields == {"cc": 85, "value": 127, "channel": 2}
    assert c.toggle is True


def test_midi_cc_defaults():
    s = specmod.parse_spec(_base(commands=[
        {"switch": "Instant1", "command": "midi_cc", "cc": 10},
    ]))
    assert s.commands[0].fields == {"cc": 10, "value": 0, "channel": 1}


def test_midi_pc_command():
    s = specmod.parse_spec(_base(commands=[
        {"switch": "Instant2", "command": "midi_pc", "program": 44, "channel": 4},
    ]))
    c = s.commands[0]
    assert c.fields == {"program": 44, "channel": 4, "bank_msb": -1, "bank_lsb": -1}


def test_midi_note_command():
    s = specmod.parse_spec(_base(commands=[
        {"switch": "FS3", "command": "midi_note", "note": 60, "velocity": 100,
         "note_off": True},
    ]))
    c = s.commands[0]
    assert c.fields == {"note": 60, "velocity": 100, "channel": 1, "note_off": True}


def test_snapshot_command():
    s = specmod.parse_spec(_base(commands=[
        {"switch": "FS1", "command": "snapshot", "snapshot": 2},
    ]))
    assert s.commands[0].fields == {"snapshot": 2}


def test_preset_family_not_offered():
    # The recall-preset family is deferred (unanchored + device-ambiguous).
    with pytest.raises(specmod.SpecError, match="command"):
        specmod.parse_spec(_base(commands=[
            {"switch": "FS2", "command": "preset", "preset": 3}]))


def test_merged_switch_two_commands():
    s = specmod.parse_spec(_base(commands=[
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 127},
        {"switch": "Instant1", "command": "midi_cc", "cc": 85, "value": 0},
    ]))
    assert len(s.commands) == 2
    assert all(c.switch == "Instant1" for c in s.commands)


def test_more_than_two_merged_rejected():
    with pytest.raises(specmod.SpecError, match="at most 2"):
        specmod.parse_spec(_base(commands=[
            {"switch": "Instant1", "command": "midi_cc", "cc": 1, "value": v}
            for v in (10, 20, 30)]))


def test_unknown_command_rejected():
    with pytest.raises(specmod.SpecError, match="command"):
        specmod.parse_spec(_base(commands=[{"switch": "FS1", "command": "bogus"}]))


def test_missing_required_field_rejected():
    with pytest.raises(specmod.SpecError, match='requires "cc"'):
        specmod.parse_spec(_base(commands=[{"switch": "FS1", "command": "midi_cc"}]))


def test_out_of_range_cc_rejected():
    with pytest.raises(specmod.SpecError, match="0..127"):
        specmod.parse_spec(_base(commands=[
            {"switch": "FS1", "command": "midi_cc", "cc": 200}]))


def test_out_of_range_channel_rejected():
    with pytest.raises(specmod.SpecError, match="1..16"):
        specmod.parse_spec(_base(commands=[
            {"switch": "FS1", "command": "midi_cc", "cc": 5, "channel": 0}]))


def test_snapshot_range_rejected():
    with pytest.raises(specmod.SpecError, match="0..7"):
        specmod.parse_spec(_base(commands=[
            {"switch": "FS1", "command": "snapshot", "snapshot": 9}]))


def test_unknown_field_rejected():
    with pytest.raises(specmod.SpecError, match="unknown field"):
        specmod.parse_spec(_base(commands=[
            {"switch": "FS1", "command": "snapshot", "snapshot": 1, "cc": 5}]))


def test_bad_behavior_rejected():
    with pytest.raises(specmod.SpecError, match="behavior"):
        specmod.parse_spec(_base(commands=[
            {"switch": "FS1", "command": "snapshot", "snapshot": 1,
             "behavior": "wild"}]))


def test_switch_shared_with_footswitch_rejected():
    with pytest.raises(specmod.SpecError, match="both a footswitch"):
        specmod.parse_spec(_base(
            footswitches=[{"switch": "FS1", "block": "Brit Plexi Brt"}],
            commands=[{"switch": "FS1", "command": "snapshot", "snapshot": 1}],
        ))


def test_commands_not_list_rejected():
    with pytest.raises(specmod.SpecError, match='"commands" must be a list'):
        specmod.parse_spec(_base(commands={"switch": "FS1"}))
