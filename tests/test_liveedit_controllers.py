"""The read-only Controllers pane (81v.3): LiveSession.controllers() projects
the current footswitch / expression / MIDI assignments off the live edit buffer.

The underlying read model — untranscode(``_sbepgsm``) -> ``.hsp`` -> ``view``
recipe — is byte-faithfully covered by tests/test_cli_to_hsp.py and the real-
export decompile acceptance suite (it needs real device model ids, absent on a
clean clone). What's NEW here, and tested here, is the thin wrapper: caching the
raw buffer at install, the cold-start guard, and grouping + English relabelling
of the recovered controllers.
"""
from __future__ import annotations

import pytest

from helixgen.liveedit.session import LiveSession, _group_controllers


# --- _group_controllers: the projection logic (pure) --------------------------

def test_group_relabels_footswitches_and_pedals():
    recipe = {
        "footswitches": [
            {"switch": "FS3", "block": "Tube Drive", "behavior": "latching"},
            {"switch": "FS5", "block": "Amp", "param": "Master",
             "min": 0.0, "max": 1.0, "behavior": "momentary"},
        ],
        "expression": [{"pedal": "EXP1", "targets": [
            {"block": "Amp", "param": "Master", "min": 0.0, "max": 0.7}]}],
        "midi": [{"cc": 11, "targets": [{"block": "Amp", "param": "Ch Vol"}]}],
        "unknown_controllers": [
            {"kind": "footswitch", "source": "0x01010200", "label": "unknown"}],
    }
    labels = {"FS3": "Footswitch 3 (top row, 3rd from left)",
              "FS5": "Footswitch 5 (top row, 5th from left)",
              "EXP1": "Expression Pedal 1"}
    out = _group_controllers(recipe, labels)

    assert out["footswitches"][0]["switch_label"] == labels["FS3"]
    assert out["footswitches"][1]["switch_label"] == labels["FS5"]
    assert out["expression"][0]["pedal_label"] == "Expression Pedal 1"
    # MIDI + unknowns pass through untouched (kept visible, not dropped).
    assert out["midi"] == recipe["midi"]
    assert out["unknown"] == recipe["unknown_controllers"]


def test_group_falls_back_to_raw_id_when_unlabelled():
    """An unresolved device gives no label map — the raw switch id is still
    shown rather than crashing or blanking the row."""
    recipe = {"footswitches": [{"switch": "FS7", "block": "X"}]}
    out = _group_controllers(recipe, {})
    assert out["footswitches"][0]["switch_label"] == "FS7"


def test_group_empty_recipe_yields_empty_groups():
    assert _group_controllers({}, {}) == {
        "footswitches": [], "expression": [], "midi": [], "unknown": []}


# --- LiveSession.controllers(): the wiring ------------------------------------

def test_controllers_empty_when_no_buffer():
    """Cold start (no buffer read yet) -> empty groups, never a crash and no
    device touched."""
    assert LiveSession().controllers() == {
        "footswitches": [], "expression": [], "midi": [], "unknown": []}


def test_install_caches_the_raw_buffer():
    """The projection reads the buffer the session last installed, so an install
    must retain the raw eb — chain-building alone used to drop it."""
    sess = LiveSession()
    eb = {"sfg_": {"flow": []}, "cg__": {}}   # minimal decoded-buffer shape
    sess._install(eb, {"cid": 1, "name": "x"})
    assert sess._eb is eb


def test_controllers_projects_and_labels(monkeypatch):
    """End-to-end wiring of the method with the heavy read model stubbed: it
    reads the cached eb, runs view, builds the label map, and returns grouped,
    English-labelled controllers."""
    from helixgen import view as _view
    from helixgen import controllers as _controllers

    recipe = {
        "footswitches": [{"switch": "FS2", "block": "Drive"}],
        "expression": [{"pedal": "EXP2", "targets": [
            {"block": "Amp", "param": "Gain", "min": 0.1, "max": 0.9}]}],
    }
    monkeypatch.setattr("helixgen.device.untranscode.sbepgsm_to_hsp",
                        lambda eb, **k: {"_marker": "body"})
    monkeypatch.setattr(_view, "view", lambda body, lib, **k: recipe)
    monkeypatch.setattr(_view, "_device_id", lambda body: "stadium")
    monkeypatch.setattr(_controllers, "controller_mapping",
                        lambda dev: [{"id": "FS2", "english": "Footswitch 2 (x)"},
                                     {"id": "EXP2", "english": "Expression Pedal 2"}])

    sess = LiveSession()
    sess._lib = object()                       # skip real Library.load()
    sess._eb = {"sfg_": {"flow": []}, "cg__": {}}
    out = sess.controllers()

    assert out["footswitches"][0]["switch_label"] == "Footswitch 2 (x)"
    assert out["expression"][0]["pedal_label"] == "Expression Pedal 2"


def test_controllers_survives_unlabellable_device(monkeypatch):
    """controller_mapping blowing up (unknown device) must not sink the pane —
    the controllers still come back, under their raw ids."""
    from helixgen import view as _view
    from helixgen import controllers as _controllers

    def _boom(dev):
        raise KeyError(dev)

    monkeypatch.setattr("helixgen.device.untranscode.sbepgsm_to_hsp",
                        lambda eb, **k: {})
    monkeypatch.setattr(_view, "view", lambda body, lib, **k:
                        {"footswitches": [{"switch": "FS1", "block": "B"}]})
    monkeypatch.setattr(_view, "_device_id", lambda body: "weird")
    monkeypatch.setattr(_controllers, "controller_mapping", _boom)

    sess = LiveSession()
    sess._lib = object()
    sess._eb = {"sfg_": {"flow": []}, "cg__": {}}
    out = sess.controllers()
    assert out["footswitches"][0]["switch_label"] == "FS1"
