"""Recipe parsing/validation for the top-level ``midi`` list (backlog #33).

MIDI CC controller sources: a CC# sweeps a param (like an expression pedal) or
toggles a block's bypass. CC-only (MIDI Note controller sources are out of
scope — the parity capture pinned only the CC encoding; see BACKLOG #33).
"""
from __future__ import annotations

import pytest

from helixgen import spec as specmod


def _base(**extra):
    d = {
        "name": "midi test",
        "paths": [{"blocks": [
            {"block": "Brit Plexi Brt", "params": {"Drive": 0.6}},
            {"block": "Tape Echo Stereo", "params": {"Mix": 0.2}},
        ]}],
    }
    d.update(extra)
    return d


def test_no_midi_key_yields_empty():
    s = specmod.parse_spec(_base())
    assert s.midi == []


def test_cc_param_sweep():
    s = specmod.parse_spec(_base(midi=[
        {"cc": 61, "targets": [{"block": "Brit Plexi Brt", "param": "Drive",
                                "min": 0.2, "max": 0.9}]},
    ]))
    assert len(s.midi) == 1
    m = s.midi[0]
    assert m.cc == 61
    t = m.targets[0]
    assert t.block == "Brit Plexi Brt" and t.param == "Drive"
    assert t.bypass is False and t.min == 0.2 and t.max == 0.9


def test_cc_param_default_range():
    s = specmod.parse_spec(_base(midi=[
        {"cc": 20, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
    ]))
    t = s.midi[0].targets[0]
    assert t.min == 0.0 and t.max == 1.0


def test_cc_bypass_toggle():
    s = specmod.parse_spec(_base(midi=[
        {"cc": 79, "targets": [{"block": "Tape Echo Stereo", "bypass": True}]},
    ]))
    t = s.midi[0].targets[0]
    assert t.param is None and t.bypass is True


def test_cc_multiple_targets():
    s = specmod.parse_spec(_base(midi=[
        {"cc": 30, "targets": [
            {"block": "Brit Plexi Brt", "param": "Drive"},
            {"block": "Tape Echo Stereo", "bypass": True},
        ]},
    ]))
    assert len(s.midi[0].targets) == 2


@pytest.mark.parametrize("cc", [-1, 128, 200])
def test_cc_out_of_range(cc):
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"cc": cc, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
        ]))


def test_cc_must_be_int():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"cc": 1.5, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
        ]))


def test_bypass_and_param_conflict():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"cc": 10, "targets": [{"block": "Brit Plexi Brt", "param": "Drive",
                                    "bypass": True}]},
        ]))


def test_target_needs_param_or_bypass():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"cc": 10, "targets": [{"block": "Brit Plexi Brt"}]},
        ]))


def test_duplicate_cc_rejected():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"cc": 10, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
            {"cc": 10, "targets": [{"block": "Tape Echo Stereo", "param": "Mix"}]},
        ]))


def test_empty_targets_rejected():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[{"cc": 10, "targets": []}]))


def test_note_source_rejected():
    """MIDI Note sources are out of scope (CC-only). A ``note`` field errors."""
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"note": 60, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
        ]))


# --- one-controller-per-param exclusivity across FS / EXP / MIDI --------------

def test_midi_param_collides_with_expression():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(
            expression=[{"pedal": "EXP1", "targets": [
                {"block": "Brit Plexi Brt", "param": "Drive"}]}],
            midi=[{"cc": 61, "targets": [
                {"block": "Brit Plexi Brt", "param": "Drive"}]}],
        ))


def test_midi_param_collides_with_footswitch_param():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(
            footswitches=[{"switch": "FS3", "block": "Brit Plexi Brt",
                           "param": "Drive", "min": 0.2, "max": 0.9}],
            midi=[{"cc": 61, "targets": [
                {"block": "Brit Plexi Brt", "param": "Drive"}]}],
        ))


def test_midi_param_duplicate_within_midi():
    with pytest.raises(specmod.SpecError):
        specmod.parse_spec(_base(midi=[
            {"cc": 61, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
            {"cc": 62, "targets": [{"block": "Brit Plexi Brt", "param": "Drive"}]},
        ]))


def test_midi_bypass_may_coexist_with_fs_bypass():
    """A block's BYPASS may be driven by both an FS and a MIDI CC (the device
    supports multiple bypass sources); only PARAM drivers are exclusive."""
    s = specmod.parse_spec(_base(
        footswitches=[{"switch": "FS3", "block": "Tape Echo Stereo"}],
        midi=[{"cc": 79, "targets": [
            {"block": "Tape Echo Stereo", "bypass": True}]}],
    ))
    assert s.midi and s.footswitches
